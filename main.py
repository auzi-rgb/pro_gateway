import json
import asyncio
import httpx
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import time
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import jobs_api
import dispatcher
import admission
import keystore
import gwauth
import admin_keys_api


def _count_free_slots() -> int:
    """
    Current number of free node slots across the fleet, using the same accounting
    the dispatcher and chat scheduler use. Passed to jobs_api for admission
    control's wait estimate. Live and self-correcting: a down/circuit-broken node
    contributes zero, so the estimate reflects real capacity.
    """
    free = 0
    for _n in nodes:
        if not _n.enabled or not _n.healthy:
            continue
        if _n.circuit_open_until > time.time():
            continue
        free += max(0, _n.max_concurrent - _n.active_requests)
    return free

import sqlite3
import secrets
from contextlib import contextmanager
from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi import Cookie, Depends
from fastapi.responses import Response

# --- Auth / User DB ---

DB_PATH = "/app/users.db"
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_SECONDS = 86400 * 30  # 30 days
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_jwt_secret():
    secret = os.environ.get("GATEWAY_JWT_SECRET", "")
    if not secret:
        raise RuntimeError("GATEWAY_JWT_SECRET environment variable is not set")
    return secret

# Pre-generate JWT secret at import time so it never races later
_JWT_SECRET = None
def _init_jwt_secret():
    global _JWT_SECRET
    _JWT_SECRET = get_jwt_secret()

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                hashed_password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            first_run_password = secrets.token_urlsafe(16)
            hashed = pwd_context.hash(first_run_password)
            conn.execute(
                "INSERT INTO users (name, email, hashed_password, role) VALUES (?, ?, ?, ?)",
                ("Admin", "admin@georgetowntexas.gov", hashed, "admin")
            )
            log.info("=" * 60)
            log.info("FIRST RUN: Admin account created")
            log.info(f"  Email:    admin@georgetowntexas.gov")
            log.info(f"  Password: {first_run_password}")
            log.info("Change this password after first login!")
            log.info("=" * 60)

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_jwt(email: str, role: str) -> str:
    import time
    payload = {
        "sub": email,
        "role": role,
        "exp": int(time.time()) + JWT_EXPIRE_SECONDS
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)

def decode_jwt(token: str):
    try:
        return jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None

