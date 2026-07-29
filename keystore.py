"""
keystore.py — hashed API key store for the AI Gateway (v2).

Replaces the old dual-source key loading (env vars + config.json), which made
revoke unreliable (env keys resurrected on restart) and allowed drift. Keys live
in ONE place — a SQLite DB — and only their HASHES are stored. The plaintext
secret is shown once at creation and never recoverable. Revoke deletes the row,
so a revoked key can never authenticate again. Single source, final revoke.

CLASS IS A PROPERTY OF THE WORK, NOT THE KEY.
    A single app can do multiple kinds of work (HireDesk grades resumes in batch
    = throughput, AND chats about them = interactive). So a key does not carry
    one class; it carries the SET of classes it is ALLOWED to use. The request
    declares which class it is (the async path already reads class from the body;
    the sync /api/chat path implies 'interactive'). The gateway checks the
    declared class is in the key's allowed set. A single-behavior app just has a
    one-element set, so nothing changes for it.

    Integrity note: because the request declares its own class, apps must declare
    correctly. That is enforced by policy, not code — the app integration guide
    gives mixed apps a spec/prompt for classifying each call. Lying cannot gain
    cross-client priority anyway: WEIGHT (who is asking) is fixed on the key and
    is NOT request-declarable, so a mis-declared class can only reorder the app's
    OWN work, which hurts only itself.

WEIGHT is single per key: "who is asking" does not change with the kind of work.
CAPABILITY is single per key: e.g. HireDesk = 'high' (the 24b) for everything it
    does. (Revisit per-request capability only if/when the 24b topology split is
    real and an app needs different models for different calls.)

WHY SHA-256 AND NOT BCRYPT (the gateway uses bcrypt for user passwords):
    Passwords are low-entropy, so they need a SLOW hash to resist brute force.
    API keys are 256-bit random tokens — uncrackable regardless of hash speed —
    and are verified on EVERY request, on the hot path. Bcrypt here would add
    ~100ms per call for zero benefit. A fast cryptographic hash (SHA-256) is both
    secure for high-entropy input and fast. Standard for API keys. Do not change.

Storage only: knows nothing about HTTP or endpoints. Auth logic lives in
main.py's check_api_key, using the helpers at the bottom of this module.
"""

import time
import hashlib
import secrets
import sqlite3
from contextlib import contextmanager

# --- Vocab (kept in sync with the v2 design) --------------------------------
VALID_CLASSES = ("interactive", "deadline", "throughput")
VALID_WEIGHTS = ("critical", "high", "normal", "low")

# Which transport each class uses. Enforced strictly by the auth layer.
#   interactive         -> synchronous /api/chat
#   deadline/throughput -> asynchronous /api/jobs
SYNC_CLASSES = ("interactive",)
ASYNC_CLASSES = ("deadline", "throughput")

KEY_BYTES = 32  # secrets.token_urlsafe(32) -> ~43 chars, 256 bits entropy


