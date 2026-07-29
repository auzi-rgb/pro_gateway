"""
dispatcher.py — async job execution engine for the AI Gateway (v2).

This is the piece that makes queued jobs actually run. It is a SEPARATE loop
from the chat scheduler (acquire_slot / dispatcher_loop in main.py). The two
cooperate through exactly one shared thing: node.active_requests, the per-node
slot counter. They do NOT share a lock.

Why separate (Option 2, agreed with the operator): the live /api/chat path is
proven and carries production traffic. Keeping this loop independent means a
fault here cannot take down chat, and any fault is localized to new code. The
eventual unified single-scheduler end state is a later refinement — the slot
coordination that makes it possible is already shared here.

THE RACE, AND HOW IT IS AVOIDED
    Both loops pick a free node and reserve it by incrementing active_requests.
    asyncio is single-threaded, so the only way the two loops interleave is at
    an `await`. Therefore the check ("is this node free?") and the reserve
    ("active_requests += 1") MUST happen in the same synchronous stretch with no
    await between them. _reserve_slot() below does exactly that. After the
    increment, awaits are safe — the slot is already ours.

ORDERING (this step)
    interactive first, then deadline by earliest deadline_ms, then throughput by
    weight (desc) then arrival (asc). Aging backstop on throughput only, so a
    throughput job that has waited too long cannot be starved forever.

WHAT THIS STEP DOES NOT DO
    - No admission control (jobs are never rejected for expected slowness here;
      that is step 2d). Every queued job eventually runs.
    - No preemption. Ordering changes who goes next; it never interrupts a job
      already running. Consistent with the design.
    - Cancel of a RUNNING job records intent in the store but does not kill the
      in-flight HTTP stream; the result is simply discarded when it returns.
"""

import time
import json
import asyncio
import logging

import httpx

from jobstore import (
    JobStore,
    STATUS_QUEUED, STATUS_RUNNING,
)
import admission

log = logging.getLogger("gateway")

# --- Weight ordering. Higher number = goes first within a class. ------------
WEIGHT_RANK = {"critical": 3, "high": 2, "normal": 1, "low": 0}
CLASS_RANK = {"interactive": 3, "deadline": 2, "throughput": 1}

# --- Module state, injected by init() from main.py --------------------------
STORE: JobStore = None
_nodes = None            # main.py's `nodes` list (live NodeState objects)
_free_node_for = None    # main.py's node selector (does filtering + round-robin)
_default_model = None
_cfg = {}

_loop_task = None
_running = False

# Tunables (from config "dispatcher" block, with defaults)
_interval_seconds = 0.1          # how often the loop wakes
_aging_grace_seconds = 30.0      # throughput waits this long at base priority
_aging_rate_per_sec = 10.0       # then climbs this fast
_node_http_timeout = 300.0       # per-inference HTTP timeout to a node

# In-flight accounting: job_id -> node object, so we can release the slot
# exactly once when the job finishes, matching the chat path's reserve/finally.
_inflight = {}


def init(config, store, nodes, free_node_for, default_model):
    """
    Wire the dispatcher to main.py's live objects. Called once at startup.
    Dependency injection (not import) avoids a circular import with main.
    """
    global STORE, _nodes, _free_node_for, _default_model, _cfg
    global _interval_seconds, _aging_grace_seconds, _aging_rate_per_sec
    global _node_http_timeout
    STORE = store
    _nodes = nodes
    _free_node_for = free_node_for
    _default_model = default_model
    _cfg = config.get("dispatcher", {})
    _interval_seconds = _cfg.get("interval_ms", 100) / 1000.0
    _aging_grace_seconds = _cfg.get("aging_grace_seconds", 30.0)
    _aging_rate_per_sec = _cfg.get("aging_rate_per_sec", 10.0)
    _node_http_timeout = _cfg.get("node_http_timeout_seconds", 300.0)
    log.info(f"dispatcher: configured interval={_interval_seconds}s "
             f"aging_grace={_aging_grace_seconds}s rate={_aging_rate_per_sec}")


async def start():
    """Start the dispatch loop. Call from main.py startup after init()."""
    global _loop_task, _running
    if _loop_task is None:
        _running = True
        _loop_task = asyncio.create_task(_dispatch_loop())
        log.info("dispatcher: loop started")


async def stop():
    """Stop the loop (for clean shutdown / tests)."""
    global _loop_task, _running
    _running = False
    if _loop_task:
        _loop_task.cancel()
        try:
            await _loop_task
        except asyncio.CancelledError:
            pass
        _loop_task = None


# --- Ordering ---------------------------------------------------------------