def get_user_by_email(email: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None

def list_users():
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, email, role, created_at FROM users ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

def db_create_user(name: str, email: str, password: str, role: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (name, email, hashed_password, role) VALUES (?, ?, ?, ?)",
            (name, email, hash_password(password), role)
        )

def db_delete_user(user_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

def db_change_password(email: str, new_password: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET hashed_password = ? WHERE email = ?",
            (hash_password(new_password), email)
        )

def require_auth(request: Request, role: str = None):
    token = request.cookies.get("dashboard_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_jwt(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if role == "admin" and payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return payload

# --- Response cache (avoids re-reading log file on every dashboard refresh) ---
_cache = {}
_cache_ttl = 10  # seconds

def get_cached(key):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < _cache_ttl:
        return entry["data"]
    return None

def set_cached(key, data):
    _cache[key] = {"ts": time.time(), "data": data}

# --- Config write lock ---
_config_lock = asyncio.Lock()

async def save_config():
    async with _config_lock:
        with open("/app/config.json", "w") as f:
            json.dump(CONFIG, f, indent=2)

# --- Logging setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gateway")

# --- Request file logger ---
os.makedirs("/app/logs", exist_ok=True)
req_logger = logging.getLogger("requests")
req_logger.setLevel(logging.INFO)
req_handler = RotatingFileHandler(
    "/app/logs/requests.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=5
)
req_handler.setFormatter(logging.Formatter("%(message)s"))
req_logger.addHandler(req_handler)
req_logger.propagate = False

def log_request(path, model_requested, model_used, node, wait_time, duration, success, status_code, client="anonymous", error=None, source_ip=None):
    record = {
        "timestamp": datetime.utcnow().isoformat(),
        "client": client,
        "source_ip": source_ip,
        "path": path,
        "model_requested": model_requested,
        "model_used": model_used,
        "node": node,
        "wait_ms": round(wait_time * 1000),
        "duration_ms": round(duration * 1000),
        "success": success,
        "status_code": status_code,
    }
    if error:
        record["error"] = error
    req_logger.info(json.dumps(record))
    # Track recent connections
    if source_ip:
        _recent_connections[source_ip] = {
            "client": client,
            "source_ip": source_ip,
            "last_seen": datetime.utcnow().isoformat(),
            "last_model": model_used,
            "last_node": node,
            "last_path": path,
        }
        # Prune stale entries inline so dict never grows unboundedly
        cutoff = datetime.utcnow().timestamp() - 86400
        stale = [ip for ip, c in _recent_connections.items()
                 if datetime.fromisoformat(c["last_seen"]).timestamp() < cutoff]
        for ip in stale:
            del _recent_connections[ip]

# --- Load config ---
with open("/app/config.json") as f:
    CONFIG = json.load(f)

DEFAULT_MODEL = CONFIG["default_model"]
REQUIRE_API_KEY = CONFIG["require_api_key"]

# API keys loaded from environment variables, not config.json
# Format: GATEWAY_API_KEY_<NAME> maps to client name in config
_ENV_KEY_PREFIX = "GATEWAY_API_KEY_"
_ENV_KEY_MAP = {
    "OPENWEBUI":        "openwebui",
    "PROJECT_HUB":      "project-hub",
    "HIREDESK":         "hiredesk",
    "MARKDOWN_HUB":     "Markdown Hub",
    "UNPLANNED_INTAKE": "unplanned-intake",
    "IT_EVAL":          "IT Eval",
    "LOAD_TESTER":      "Load Tester",
    "LOAD_TESTER_T1":   "Load Tester T1",
    "LOAD_TESTER_T3":   "Load Tester T3",
}
# Build API_KEYS dict — env vars are authoritative, config.json fills gaps
# This supports both the original env-var keys AND keys created via the dashboard
_config_api_keys = CONFIG.get("api_keys", {})
API_KEYS = {}
KEY_STORE = None  # keystore.KeyStore instance; set in startup()

# First load any keys that have full values in config.json (dashboard-created keys)
for client_name, data in _config_api_keys.items():
    if data.get("key"):
        API_KEYS[client_name] = {"key": data["key"], "tier": data.get("tier", 2)}

# Then overlay with env var keys (these take precedence)
for env_suffix, client_name in _ENV_KEY_MAP.items():
    key_value = os.environ.get(f"{_ENV_KEY_PREFIX}{env_suffix}", "")
    tier = _config_api_keys.get(client_name, {}).get("tier", 2)
    if key_value:
        API_KEYS[client_name] = {"key": key_value, "tier": tier}
TIERS = CONFIG.get("tiers", {
    "1": {"name": "Priority", "description": "GB10 only", "preferred_nodes": ["ai-node-GB10"], "fallback_nodes": [], "timeout_seconds": 60},
    "2": {"name": "Standard", "description": "GB10 first, node-01 fallback", "preferred_nodes": ["ai-node-GB10", "ai-node-01"], "fallback_nodes": ["ai-node-01"], "timeout_seconds": 30},
    "3": {"name": "Economy", "description": "node-01 first, GB10 fallback", "preferred_nodes": ["ai-node-01", "ai-node-GB10"], "fallback_nodes": ["ai-node-GB10"], "timeout_seconds": 30}
})
ALERT_CONFIG = CONFIG.get("alerts", {
    "enabled": False,
    "email": "",
    "smtp_host": "smtp.office365.com",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "failed_checks_before_alert": 2
})

# Alert state tracking per node
_node_fail_counts = {}   # node_name -> consecutive fail count
_node_alert_sent = {}    # node_name -> bool (alert already sent for this outage)

def _send_alert_email_sync(node_name: str, node_url: str):
    """Synchronous email send — called via executor to avoid blocking event loop."""
    if not ALERT_CONFIG.get("enabled") or not ALERT_CONFIG.get("email"):
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[AI Gateway] Node DOWN: {node_name}"
    msg["From"] = ALERT_CONFIG["smtp_user"]
    msg["To"] = ALERT_CONFIG["email"]
    body = f"""
AI Gateway Alert
================
Node: {node_name}
URL: {node_url}
Status: UNREACHABLE
Time: {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")}

The AI Gateway health check has failed {ALERT_CONFIG["failed_checks_before_alert"]} times in a row for this node.
Traffic is being rerouted to other available nodes.

Dashboard: http://192.168.44.9:8000/dashboard
"""
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP(ALERT_CONFIG["smtp_host"], ALERT_CONFIG["smtp_port"]) as server:
        server.starttls()
        server.login(ALERT_CONFIG["smtp_user"], ALERT_CONFIG["smtp_password"])
        server.sendmail(ALERT_CONFIG["smtp_user"], ALERT_CONFIG["email"], msg.as_string())

async def send_alert_email(node_name: str, node_url: str):
    """Send email alert when a node goes down — non-blocking."""
    if not ALERT_CONFIG.get("enabled") or not ALERT_CONFIG.get("email"):
        return
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send_alert_email_sync, node_name, node_url)
        log.info(f"Alert email sent for node {node_name}")
    except Exception as e:
        log.error(f"Failed to send alert email: {e}")

def _send_recovery_email_sync(node_name: str, node_url: str):
    """Synchronous recovery email — called via executor."""
    if not ALERT_CONFIG.get("enabled") or not ALERT_CONFIG.get("email"):
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[AI Gateway] Node RECOVERED: {node_name}"
    msg["From"] = ALERT_CONFIG["smtp_user"]
    msg["To"] = ALERT_CONFIG["email"]
    body = f"""
AI Gateway Recovery Notice
==========================
Node: {node_name}
URL: {node_url}
Status: ONLINE
Time: {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")}

The node is back online and receiving traffic normally.

Dashboard: http://192.168.44.9:8000/dashboard
"""
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP(ALERT_CONFIG["smtp_host"], ALERT_CONFIG["smtp_port"]) as server:
        server.starttls()
        server.login(ALERT_CONFIG["smtp_user"], ALERT_CONFIG["smtp_password"])
        server.sendmail(ALERT_CONFIG["smtp_user"], ALERT_CONFIG["email"], msg.as_string())

async def send_recovery_email(node_name: str, node_url: str):
    """Send recovery email — non-blocking."""
    if not ALERT_CONFIG.get("enabled") or not ALERT_CONFIG.get("email"):
        return
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send_recovery_email_sync, node_name, node_url)
        log.info(f"Recovery email sent for node {node_name}")
    except Exception as e:
        log.error(f"Failed to send recovery email: {e}")

def get_key_info(key_str: str) -> dict:
    for name, info in API_KEYS.items():
        stored_key = info["key"] if isinstance(info, dict) else info
        if key_str == stored_key:
            tier = info.get("tier", 2) if isinstance(info, dict) else 2
            return {"name": name, "tier": tier}
    return None

def get_tier_config(tier: int) -> dict:
    tier_cfg = TIERS.get(str(tier), TIERS.get("2", {}))
    return {
        "preferred_nodes": tier_cfg.get("preferred_nodes", []),
        "fallback_nodes": tier_cfg.get("fallback_nodes", []),
        "timeout_seconds": tier_cfg.get("timeout_seconds", 30),
        "name": tier_cfg.get("name", "Standard")
    }

# --- Node state ---
# Each node gets a semaphore to limit concurrent requests
class NodeState:
    CIRCUIT_BREAK_THRESHOLD = 5   # consecutive errors before cutting the node
    CIRCUIT_RESET_AFTER     = 60  # seconds before trying again

    def __init__(self, node_cfg):
        self.name = node_cfg["name"]
        self.url = node_cfg["url"]
        self.enabled = node_cfg["enabled"]
        self.max_concurrent = node_cfg["max_concurrent_requests"]
        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        self.active_requests = 0
        self.healthy = True
        self.last_checked = None
        self.available_models = []
        self.total_requests = 0
        self.total_errors = 0
        self.total_duration_ms = 0
        self.active_models = {}
        self.total_ram_gb = node_cfg.get("total_ram_gb", 0)
        self.loaded_models = []
        self.vram_used_gb = 0.0
        self.queued_requests = 0  # waiting for semaphore
        self.consecutive_errors = 0  # circuit breaker counter
        self.circuit_open_until = 0.0  # epoch time when circuit resets

nodes: list[NodeState] = []
_last_health_check: float = 0.0
_health_check_interval: float = 30.0

# --- Recent connections tracker ---
# { ip: { client, last_seen, last_model, request_count } }
_recent_connections: dict = {}
_connection_window: float = 600.0  # 10 minutes

# --- App startup ---
# 10MB max request body — prevents memory exhaustion from oversized payloads
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_bytes: int = 10 * 1024 * 1024):
        super().__init__(app)
        self.max_bytes = max_bytes
    async def dispatch(self, request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > self.max_bytes:
                return StarletteResponse("Request body too large", status_code=413)
        return await call_next(request)

app = FastAPI(title="AI Gateway")
app.add_middleware(MaxBodySizeMiddleware, max_bytes=10 * 1024 * 1024)
app.include_router(jobs_api.router)
app.include_router(admin_keys_api.router)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")
GATEWAY_START_TIME = datetime.utcnow().isoformat()

# HTML template cache — loaded once at startup
_html_cache = {}

@app.on_event("startup")
async def startup():
    global _html_cache
    init_db()
    _init_jwt_secret()
    for node_cfg in CONFIG["nodes"]:
        if node_cfg["enabled"]:
            nodes.append(NodeState(node_cfg))
    log.info(f"Gateway started with {len(nodes)} nodes")
    # --- v2 async job store: recover orphans, start prune loop ---
    global KEY_STORE
    KEY_STORE = keystore.KeyStore()
    gwauth.init(KEY_STORE, require_api_key=REQUIRE_API_KEY, legacy_lookup=get_key_info)
    admin_keys_api.init(KEY_STORE, require_auth=require_auth)
    jobs_api.init(CONFIG, check_api_key=check_api_key, free_slots_fn=_count_free_slots)
    await jobs_api.start_prune_task()
    admission.init(CONFIG)
    # --- v2 async dispatcher: runs queued jobs across the fleet ---
    dispatcher.init(CONFIG, jobs_api.STORE, nodes, _free_node_for, DEFAULT_MODEL)
    await dispatcher.start()
    # Cache HTML templates in memory
    for tmpl in ["dashboard.html", "live.html"]:
        try:
            with open(f"/app/templates/{tmpl}", "r") as f:
                _html_cache[tmpl] = f.read()
            log.info(f"Cached template: {tmpl}")
        except Exception as e:
            log.warning(f"Could not cache {tmpl}: {e}")
    # Do an initial health check on all nodes
    await check_all_nodes(force=True)
    # Start the priority scheduler dispatcher
    global _dispatcher_task
    if SCHED.get("enabled", False):
        _dispatcher_task = asyncio.create_task(dispatcher_loop())
        log.info(f"Priority scheduler ENABLED - max_queue_depth={SCHED.get('max_queue_depth')}")
    else:
        log.info("Priority scheduler disabled")

# --- Health checking ---
async def check_node(node: NodeState):
    was_healthy = node.healthy
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{node.url}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                node.available_models = [m["name"] for m in data.get("models", [])]
                node.healthy = True
            else:
                node.healthy = False
                node.available_models = []
            # Fetch loaded models and VRAM usage from /api/ps
            try:
                ps_resp = await client.get(f"{node.url}/api/ps")
                if ps_resp.status_code == 200:
                    ps_data = ps_resp.json()
                    node.loaded_models = ps_data.get("models", [])
                    node.vram_used_gb = sum(
                        m.get("size_vram", 0) for m in node.loaded_models
                    ) / 1024**3
                else:
                    node.loaded_models = []
                    node.vram_used_gb = 0.0
            except:
                node.loaded_models = []
                node.vram_used_gb = 0.0
    except Exception as e:
        log.warning(f"Node {node.name} health check failed: {e}")
        node.healthy = False
        node.available_models = []
        node.loaded_models = []
        node.vram_used_gb = 0.0
    node.last_checked = datetime.utcnow().isoformat()

    # Alert logic
    threshold = ALERT_CONFIG.get("failed_checks_before_alert", 2)
    if not node.healthy:
        _node_fail_counts[node.name] = _node_fail_counts.get(node.name, 0) + 1
        if _node_fail_counts[node.name] >= threshold and not _node_alert_sent.get(node.name):
            log.warning(f"Node {node.name} DOWN after {threshold} failed checks - sending alert")
            _node_alert_sent[node.name] = True
            asyncio.create_task(send_alert_email(node.name, node.url))
    else:
        if _node_alert_sent.get(node.name):
            log.info(f"Node {node.name} recovered - sending recovery alert")
            _node_alert_sent[node.name] = False
            asyncio.create_task(send_recovery_email(node.name, node.url))
        _node_fail_counts[node.name] = 0

async def check_all_nodes(force: bool = False):
    global _last_health_check
    now = time.time()
    if not force and (now - _last_health_check) < _health_check_interval:
        return
    await asyncio.gather(*[check_node(n) for n in nodes])
    _last_health_check = time.time()

# --- Node selection ---
def get_node_by_name(name: str) -> NodeState | None:
    for node in nodes:
        if node.name == name:
            return node
    return None

_rr_counter = 0

def best_available(node_names: list, model: str) -> NodeState | None:
    """
    From a list of node names, return the least-loaded node that:
    - is enabled, healthy, not circuit-broken
    - can run the requested model
    - has at least one free slot
    Load is measured as active/max ratio so a 1/2 node beats a 2/3 node.
    """
    candidates = []
    for name in node_names:
        node = get_node_by_name(name)
        if not node or not node.enabled or not node.healthy:
            continue
        if node.circuit_open_until > time.time():
            log.warning(f"Circuit open for {node.name} - skipping")
            continue
        if model and model not in node.available_models:
            continue
        if node.active_requests < node.max_concurrent:
            candidates.append(node)
    if not candidates:
        return None
    # Find the lowest load ratio among candidates
    def load_ratio(n):
        return n.active_requests / n.max_concurrent if n.max_concurrent else 999
    min_ratio = min(load_ratio(n) for n in candidates)
    # All candidates tied at the lowest ratio (e.g. all idle) get round-robined
    tied = [n for n in candidates if abs(load_ratio(n) - min_ratio) < 1e-9]
    if len(tied) == 1:
        return tied[0]
    global _rr_counter
    _rr_counter += 1
    # Stable order by name so rotation is deterministic across calls
    tied.sort(key=lambda n: n.name)
    return tied[_rr_counter % len(tied)]

# --- Priority scheduler with aging ---
SCHED = CONFIG.get("scheduler", {
    "enabled": False, "max_queue_depth": 60, "dispatch_interval_ms": 100,
    "tiers": {"1": {"base_weight": 100, "aging_rate_per_sec": 1.0, "timeout_seconds": 60},
              "2": {"base_weight": 50, "aging_rate_per_sec": 1.5, "timeout_seconds": 60},
              "3": {"base_weight": 10, "aging_rate_per_sec": 3.0, "timeout_seconds": 90}}
})

class QueueTicket:
    """One waiting request in the priority queue."""
    __slots__ = ("tier", "model", "client", "arrived", "event",
                 "assigned_node", "rejected", "reject_reason")

    def __init__(self, tier: int, model: str, client: str):
        self.tier = tier
        self.model = model
        self.client = client
        self.arrived = time.time()
        self.event = asyncio.Event()
        self.assigned_node = None
        self.rejected = False
        self.reject_reason = None

    def tier_cfg(self):
        return SCHED.get("tiers", {}).get(str(self.tier), {})

    def score(self, now=None):
        """
        Aging as a starvation backstop, not routine fairness.
        A request holds its base weight until it has waited longer than
        aging_grace_seconds; only then does it start climbing. This keeps
        tier ordering strict during normal operation while guaranteeing
        that a request stuck far longer than expected eventually wins.
        """
        now = now or time.time()
        cfg = self.tier_cfg()
        base = cfg.get("base_weight", 50)
        rate = cfg.get("aging_rate_per_sec", 1.0)
        grace = cfg.get("aging_grace_seconds", 0)
        waited = now - self.arrived
        if waited <= grace:
            return base
        return base + (waited - grace) * rate

    def timeout_seconds(self):
        return self.tier_cfg().get("timeout_seconds", 60)

    def waited(self, now=None):
        return (now or time.time()) - self.arrived


_queue: list = []           # list[QueueTicket]
_queue_lock = asyncio.Lock()
_dispatcher_task = None

# Dispatch instrumentation - rolling record of who won each slot and why
from collections import deque as _deque
_dispatch_log = _deque(maxlen=2000)
_dispatch_counts = {}   # tier -> dispatched count
_reject_counts = {}     # tier -> timeout/reject count


def scheduler_stats():
    now = time.time()
    by_tier = {}
    for t in _queue:
        k = str(t.tier)
        if k not in by_tier:
            by_tier[k] = {"waiting": 0, "oldest_wait_s": 0, "top_score": 0}
        by_tier[k]["waiting"] += 1
        by_tier[k]["oldest_wait_s"] = max(by_tier[k]["oldest_wait_s"], round(t.waited(now), 1))
        by_tier[k]["top_score"] = max(by_tier[k]["top_score"], round(t.score(now), 1))
    return {
        "enabled": SCHED.get("enabled", False),
        "queue_depth": len(_queue),
        "max_queue_depth": SCHED.get("max_queue_depth", 60),
        "by_tier": by_tier,
        "config": SCHED,
    }


def _free_node_for(model: str):
    """Return a node with a free slot that can serve this model, else None."""
    target = model or DEFAULT_MODEL
    candidates = []
    for node in nodes:
        if not node.enabled or not node.healthy:
            continue
        if node.circuit_open_until > time.time():
            continue
        if target and target not in node.available_models:
            continue
        if node.active_requests < node.max_concurrent:
            candidates.append(node)
    if not candidates:
        return None
    min_ratio = min(n.active_requests / n.max_concurrent for n in candidates)
    tied = [n for n in candidates
            if abs(n.active_requests / n.max_concurrent - min_ratio) < 1e-9]
    if len(tied) == 1:
        return tied[0]
    global _rr_counter
    _rr_counter += 1
    tied.sort(key=lambda n: n.name)
    return tied[_rr_counter % len(tied)]


async def dispatcher_loop():
    """Assign free slots to the highest-scoring waiting tickets."""
    interval = SCHED.get("dispatch_interval_ms", 100) / 1000.0
    while True:
        try:
            await asyncio.sleep(interval)
            async with _queue_lock:
                if not _queue:
                    continue
                now = time.time()
                # Expire timed-out tickets
                expired = [t for t in _queue if t.waited(now) >= t.timeout_seconds()]
                for t in expired:
                    t.rejected = True
                    t.reject_reason = f"Queue timeout after {round(t.waited(now))}s"
                    _reject_counts[str(t.tier)] = _reject_counts.get(str(t.tier), 0) + 1
                    t.event.set()
                    _queue.remove(t)
                # Dispatch by score, highest first
                ordered = sorted(_queue, key=lambda t: t.score(now), reverse=True)
                for ticket in ordered:
                    node = _free_node_for(ticket.model)
                    if not node:
                        break
                    # snapshot of what this ticket beat
                    waiting_by_tier = {}
                    for t in _queue:
                        waiting_by_tier[str(t.tier)] = waiting_by_tier.get(str(t.tier), 0) + 1
                    runner_up = None
                    for t in ordered:
                        if t is not ticket:
                            runner_up = {"tier": t.tier, "score": round(t.score(now), 1),
                                         "waited": round(t.waited(now), 1)}
                            break
                    _dispatch_log.append({
                        "ts": round(now, 2),
                        "tier": ticket.tier,
                        "client": ticket.client,
                        "waited_s": round(ticket.waited(now), 2),
                        "score": round(ticket.score(now), 1),
                        "queue_depth": len(_queue),
                        "waiting_by_tier": waiting_by_tier,
                        "runner_up": runner_up,
                        "node": node.name,
                    })
                    _dispatch_counts[str(ticket.tier)] = _dispatch_counts.get(str(ticket.tier), 0) + 1
                    node.active_requests += 1   # reserve immediately
                    ticket.assigned_node = node
                    ticket.event.set()
                    _queue.remove(ticket)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"dispatcher error: {e}")