def hash_key(secret: str) -> str:
    """SHA-256 hex of the raw key. Deterministic so lookup is a direct match."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _normalize_classes(classes):
    """
    Accept a list/tuple/set/comma-string of class names; return a validated,
    de-duplicated, sorted tuple. Raises ValueError on any invalid element or an
    empty set.
    """
    if isinstance(classes, str):
        items = [c.strip() for c in classes.split(",") if c.strip()]
    else:
        items = list(classes)
    if not items:
        raise ValueError("a key must allow at least one class")
    out = []
    for c in items:
        if c not in VALID_CLASSES:
            raise ValueError(
                f"invalid class '{c}'; must be one of {VALID_CLASSES}")
        if c not in out:
            out.append(c)
    return tuple(sorted(out))


class KeyStore:
    def __init__(self, db_path="/app/keys.db"):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self.get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    client          TEXT NOT NULL UNIQUE,
                    key_hash        TEXT NOT NULL UNIQUE,
                    key_prefix      TEXT NOT NULL,
                    allowed_classes TEXT NOT NULL,
                    weight          TEXT NOT NULL,
                    capability      TEXT,
                    created_at      REAL NOT NULL,
                    last_used_at    REAL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_keys_hash ON api_keys(key_hash)")

    @staticmethod
    def _validate_weight(weight):
        if weight not in VALID_WEIGHTS:
            raise ValueError(
                f"invalid weight '{weight}'; must be one of {VALID_WEIGHTS}")

    @staticmethod
    def _row_to_dict(row):
        if row is None:
            return None
        d = dict(row)
        d.pop("key_hash", None)
        if "allowed_classes" in d and isinstance(d["allowed_classes"], str):
            d["allowed_classes"] = tuple(
                c for c in d["allowed_classes"].split(",") if c)
        return d

    def create(self, client, allowed_classes, weight, capability=None):
        """
        Create a key for `client`. allowed_classes is a list/set/comma-string of
        one or more classes. Returns the PLAINTEXT secret exactly once — not
        stored, not recoverable. Only the hash is kept.
        """
        classes = _normalize_classes(allowed_classes)
        self._validate_weight(weight)
        secret = secrets.token_urlsafe(KEY_BYTES)
        khash = hash_key(secret)
        prefix = secret[:8]
        now = time.time()
        try:
            with self.get_conn() as conn:
                conn.execute(
                    """INSERT INTO api_keys
                       (client, key_hash, key_prefix, allowed_classes, weight,
                        capability, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (client, khash, prefix, ",".join(classes), weight,
                     capability, now),
                )
        except sqlite3.IntegrityError as e:
            raise ValueError(f"client '{client}' already has a key") from e
        return secret

    def lookup(self, secret, touch=True):
        """
        Given a presented bearer token, return the key record dict or None.
        Called on every request; direct hash-index lookup. allowed_classes comes
        back as a tuple.
        """
        khash = hash_key(secret)
        with self.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ?", (khash,)
            ).fetchone()
            if row is None:
                return None
            if touch:
                conn.execute(
                    "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                    (time.time(), row["id"]),
                )
        return self._row_to_dict(row)

    def list_keys(self):
        with self.get_conn() as conn:
            rows = conn.execute(
                """SELECT client, key_prefix, allowed_classes, weight, capability,
                          created_at, last_used_at
                   FROM api_keys ORDER BY client"""
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["allowed_classes"] = tuple(
                c for c in d["allowed_classes"].split(",") if c)
            result.append(d)
        return result

    def revoke(self, client):
        with self.get_conn() as conn:
            cur = conn.execute("DELETE FROM api_keys WHERE client = ?", (client,))
            return cur.rowcount > 0

    def update(self, client, allowed_classes=None, weight=None, capability=None):
        """Change class-set / weight / capability without reissuing the secret."""
        sets, vals = [], []
        if allowed_classes is not None:
            classes = _normalize_classes(allowed_classes)
            sets.append("allowed_classes = ?"); vals.append(",".join(classes))
        if weight is not None:
            self._validate_weight(weight)
            sets.append("weight = ?"); vals.append(weight)
        if capability is not None:
            sets.append("capability = ?"); vals.append(capability)
        if not sets:
            return False
        vals.append(client)
        with self.get_conn() as conn:
            cur = conn.execute(
                f"UPDATE api_keys SET {', '.join(sets)} WHERE client = ?", vals)
            return cur.rowcount > 0

    def count(self):
        with self.get_conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM api_keys").fetchone()["n"]


# --- Auth helpers (used by check_api_key) -----------------------------------

def class_allowed(key_record, requested_class):
    """Is the request's declared class permitted for this key?"""
    return requested_class in (key_record.get("allowed_classes") or ())


def endpoint_class_ok(key_record, requested_class, is_async_endpoint):
    """
    Full strict check for a request:
      1. the declared class must be in the key's allowed set, AND
      2. the class must match the endpoint's transport
         (interactive -> sync /api/chat; deadline/throughput -> async /api/jobs).
    Returns (ok: bool, reason: str|None).
    """
    if requested_class not in VALID_CLASSES:
        return False, f"unknown class '{requested_class}'"
    if not class_allowed(key_record, requested_class):
        return (False,
                f"key not permitted for class '{requested_class}' "
                f"(allowed: {','.join(key_record.get('allowed_classes') or ())})")
    if is_async_endpoint and requested_class not in ASYNC_CLASSES:
        return False, f"class '{requested_class}' must use the sync endpoint"
    if not is_async_endpoint and requested_class not in SYNC_CLASSES:
        return False, f"class '{requested_class}' must use the async endpoint"
    return True, None