def _sort_key(job, now):
    """
    Build a sort key so that sorted(reverse=True) yields dispatch order:
    interactive > deadline > throughput; within deadline, earliest first;
    within throughput, higher weight then earlier arrival, with an aging bump.

    Returned tuple is (class_rank, urgency, weight_rank, recency). Larger sorts
    first under reverse=True.
    """
    cls = job["job_class"]
    crank = CLASS_RANK.get(cls, 0)
    submitted = job["submitted_at"]
    waited = now - submitted

    if cls == "deadline":
        # Earliest deadline first. deadline_ms is an absolute-ish budget from
        # submission; smaller remaining time => more urgent => sorts first.
        # Represent urgency as negative time-remaining so "less remaining"
        # is "larger" under reverse sort.
        deadline_ms = job.get("deadline_ms") or 0
        deadline_at = submitted + (deadline_ms / 1000.0)
        remaining = deadline_at - now
        urgency = -remaining          # less remaining -> larger
        return (crank, urgency, 0, 0)

    if cls == "interactive":
        # Interactive is small-volume and always first; order within it by
        # arrival (older first). No aging needed — it should never wait.
        return (crank, 0, WEIGHT_RANK.get(job["weight"], 1), waited)

    # throughput: weight, then arrival, plus aging backstop.
    wrank = WEIGHT_RANK.get(job["weight"], 1)
    if waited > _aging_grace_seconds:
        aged = (waited - _aging_grace_seconds) * _aging_rate_per_sec
    else:
        aged = 0.0
    # recency component: older arrival sorts first (larger waited). aging adds
    # to it so a long-waiting low-weight job eventually outranks fresh high.
    return (crank, 0, wrank, waited + aged)


def _order_queued(jobs, now):
    return sorted(jobs, key=lambda j: _sort_key(j, now), reverse=True)


# --- Slot reservation (the race-critical part) ------------------------------

def _reserve_slot(model):
    """
    Find a free node for `model` and reserve it, ATOMICALLY with respect to the
    chat scheduler. There is NO await between _free_node_for() (which checks
    active_requests < max) and the increment, so the chat loop cannot grab the
    same slot in between. Returns the node, or None if none free.

    Must be called from sync context (it is — the loop calls it before awaiting).
    """
    node = _free_node_for(model)
    if node is None:
        return None
    node.active_requests += 1   # reserve; released in _run_job's finally
    return node


def _release_slot(node):
    if node is not None:
        node.active_requests -= 1


# --- The loop ---------------------------------------------------------------

async def _dispatch_loop():
    while _running:
        try:
            await asyncio.sleep(_interval_seconds)
            queued = STORE.list_queued()
            if not queued:
                continue
            now = time.time()
            ordered = _order_queued(queued, now)

            for job in ordered:
                model = job.get("model") or _default_model
                # Reserve BEFORE any await (race-critical).
                node = _reserve_slot(model)
                if node is None:
                    # No free slot for this job's model right now. Because a
                    # different job might target a different model that IS free,
                    # keep scanning rather than breaking outright.
                    continue

                # Claim the job in the store (guarded: only if still queued).
                claimed = STORE.mark_running(job["id"], node.name)
                if not claimed:
                    # Someone cancelled or it moved since list_queued(); release
                    # the slot we reserved and move on.
                    _release_slot(node)
                    continue

                # Hand off execution; do NOT await it here or we serialize the
                # loop. Fire-and-track so the loop keeps assigning other slots.
                _inflight[job["id"]] = node
                asyncio.create_task(_run_job(job, node))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"dispatcher loop error: {e}")


async def _run_job(job, node):
    """
    Replay the job payload to the node, write result/error back to the store,
    release the slot. Runs as its own task so many jobs run concurrently across
    the fleet.
    """
    job_id = job["id"]
    payload = dict(job["payload"])  # stored verbatim; replay as-is
    payload["stream"] = False       # async jobs collect the full result
    # Decide endpoint: chat payloads have "messages", generate payloads "prompt".
    if "messages" in payload:
        path = "/api/chat"
    else:
        path = "/api/generate"
    url = f"{node.url}{path}"
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=_node_http_timeout) as client:
            resp = await client.post(url, json=payload,
                                     headers={"Content-Type": "application/json"})
        data = resp.json()
        # Ollama can return {"error": ...} with HTTP 200 — treat as failure.
        if isinstance(data, dict) and data.get("error"):
            STORE.mark_failed(job_id, f"node error: {data['error']}")
            log.info(f"dispatcher: job {job_id} FAILED (node error) "
                     f"on {node.name}")
        else:
            ok = STORE.mark_succeeded(job_id, data)
            if ok:
                dt = round(time.time() - t0, 2)
                # Feed real output into the throughput tracker (self-healing
                # admission control). Ollama reports eval_count (tokens) and
                # eval_duration (nanoseconds). This is the live measurement that
                # keeps wait estimates honest as the fleet/model changes.
                if isinstance(data, dict):
                    tokens = data.get("eval_count")
                    eval_ns = data.get("eval_duration")
                    if tokens and eval_ns:
                        admission.record_completion(tokens, eval_ns / 1e9)
                log.info(f"dispatcher: job {job_id} succeeded on "
                         f"{node.name} in {dt}s")
            else:
                # Job left 'running' state under us (e.g. cancelled). Result is
                # discarded — the caller gave up. Nothing to store.
                log.info(f"dispatcher: job {job_id} finished but was no longer "
                         f"running (likely cancelled); result discarded")
    except Exception as e:
        STORE.mark_failed(job_id, str(e))
        log.info(f"dispatcher: job {job_id} FAILED on {node.name}: {e}")
    finally:
        _release_slot(node)
        _inflight.pop(job_id, None)


# --- Introspection (for the dashboard / debugging) --------------------------

def status():
    return {
        "running": _running,
        "inflight": len(_inflight),
        "interval_seconds": _interval_seconds,
        "aging_grace_seconds": _aging_grace_seconds,
        "aging_rate_per_sec": _aging_rate_per_sec,
    }