async def acquire_slot(model: str, client: str, tier: int):
    """
    Wait in the priority queue for a node slot.
    Returns (node, wait_seconds). Raises HTTPException on reject/timeout.
    The returned node already has active_requests incremented (reserved);
    the caller MUST decrement it when done.
    """
    if not SCHED.get("enabled", False):
        # Scheduler off - fall back to immediate pick
        node = _free_node_for(model)
        if not node:
            raise HTTPException(status_code=503, detail="No available nodes")
        node.active_requests += 1
        return node, 0.0

    async with _queue_lock:
        if len(_queue) >= SCHED.get("max_queue_depth", 60):
            raise HTTPException(
                status_code=503,
                detail=f"Queue full ({len(_queue)} waiting) - try again shortly")
        ticket = QueueTicket(tier, model, client)
        _queue.append(ticket)

    await ticket.event.wait()

    if ticket.rejected:
        raise HTTPException(status_code=503, detail=ticket.reject_reason or "Rejected")
    return ticket.assigned_node, ticket.waited()


def pick_node(model: str = None, client: str = "anonymous", tier: int = 2) -> NodeState | None:
    target_model = model or DEFAULT_MODEL
    tier_cfg = get_tier_config(tier)
    preferred = tier_cfg["preferred_nodes"]
    fallback  = tier_cfg["fallback_nodes"]

    # Phase 1 - load-balance across all preferred nodes that can run this model
    # If GB10 and node-01 both have slots, pick whichever is less loaded
    node = best_available(preferred, target_model)
    if node:
        return node

    # Phase 2 - preferred nodes are all full; try fallback nodes
    node = best_available(fallback, target_model)
    if node:
        return node

    # Phase 3 - everything in tier is full; try any healthy node as last resort
    all_node_names = [n.name for n in nodes]
    node = best_available(all_node_names, target_model)
    if node:
        return node

    return None

# --- API key check ---
def check_api_key(request: Request) -> tuple:
    """Returns (client_name, tier). Delegates to gwauth (keystore-backed auth);
    falls back to the legacy env/config lookup only while keys.db is empty
    (see gwauth.py's empty-keystore safety net)."""
    try:
        rec = gwauth.authenticate(request.headers)
    except gwauth.AuthError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return rec["client"], rec["tier"]

# --- Core proxy functions ---
async def proxy_request(node: NodeState, method: str, path: str, body: dict = None, original_request: Request = None, model_key: str = None, skip_reserve: bool = False):
    url = f"{node.url}{path}"
    headers = {"Content-Type": "application/json"}
    if not skip_reserve:
        node.active_requests += 1
    if model_key:
        node.active_models[model_key] = node.active_models.get(model_key, 0) + 1
    t0 = time.time()
    success = False
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            else:
                resp = await client.post(url, json=body, headers=headers)
        success = True
        node.consecutive_errors = 0  # reset circuit breaker on success
        return resp.json()
    except Exception:
        node.total_errors += 1
        node.consecutive_errors += 1
        if node.consecutive_errors >= NodeState.CIRCUIT_BREAK_THRESHOLD:
            node.circuit_open_until = time.time() + NodeState.CIRCUIT_RESET_AFTER
            log.warning(f"Circuit breaker OPEN for {node.name} after {node.consecutive_errors} errors — pausing for {NodeState.CIRCUIT_RESET_AFTER}s")
        raise
    finally:
        if not skip_reserve:
            node.active_requests -= 1
        if model_key:
            node.active_models[model_key] = max(0, node.active_models.get(model_key, 1) - 1)
            if node.active_models.get(model_key, 0) == 0:
                node.active_models.pop(model_key, None)
        node.total_requests += 1
        node.total_duration_ms += round((time.time() - t0) * 1000)

async def proxy_stream(node: NodeState, path: str, body: dict):
    """Stream response from Ollama node back to client."""
    url = f"{node.url}{path}"
    headers = {"Content-Type": "application/json"}
    node.active_requests += 1
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
    finally:
        node.active_requests -= 1

# --- Routes ---

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    try:
        require_auth(request)
    except HTTPException:
        return RedirectResponse(url="/gateway/login")
    html = _html_cache.get("dashboard.html")
    if not html:
        with open("/app/templates/dashboard.html", "r") as f:
            html = f.read()
    return HTMLResponse(content=html)

@app.get("/live", response_class=HTMLResponse)
async def live_view(request: Request):
    html = _html_cache.get("live.html")
    if not html:
        with open("/app/templates/live.html", "r") as f:
            html = f.read()
    return HTMLResponse(content=html)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    error = request.query_params.get("error", "")
    error_msg = ""
    if error == "1":
        error_msg = "Incorrect email or password."
    elif error == "2":
        error_msg = "Session expired. Please sign in again."
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>AI Gateway Login</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:"Segoe UI",system-ui,sans-serif;background:#f4f3f0;min-height:100vh;display:flex;flex-direction:column}}
    .header{{background:#fff;border-bottom:3px solid #B5282A;height:56px;display:flex;align-items:center;padding:0 24px;gap:14px}}
    .logo-g{{font-size:30px;color:#B5282A;font-family:Georgia,serif;font-style:italic;font-weight:700;line-height:1}}
    .header-divider{{width:1px;height:32px;background:#e0dfd8}}
    .header-sup{{font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:#999}}
    .header-title{{font-size:15px;font-weight:600;color:#1a1a18}}
    .body{{flex:1;display:flex;align-items:center;justify-content:center}}
    .card{{background:#fff;border:1px solid #e0dfd8;border-radius:8px;width:360px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
    .card-header{{background:#B5282A;padding:20px 24px;text-align:center}}
    .card-header h2{{color:#fff;font-size:18px;font-weight:600;margin-bottom:4px}}
    .card-header p{{color:rgba(255,255,255,.8);font-size:13px}}
    .card-body{{padding:24px}}
    .field{{margin-bottom:16px}}
    .field label{{display:block;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#666;margin-bottom:6px}}
    .field input{{width:100%;padding:8px 12px;border:1px solid #e0dfd8;border-radius:4px;font-size:14px;background:#f7f7f5;color:#1a1a18}}
    .field input:focus{{outline:2px solid #B5282A;border-color:#B5282A;background:#fff}}
    .btn{{width:100%;padding:10px;background:#B5282A;color:#fff;border:none;border-radius:4px;font-size:14px;font-weight:600;cursor:pointer;margin-top:4px}}
    .btn:hover{{background:#8a1e1f}}
    .error{{color:#B5282A;font-size:12px;margin-top:12px;text-align:center}}
    .footer{{text-align:center;padding:16px;font-size:11px;color:#999}}
  </style>
</head>
<body>
<header class="header">
  <div class="logo-g">G</div>
  <div class="header-divider"></div>
  <div>
    <div class="header-sup">City of Georgetown, Texas &middot; Information Technology</div>
    <div class="header-title">AI Gateway Dashboard</div>
  </div>
</header>
<div class="body">
  <div class="card">
    <div class="card-header">
      <h2>Sign In</h2>
      <p>Enter your credentials to continue</p>
    </div>
    <div class="card-body">
      <form method="post" action="/gateway/login">
        <div class="field"><label>Email</label><input type="email" name="email" autofocus placeholder="you@georgetowntexas.gov"></div>
        <div class="field"><label>Password</label><input type="password" name="password" placeholder="Password"></div>
        <button type="submit" class="btn">Sign In</button>
        {"<div class='error'>" + error_msg + "</div>" if error_msg else ""}
      </form>
    </div>
    <div class="footer">IT Department use only</div>
  </div>
</div>
</body>
</html>""")

@app.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    user = get_user_by_email(email)
    if not user or not verify_password(password, user["hashed_password"]):
        return RedirectResponse(url="/gateway/login?error=1", status_code=302)
    token = create_jwt(user["email"], user["role"])
    response = RedirectResponse(url="/gateway/dashboard", status_code=302)
    response.set_cookie(key="dashboard_token", value=token, httponly=True, max_age=JWT_EXPIRE_SECONDS, samesite="strict")
    return response

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/gateway/login")
    response.delete_cookie("dashboard_token")
    return response

# --- User management endpoints ---

@app.get("/admin/users")
async def admin_list_users(request: Request):
    require_auth(request, role="admin")
    return JSONResponse(list_users())

@app.post("/admin/users/create")
async def admin_create_user(request: Request):
    require_auth(request, role="admin")
    body = await request.json()
    name = body.get("name", "").strip()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "").strip()
    role = body.get("role", "viewer")
    if not name or not email or not password:
        raise HTTPException(status_code=400, detail="name, email, and password are required")
    if role not in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="role must be admin or viewer")
    if get_user_by_email(email):
        raise HTTPException(status_code=409, detail="Email already exists")
    db_create_user(name, email, password, role)
    return JSONResponse({"ok": True})

@app.post("/admin/users/delete")
async def admin_delete_user(request: Request):
    require_auth(request, role="admin")
    body = await request.json()
    user_id = body.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="id is required")
    # Prevent deleting the last admin
    all_users = list_users()
    target = next((u for u in all_users if u["id"] == user_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    admins = [u for u in all_users if u["role"] == "admin"]
    if target["role"] == "admin" and len(admins) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last admin account")
    db_delete_user(user_id)
    return JSONResponse({"ok": True})

@app.post("/admin/users/change-password")
async def admin_change_password(request: Request):
    payload = require_auth(request)
    body = await request.json()
    current = body.get("current_password", "")
    new_pw = body.get("new_password", "").strip()
    if not new_pw or len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    user = get_user_by_email(payload["sub"])
    if not user or not verify_password(current, user["hashed_password"]):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    db_change_password(payload["sub"], new_pw)
    return JSONResponse({"ok": True})

@app.post("/admin/users/reset-password")
async def admin_reset_password(request: Request):
    require_auth(request, role="admin")
    body = await request.json()
    email = body.get("email", "").strip().lower()
    new_pw = body.get("new_password", "").strip()
    if not email or not new_pw or len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="email and new_password (min 8 chars) required")
    if not get_user_by_email(email):
        raise HTTPException(status_code=404, detail="User not found")
    db_change_password(email, new_pw)
    return JSONResponse({"ok": True})

@app.get("/admin/me")
async def admin_me(request: Request):
    payload = require_auth(request)
    user = get_user_by_email(payload["sub"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return JSONResponse({
        "name": user["name"],
        "email": user["email"],
        "role": user["role"]
    })

@app.get("/health")
async def health():
    return {"healthy": True, "nodes": len(nodes)}

@app.get("/")
async def root():
    return {"status": "ok", "service": "ai-gateway", "default_model": DEFAULT_MODEL}

# -- Ollama-compatible --

@app.get("/api/tags")
async def api_tags(request: Request):
    check_api_key(request)
    await check_all_nodes(force=True)
    # Merge model lists from all healthy nodes, deduplicate
    seen = set()
    merged = []
    for node in nodes:
        if node.healthy:
            for model_name in node.available_models:
                if model_name not in seen:
                    seen.add(model_name)
                    merged.append({"name": model_name, "model": model_name})
    return {"models": merged}

@app.post("/api/chat")
async def api_chat(request: Request):
    from starlette.responses import StreamingResponse as StarletteStreaming
    client, tier = check_api_key(request)
    body = await request.json()
    model = body.get("model") or DEFAULT_MODEL
    body["model"] = model
    want_stream = body.get("stream", False)
    await check_all_nodes(force=False)
    source_ip = request.client.host if request.client else None
    t_queue_start = time.time()
    try:
        node, wait_secs = await acquire_slot(model, client, tier)
    except HTTPException as e:
        elapsed = time.time() - t_queue_start
        log_request("/api/chat", model, None, None, elapsed, 0, False, 503,
                    str(client), str(e.detail), source_ip)
        raise

    if want_stream:
        async def generate():
            t_start = time.time()
            wait_time = wait_secs
            log.info(f"Routing /api/chat (stream) model={model} client={client} ip={source_ip} -> {node.name} (waited {wait_time:.1f}s)")
            try:
                body["stream"] = True
                url = f"{node.url}/api/chat"
                headers = {"Content-Type": "application/json"}
                async with httpx.AsyncClient(timeout=300.0) as http_client:
                    async with http_client.stream("POST", url, json=body, headers=headers) as resp:
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                yield chunk
                duration = time.time() - t_start
                log_request("/api/chat", model, model, node.name, wait_time, duration, True, 200, client, None, source_ip)
            except Exception as e:
                duration = time.time() - t_start
                log_request("/api/chat", model, model, node.name, wait_time, duration, False, 500, client, str(e), source_ip)
                raise
            finally:
                node.active_requests -= 1
        return StarletteStreaming(generate(), media_type="application/x-ndjson", headers={"x-node": node.name})

    else:
        body["stream"] = False
        t_start = time.time()
        wait_time = wait_secs
        if wait_time > 0.5:
            log.info(f"Queued {wait_time:.1f}s for slot on {node.name}")
        log.info(f"Routing /api/chat model={model} client={client} ip={source_ip} -> {node.name}")
        try:
            result = await proxy_request(node, "POST", "/api/chat", body, request, model, skip_reserve=True)
            duration = time.time() - t_start
            log_request("/api/chat", model, model, node.name, wait_time, duration, True, 200, client, None, source_ip)
        except Exception as e:
            duration = time.time() - t_start
            log_request("/api/chat", model, model, node.name, wait_time, duration, False, 500, client, str(e), source_ip)
            raise
        finally:
            node.active_requests -= 1
        return JSONResponse(content=result, headers={"x-node": node.name})

@app.post("/api/embed")
async def api_embed(request: Request):
    client, tier = check_api_key(request)
    body = await request.json()
    model = body.get("model") or "mxbai-embed-large:335m"
    body["model"] = model
    node = pick_node(model, client, tier)
    if not node:
        raise HTTPException(status_code=503, detail="No available nodes for this request")
    async with node.semaphore:
        log.info(f"Routing /api/embed model={model} client={client} tier={tier} -> {node.name}")
        result = await proxy_request(node, "POST", "/api/embed", body, request)
    return JSONResponse(content=result)

@app.post("/api/embeddings")
async def api_embeddings(request: Request):
    client, tier = check_api_key(request)
    body = await request.json()
    model = body.get("model") or "nomic-embed-text:latest"
    body["model"] = model
    node = pick_node(model, client, tier)
    if not node:
        raise HTTPException(status_code=503, detail="No available nodes for this request")
    async with node.semaphore:
        log.info(f"Routing /api/embeddings model={model} client={client} tier={tier} -> {node.name}")
        result = await proxy_request(node, "POST", "/api/embeddings", body, request)
    return JSONResponse(content=result)

# -- OpenAI-compatible --

@app.get("/v1/models")
async def v1_models(request: Request):
    check_api_key(request)
    await check_all_nodes(force=True)
    seen = set()
    model_list = []
    for node in nodes:
        if node.healthy:
            for model_name in node.available_models:
                if model_name not in seen:
                    seen.add(model_name)
                    model_list.append({
                        "id": model_name,
                        "object": "model",
                        "owned_by": "local",
                        "node": node.name
                    })
    return {"object": "list", "data": model_list}

@app.post("/v1/chat/completions")
async def v1_chat_completions(request: Request):
    from starlette.responses import StreamingResponse as StarletteStreaming
    client, tier = check_api_key(request)
    body = await request.json()
    model = body.get("model") or DEFAULT_MODEL
    want_stream = body.get("stream", False)
    ollama_body = {
        "model": model,
        "messages": body.get("messages", []),
        "stream": want_stream,
        "options": {}
    }
    if "temperature" in body:
        ollama_body["options"]["temperature"] = body["temperature"]
    node = pick_node(model, client, tier)
    if not node:
        raise HTTPException(status_code=503, detail="No available nodes for this request")
    t_wait_start = time.time()

    if want_stream:
        # Streaming path: translate Ollama SSE -> OpenAI SSE on the fly
        async def generate():
            node.active_requests += 1
            t_start = time.time()
            wait_time = t_start - t_wait_start
            try:
                url = f"{node.url}/api/chat"
                headers = {"Content-Type": "application/json"}
                async with httpx.AsyncClient(timeout=300.0) as http_client:
                    async with http_client.stream("POST", url, json=ollama_body, headers=headers) as resp:
                        async for line in resp.aiter_lines():
                            if not line.strip():
                                continue
                            try:
                                chunk = json.loads(line)
                            except Exception:
                                continue
                            msg = chunk.get("message", {})
                            content = msg.get("content", "")
                            done = chunk.get("done", False)
                            openai_chunk = {
                                "id": "chatcmpl-gateway",
                                "object": "chat.completion.chunk",
                                "model": model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"role": "assistant", "content": content} if content else {},
                                    "finish_reason": "stop" if done else None
                                }]
                            }
                            yield f"data: {json.dumps(openai_chunk)}\n\n"
                            if done:
                                break
                yield "data: [DONE]\n\n"
                duration = time.time() - t_start
                log_request("/v1/chat/completions", model, model, node.name, wait_time, duration, True, 200, client)
            except Exception as e:
                duration = time.time() - t_start
                log_request("/v1/chat/completions", model, model, node.name, wait_time, duration, False, 500, client, str(e))
                raise
            finally:
                node.active_requests -= 1
        return StarletteStreaming(generate(), media_type="text/event-stream")

    else:
        # Non-streaming path: unchanged
        async with node.semaphore:
            t_start = time.time()
            wait_time = t_start - t_wait_start
            log.info(f"Routing /v1/chat/completions model={model} client={client} tier={tier} -> {node.name}")
            try:
                result = await proxy_request(node, "POST", "/api/chat", ollama_body, request)
                duration = time.time() - t_start
                log_request("/v1/chat/completions", model, model, node.name, wait_time, duration, True, 200, client)
            except Exception as e:
                duration = time.time() - t_start
                log_request("/v1/chat/completions", model, model, node.name, wait_time, duration, False, 500, client, str(e))
                raise
        message = result.get("message", {})
        return {
            "id": "chatcmpl-gateway",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": "stop"
            }],
            "usage": result.get("usage", {})
        }

# -- Admin endpoints --

def _calc_p95_latency() -> int:
    """Calculate p95 response time from last 200 log entries."""
    log_file = "/app/logs/requests.log"
    if not os.path.exists(log_file):
        return 0
    try:
        with open(log_file, "r") as f:
            lines = f.readlines()
        durations = []
        for line in lines[-200:]:
            try:
                r = json.loads(line.strip())
                if r.get("success") and r.get("duration_ms"):
                    durations.append(r["duration_ms"])
            except:
                continue
        if not durations:
            return 0
        durations.sort()
        idx = int(len(durations) * 0.95)
        return durations[min(idx, len(durations) - 1)]
    except:
        return 0

@app.get("/admin/status")
async def admin_status(request: Request):
    await check_all_nodes(force=False)
    total_reqs = sum(n.total_requests for n in nodes)
    total_errs = sum(n.total_errors for n in nodes)
    err_rate = round(total_errs / total_reqs * 100, 1) if total_reqs else 0
    return {
        "gateway": "online",
        "default_model": DEFAULT_MODEL,
        "require_api_key": REQUIRE_API_KEY,
        "node_count": len(nodes),
        "healthy_nodes": sum(1 for n in nodes if n.healthy),
        "timestamp": datetime.utcnow().isoformat(),
        "started_at": GATEWAY_START_TIME,
        "p95_latency_ms": _calc_p95_latency(),
        "total_requests": total_reqs,
        "error_rate_pct": err_rate
    }

@app.get("/admin/live-nodes")
async def admin_live_nodes(request: Request):
    """
    Public (no require_auth), same precedent as /admin/status above -- this is
    a reduced, safe-to-expose subset of /admin/nodes below (which needs the
    admin JWT), built for external tools like the load tester that have no
    business holding admin credentials but have a real use for live per-node
    load (e.g. a live workload panel during a run). Deliberately omits
    anything sensitive /admin/nodes exposes: no raw node URLs, no loaded-model
    detail, no RAM/VRAM figures -- just enough to answer "how loaded is each
    node right now".
    """
    await check_all_nodes(force=False)
    return {
        "nodes": [
            {
                "name": n.name,
                "healthy": n.healthy,
                "active_requests": n.active_requests,
                "max_concurrent": n.max_concurrent,
                "load_ratio": round(n.active_requests / n.max_concurrent, 2) if n.max_concurrent else 0,
            }
            for n in nodes
        ]
    }

@app.get("/admin/nodes")
async def admin_nodes(request: Request):
    require_auth(request)
    await check_all_nodes(force=True)
    return {
        "nodes": [
            {
                "name": n.name,
                "url": n.url,
                "enabled": n.enabled,
                "healthy": n.healthy,
                "active_requests": n.active_requests,
                "max_concurrent": n.max_concurrent,
                "available_models": n.available_models,
                "last_checked": n.last_checked,
                "total_ram_gb": n.total_ram_gb,
                "vram_used_gb": round(n.vram_used_gb, 1),
                "ram_pct": round(n.vram_used_gb / n.total_ram_gb * 100) if n.total_ram_gb else 0,
                "queued_requests": n.queued_requests,
                "loaded_models": [
                    {
                        "name": m.get("name"),
                        "size_gb": round(m.get("size_vram", 0) / 1024**3, 1),
                        "expires_at": m.get("expires_at")
                    }
                    for m in n.loaded_models
                ]
            }
            for n in nodes
        ]
    }


@app.get("/api/version")
async def api_version():
    return {"version": "0.1.0-gateway"}

@app.get("/api/ps")
async def api_ps():
    # Return currently active models across all nodes
    running = []
    for node in nodes:
        if node.healthy and node.active_requests > 0:
            running.append({
                "name": node.name,
                "model": DEFAULT_MODEL,
                "node": node.name
            })
    return {"models": running}


@app.post("/admin/config/default-model")
async def set_default_model(request: Request):
    require_auth(request, role="admin")
    body = await request.json()
    model = body.get("model", "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model name required")
    global DEFAULT_MODEL
    DEFAULT_MODEL = model
    CONFIG["default_model"] = model
    await save_config()
    log.info(f"Default model changed to {model}")
    return {"success": True, "default_model": DEFAULT_MODEL}

@app.post("/admin/config/node-toggle")
async def toggle_node(request: Request):
    require_auth(request, role="admin")
    body = await request.json()
    node_name = body.get("name", "").strip()
    enabled = body.get("enabled", True)
    # Update in-memory node state
    for node in nodes:
        if node.name == node_name:
            node.enabled = enabled
            break
    else:
        raise HTTPException(status_code=404, detail=f"Node {node_name} not found")
    # Update config file
    for node_cfg in CONFIG["nodes"]:
        if node_cfg["name"] == node_name:
            node_cfg["enabled"] = enabled
            break
    await save_config()
    log.info(f"Node {node_name} {'enabled' if enabled else 'disabled'}")
    return {"success": True, "name": node_name, "enabled": enabled}



@app.get("/admin/scheduler/dispatch")
async def admin_scheduler_dispatch(request: Request, limit: int = 200):
    """Recent dispatch decisions - who won each slot, at what score, and what they beat."""
    require_auth(request)
    recent = list(_dispatch_log)[-limit:]
    # Aggregate: per-tier dispatch stats
    agg = {}
    for r in recent:
        k = str(r["tier"])
        a = agg.setdefault(k, {"dispatched": 0, "total_wait": 0.0,
                               "max_wait": 0.0, "total_score": 0.0})
        a["dispatched"] += 1
        a["total_wait"] += r["waited_s"]
        a["max_wait"] = max(a["max_wait"], r["waited_s"])
        a["total_score"] += r["score"]
    for k, a in agg.items():
        n = max(a["dispatched"], 1)
        a["avg_wait_s"] = round(a["total_wait"] / n, 2)
        a["avg_score"] = round(a["total_score"] / n, 1)
        del a["total_wait"]; del a["total_score"]
    return {
        "window_size": len(recent),
        "per_tier": agg,
        "lifetime_dispatched": _dispatch_counts,
        "lifetime_rejected": _reject_counts,
        "recent": recent[-40:],
    }


@app.get("/admin/scheduler")
async def admin_scheduler(request: Request):
    """Public - live scheduler/queue state and its configuration."""
    return scheduler_stats()


@app.get("/admin/clients")
async def admin_clients(request: Request):
    """Public endpoint — returns only client names and tiers for live view legend."""
    result = []
    for name, info in API_KEYS.items():
        tier = info.get("tier", 2) if isinstance(info, dict) else 2
        result.append({"name": name, "tier": tier})
    return {"clients": result}

@app.get("/admin/config/keys")
async def get_api_keys(request: Request):
    require_auth(request, role="admin")
    keys = []
    for name, info in API_KEYS.items():
        if isinstance(info, dict):
            key = info["key"]
            tier = info.get("tier", 2)
        else:
            key = info
            tier = 2
        keys.append({
            "name": name,
            "key_preview": key[:8] + "..." + key[-4:],
            "tier": tier,
            "tier_name": TIERS.get(str(tier), {}).get("name", "Standard")
        })
    return {"keys": keys, "require_api_key": REQUIRE_API_KEY, "tiers": TIERS}

@app.post("/admin/config/keys/add")
async def add_api_key(request: Request):
    import secrets
    require_auth(request, role="admin")
    body = await request.json()
    name = body.get("name", "").strip()
    tier = int(body.get("tier", 2))
    if not name:
        raise HTTPException(status_code=400, detail="Client name required")
    if name in API_KEYS:
        raise HTTPException(status_code=400, detail=f"Key for {name} already exists")
    new_key = secrets.token_urlsafe(32)
    API_KEYS[name] = {"key": new_key, "tier": tier}
    CONFIG["api_keys"][name] = {"key": new_key, "tier": tier}
    await save_config()
    log.info(f"API key added for client: {name} tier: {tier}")
    return {"success": True, "name": name, "key": new_key, "tier": tier}

@app.post("/admin/config/keys/tier")
async def update_key_tier(request: Request):
    require_auth(request, role="admin")
    body = await request.json()
    name = body.get("name", "").strip()
    tier = int(body.get("tier", 2))
    if name not in API_KEYS:
        raise HTTPException(status_code=404, detail=f"No key found for {name}")
    if isinstance(API_KEYS[name], dict):
        API_KEYS[name]["tier"] = tier
    else:
        API_KEYS[name] = {"key": API_KEYS[name], "tier": tier}
    CONFIG["api_keys"] = API_KEYS
    await save_config()
    log.info(f"Tier updated for client: {name} -> tier {tier}")
    return {"success": True, "name": name, "tier": tier}

@app.post("/admin/config/keys/revoke")
async def revoke_api_key(request: Request):
    body = await request.json()
    name = require_auth(request, role="admin")
    body = await request.json()
    name = body.get("name", "").strip()
    if name not in API_KEYS:
        raise HTTPException(status_code=404, detail=f"No key found for {name}")
    del API_KEYS[name]
    del CONFIG["api_keys"][name]
    await save_config()
    log.info(f"API key revoked for client: {name}")
    return {"success": True, "name": name}

@app.post("/admin/config/require-api-key")
async def set_require_api_key(request: Request):
    require_auth(request, role="admin")
    global REQUIRE_API_KEY
    body = await request.json()
    value = body.get("enabled", False)
    REQUIRE_API_KEY = value
    CONFIG["require_api_key"] = value
    # gwauth caches REQUIRE_API_KEY at init() time; keep it in sync with the
    # live toggle above or this dashboard switch silently stops doing anything.
    gwauth.init(KEY_STORE, require_api_key=REQUIRE_API_KEY, legacy_lookup=get_key_info)
    await save_config()
    log.info(f"require_api_key set to {value}")
    return {"success": True, "require_api_key": REQUIRE_API_KEY}


@app.get("/admin/stats")
async def admin_stats(request: Request, window: str = "all"):
    cache_key = f"stats_{window}"
    cached = get_cached(cache_key)
    if cached:
        return cached
    log_file = "/app/logs/requests.log"
    if not os.path.exists(log_file):
        return {"clients": [], "window": window}

    now = datetime.utcnow()
    cutoff = None
    if window == "24h":
        cutoff = now.timestamp() - 86400
    elif window == "7d":
        cutoff = now.timestamp() - 604800

    clients = {}

    with open(log_file, "r") as f:
        for line in f:
            try:
                r = json.loads(line.strip())
            except:
                continue

            # Apply time filter
            if cutoff:
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp()
                    if ts < cutoff:
                        continue
                except:
                    continue

            client = r.get("client", "anonymous")
            if isinstance(client, list): client = client[0] if client else "anonymous"
            if client not in clients:
                clients[client] = {
                    "client": client,
                    "total_requests": 0,
                    "successful_requests": 0,
                    "failed_requests": 0,
                    "total_duration_ms": 0,
                    "total_wait_ms": 0,
                    "models_used": set(),
                    "nodes_used": set(),
                    "last_seen": None
                }

            c = clients[client]
            c["total_requests"] += 1
            c["total_duration_ms"] += r.get("duration_ms", 0)
            c["total_wait_ms"] += r.get("wait_ms", 0)

            if r.get("success"):
                c["successful_requests"] += 1
            else:
                c["failed_requests"] += 1

            if r.get("model_used"):
                c["models_used"].add(r["model_used"])
            if r.get("node"):
                c["nodes_used"].add(r["node"])

            ts = r.get("timestamp")
            if ts and (c["last_seen"] is None or ts > c["last_seen"]):
                c["last_seen"] = ts

    # Serialize sets and compute averages
    result = []
    for c in clients.values():
        total = c["total_requests"]
        result.append({
            "client": c["client"],
            "total_requests": total,
            "successful_requests": c["successful_requests"],
            "failed_requests": c["failed_requests"],
            "avg_duration_ms": round(c["total_duration_ms"] / total) if total else 0,
            "avg_wait_ms": round(c["total_wait_ms"] / total) if total else 0,
            "total_duration_ms": c["total_duration_ms"],
            "models_used": sorted(list(c["models_used"])),
            "nodes_used": sorted(list(c["nodes_used"])),
            "last_seen": c["last_seen"]
        })

    result.sort(key=lambda x: x["total_requests"], reverse=True)
    response = {"clients": result, "window": window}
    set_cached(f"stats_{window}", response)
    return response


@app.get("/admin/alerts")
async def get_alerts(request: Request):
    require_auth(request)
    return {
        "config": {
            "enabled": ALERT_CONFIG.get("enabled", False),
            "email": ALERT_CONFIG.get("email", ""),
            "smtp_host": ALERT_CONFIG.get("smtp_host", "smtp.office365.com"),
            "smtp_port": ALERT_CONFIG.get("smtp_port", 587),
            "smtp_user": ALERT_CONFIG.get("smtp_user", ""),
            "failed_checks_before_alert": ALERT_CONFIG.get("failed_checks_before_alert", 2)
        },
        "node_status": {
            name: {
                "fail_count": _node_fail_counts.get(name, 0),
                "alert_sent": _node_alert_sent.get(name, False)
            }
            for name in [n.name for n in nodes]
        }
    }

@app.post("/admin/alerts/config")
async def update_alert_config(request: Request):
    require_auth(request, role="admin")
    global ALERT_CONFIG
    body = await request.json()
    ALERT_CONFIG.update({
        "enabled": body.get("enabled", False),
        "email": body.get("email", ""),
        "smtp_host": body.get("smtp_host", "smtp.office365.com"),
        "smtp_port": int(body.get("smtp_port", 587)),
        "smtp_user": body.get("smtp_user", ""),
        "failed_checks_before_alert": int(body.get("failed_checks_before_alert", 2))
    })
    if "smtp_password" in body and body["smtp_password"]:
        ALERT_CONFIG["smtp_password"] = body["smtp_password"]
    CONFIG["alerts"] = ALERT_CONFIG
    await save_config()
    log.info(f"Alert config updated: enabled={ALERT_CONFIG['enabled']} email={ALERT_CONFIG['email']}")
    return {"success": True, "config": {k:v for k,v in ALERT_CONFIG.items() if k != "smtp_password"}}

@app.post("/admin/alerts/test")
async def test_alert(request: Request):
    require_auth(request, role="admin")
    if not ALERT_CONFIG.get("enabled") or not ALERT_CONFIG.get("email"):
        raise HTTPException(status_code=400, detail="Alerts not configured or not enabled")
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "[AI Gateway] Test Alert"
        msg["From"] = ALERT_CONFIG["smtp_user"]
        msg["To"] = ALERT_CONFIG["email"]
        msg.attach(MIMEText("This is a test alert from the AI Gateway. Alerts are working correctly.", "plain"))
        with smtplib.SMTP(ALERT_CONFIG["smtp_host"], ALERT_CONFIG["smtp_port"]) as server:
            server.starttls()
            server.login(ALERT_CONFIG["smtp_user"], ALERT_CONFIG["smtp_password"])
            server.sendmail(ALERT_CONFIG["smtp_user"], ALERT_CONFIG["email"], msg.as_string())
        return {"success": True, "message": f"Test email sent to {ALERT_CONFIG['email']}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send test email: {str(e)}")

@app.get("/admin/workload")
async def admin_workload(request: Request):
    cached = get_cached("workload")
    if cached:
        return cached
    log_file = "/app/logs/requests.log"
    now = datetime.utcnow().timestamp()
    windows = {"4h": 14400, "6h": 21600, "12h": 43200, "24h": 86400}

    # Initialize per-node stats
    node_stats = {}
    for node in nodes:
        node_stats[node.name] = {
            "name": node.name,
            "healthy": node.healthy,
            "enabled": node.enabled,
            "active_requests": node.active_requests,
            "max_concurrent": node.max_concurrent,
            "load_pct": round(node.active_requests / node.max_concurrent * 100) if node.max_concurrent else 0,
            "total_requests": node.total_requests,
            "total_errors": node.total_errors,
            "avg_duration_ms": round(node.total_duration_ms / node.total_requests) if node.total_requests else 0,
            "active_models": dict(node.active_models),
            "total_ram_gb": node.total_ram_gb,
            "vram_used_gb": round(node.vram_used_gb, 1),
            "ram_pct": round(node.vram_used_gb / node.total_ram_gb * 100) if node.total_ram_gb else 0,
            "queued_requests": node.queued_requests,
            "loaded_models": [
                {
                    "name": m.get("name"),
                    "size_gb": round(m.get("size_vram", 0) / 1024**3, 1),
                }
                for m in node.loaded_models
            ],
            "windows": {w: {"total": 0, "models": {}} for w in windows}
        }

    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            for line in f:
                try:
                    r = json.loads(line.strip())
                except:
                    continue
                node_name = r.get("node")
                if not node_name or node_name not in node_stats:
                    continue
                model = r.get("model_used", "unknown")
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp()
                except:
                    continue
                age = now - ts
                for w, seconds in windows.items():
                    if age <= seconds:
                        node_stats[node_name]["windows"][w]["total"] += 1
                        wm = node_stats[node_name]["windows"][w]["models"]
                        wm[model] = wm.get(model, 0) + 1

    response = {"nodes": list(node_stats.values()), "timestamp": datetime.utcnow().isoformat()}
    set_cached("workload", response)
    return response

@app.get("/admin/routing")
async def admin_routing(request: Request):
    require_auth(request)
    await check_all_nodes()

    # Build model routing map — for each model, which nodes can serve it and current preference
    model_map = {}
    for node in nodes:
        if not node.healthy or not node.enabled:
            continue
        for model in node.available_models:
            if model not in model_map:
                model_map[model] = []
            load_pct = round(node.active_requests / node.max_concurrent * 100) if node.max_concurrent else 0
            model_map[model].append({
                "node": node.name,
                "url": node.url,
                "active_requests": node.active_requests,
                "max_concurrent": node.max_concurrent,
                "load_pct": load_pct,
                "would_be_selected": False
            })

    # Mark which node would currently be selected for each model
    for model, candidates in model_map.items():
        if candidates:
            best = min(candidates, key=lambda n: n["active_requests"] / n["max_concurrent"] if n["max_concurrent"] else 0)
            best["would_be_selected"] = True

    # Build client routing history from recent connections
    client_history = []
    for ip, conn in _recent_connections.items():
        client_key = conn.get("client", ip)
        client_history.append({
            "client": conn["client"],
            "source_ip": conn["source_ip"],
            "last_model": conn["last_model"],
            "last_path": conn["last_path"],
            "last_seen": conn["last_seen"]
        })

    # Node summary
    node_summary = []
    for node in nodes:
        node_summary.append({
            "name": node.name,
            "enabled": node.enabled,
            "healthy": node.healthy,
            "active_requests": node.active_requests,
            "max_concurrent": node.max_concurrent,
            "load_pct": round(node.active_requests / node.max_concurrent * 100) if node.max_concurrent else 0,
            "model_count": len(node.available_models)
        })

    return {
        "algorithm": "lowest-load-ratio",
        "algorithm_description": "Requests are routed to the healthy, enabled node with the requested model that has the lowest ratio of active requests to max concurrent requests. If tied, the first matching node in config order wins.",
        "default_model": DEFAULT_MODEL,
        "nodes": node_summary,
        "model_routing": [
            {"model": m, "candidates": c}
            for m, c in sorted(model_map.items())
        ],
        "recent_client_routing": sorted(client_history, key=lambda x: x["last_seen"] or "", reverse=True)
    }

@app.get("/admin/topology")
async def admin_topology(request: Request, window: int = 600):
    await check_all_nodes()
    now = datetime.utcnow().timestamp()

    # Prune in-memory connections older than window
    stale = [ip for ip, c in _recent_connections.items()
             if (now - datetime.fromisoformat(c["last_seen"]).timestamp()) > window]
    for ip in stale:
        del _recent_connections[ip]

    # For longer windows, also pull from log file
    log_clients = {}
    if window > _connection_window and os.path.exists("/app/logs/requests.log"):
        cutoff = now - window
        with open("/app/logs/requests.log", "r") as f:
            for line in f:
                try:
                    r = json.loads(line.strip())
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp()
                    if ts < cutoff:
                        continue
                    ip = r.get("source_ip")
                    client = r.get("client", "anonymous")
                    if isinstance(client, list):
                        client = client[0] if client else "anonymous"
                    # Key by client name so shared IPs don't collapse into one entry
                    if client not in log_clients or r["timestamp"] > log_clients[client]["last_seen"]:
                        log_clients[client] = {
                            "client": client,
                            "source_ip": ip,
                            "last_seen": r["timestamp"],
                            "last_model": r.get("model_used"),
                            "last_node": r.get("node"),
                            "last_path": r.get("path")
                        }
                except:
                    continue

    # Merge in-memory and log clients, keyed by client name
    merged = dict(log_clients)
    for ip, conn in _recent_connections.items():
        client_key = conn.get("client", ip)
        if client_key not in merged or conn["last_seen"] > merged[client_key]["last_seen"]:
            merged[client_key] = conn

    return {
        "gateway": {
            "name": "AI Gateway",
            "ip": "192.168.44.9",
            "port": 8000,
            "status": "online"
        },
        "ai_nodes": [
            {
                "name": n.name,
                "ip": n.url.split("//")[1].split(":")[0],
                "url": n.url,
                "healthy": n.healthy,
                "enabled": n.enabled,
                "active_requests": n.active_requests,
                "max_concurrent": n.max_concurrent,
                "model_count": len(n.available_models),
                "queued_requests": n.queued_requests
            }
            for n in nodes
        ],
        "clients": list(merged.values())
    }


@app.get("/admin/stats/node/{node_name}")
async def admin_stats_node(node_name: str, request: Request):
    require_auth(request)
    log_file = "/app/logs/requests.log"
    if not os.path.exists(log_file):
        return {"node": node_name, "windows": {}}
    now = datetime.utcnow().timestamp()
    windows = {"4h": 14400, "6h": 21600, "12h": 43200, "24h": 86400}
    result = {}
    for win_name, win_secs in windows.items():
        cutoff = now - win_secs
        breakdown = {}  # "client|model" -> count
        total = 0
        with open(log_file, "r") as f:
            for line in f:
                try:
                    r = json.loads(line.strip())
                except:
                    continue
                if r.get("node") != node_name:
                    continue
                if not r.get("success"):
                    continue
                try:
                    ts = datetime.fromisoformat(r["timestamp"]).timestamp()
                    if ts < cutoff:
                        continue
                except:
                    continue
                client = r.get("client", "anonymous")
                if isinstance(client, list): client = client[0] if client else "anonymous"
                model = (r.get("model_used") or "unknown").split(":")[0]
                key = client + "|" + model
                breakdown[key] = breakdown.get(key, 0) + 1
                total += 1
        # Convert to sorted list
        rows = []
        for key, count in sorted(breakdown.items(), key=lambda x: -x[1]):
            parts = key.split("|", 1)
            rows.append({"client": parts[0], "model": parts[1] if len(parts)>1 else "unknown", "count": count})
        result[win_name] = {"total": total, "rows": rows}
    return {"node": node_name, "windows": result}

@app.get("/admin/logs")
async def admin_logs(request: Request, limit: int = 50):
    require_auth(request)
    log_file = "/app/logs/requests.log"
    if not os.path.exists(log_file):
        return {"logs": []}
    with open(log_file, "r") as f:
        lines = f.readlines()
    # Return most recent entries first
    recent = lines[-limit:][::-1]
    parsed = []
    for line in recent:
        try:
            parsed.append(json.loads(line.strip()))
        except:
            pass
    return {"logs": parsed}

@app.get("/admin/models")
async def admin_models(request: Request):
    require_auth(request)
    await check_all_nodes(force=True)
    model_map = {}
    for node in nodes:
        if node.healthy:
            for m in node.available_models:
                if m not in model_map:
                    model_map[m] = []
                model_map[m].append(node.name)
    return {
        "default_model": DEFAULT_MODEL,
        "models": [
            {"name": m, "nodes": nodes_list}
            for m, nodes_list in sorted(model_map.items())
        ]
    }
