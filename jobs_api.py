"""
jobs_api.py — async job endpoints for the AI Gateway (v2), as a mountable
APIRouter.

This is the transport half of the async path. It accepts jobs, stores them,
and lets clients poll and cancel. It does NOT execute anything yet — there is
no dispatcher at this stage, so a submitted job sits in 'queued' until the
dispatcher (next build step) is added. That separation is deliberate: it lets
submit / poll / cancel / restart-persistence be proven in isolation before any
inference logic exists.

Wiring into main.py is three lines (see the deploy notes):

    import jobs_api
    jobs_api.init(CONFIG)                 # in startup(), after CONFIG is loaded
    app.include_router(jobs_api.router)   # after `app = FastAPI(...)`

Auth reuses main.py's check_api_key via dependency injection set in init(),
so this module does not duplicate key logic. Class/weight come from the request
body at this stage (client-stated); key-based enforcement of the class/endpoint
pairing is a later step, once keys carry a class.
"""

import asyncio
import logging

from fastapi import APIRouter, Request, HTTPException

from jobstore import JobStore
import admission
from keystore import VALID_CLASSES, VALID_WEIGHTS

log = logging.getLogger("gateway")

router = APIRouter()


def _admission_check(job_class, payload, deadline_ms):
    """
    Ask admission control whether to accept this job. Returns None to admit, or
    an HTTPException detail string to reject. Uses live queue + free-slot state.
    """
    queued = STORE.list_queued()
    free = _free_slots_fn() if _free_slots_fn else 0
    result = admission.decide_for_submit(
        job_class, payload, queued, free, deadline_ms=deadline_ms)
    return None if result.admit else result.reason

# --- Vocab (kept in sync with 03-gateway-v2-design.md) -----------------------
# VALID_CLASSES/VALID_WEIGHTS defined once in keystore.py; imported here so
# the two modules can't drift out of sync.

# Defaults for an under-specified job: the safe corner of the matrix. throughput
# is never rejected for waiting and never jumps the line; normal is app-default.
DEFAULT_CLASS = "throughput"
DEFAULT_WEIGHT = "normal"

# --- Module state, set by init() --------------------------------------------
STORE: JobStore = None
_check_api_key = None          # injected from main.py
_free_slots_fn = None          # injected: returns current count of free node slots
_prune_task = None
_retention_seconds = 86400     # completed jobs kept 24h by default
_prune_interval_seconds = 300  # prune sweep every 5 min by default


def init(config: dict, check_api_key=None, db_path="/app/jobs.db",
         free_slots_fn=None):
    """
    Called once from main.py startup. Instantiates the store, recovers orphans
    from any previous crash, and reads retention config. check_api_key is the
    function from main.py used for auth; free_slots_fn (optional) returns the
    current count of free node slots, used by admission control. Passing them in
    avoids a circular import.
    """
    global STORE, _check_api_key, _free_slots_fn
    global _retention_seconds, _prune_interval_seconds
    STORE = JobStore(db_path=db_path)
    _check_api_key = check_api_key
    _free_slots_fn = free_slots_fn

    jobs_cfg = config.get("jobs", {})
    _retention_seconds = jobs_cfg.get("retention_seconds", 86400)
    _prune_interval_seconds = jobs_cfg.get("prune_interval_seconds", 300)

    # A job left 'running' means the process died mid-inference last time.
    # No dispatcher exists yet, so in practice this is 0 now, but wiring it in
    # from the start means restart-safety is real the moment dispatch lands.
    requeued = STORE.requeue_orphans()
    if requeued:
        log.info(f"jobs: requeued {requeued} orphaned job(s) from previous run")

    counts = STORE.counts_by_status()
    log.info(f"jobs: store ready, retention={_retention_seconds}s, "
             f"counts={counts or '{}'}")


async def start_prune_task():
    """Start the periodic prune loop. Call from startup() after init()."""
    global _prune_task
    if _prune_task is None:
        _prune_task = asyncio.create_task(_prune_loop())
        log.info(f"jobs: prune task started "
                 f"(every {_prune_interval_seconds}s, retention "
                 f"{_retention_seconds}s)")


async def _prune_loop():
    while True:
        try:
            await asyncio.sleep(_prune_interval_seconds)
            removed = STORE.prune(_retention_seconds)
            if removed:
                log.info(f"jobs: pruned {removed} expired job(s)")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"jobs: prune loop error: {e}")


# --- Helpers ----------------------------------------------------------------

def _auth(request: Request) -> str:
    """Return the client name, or raise. Reuses main.py's check_api_key."""
    if _check_api_key is None:
        # init() wasn't called or didn't get the auth fn — fail closed.
        raise HTTPException(status_code=500, detail="jobs auth not configured")
    client, _tier = _check_api_key(request)
    return client


def _validate_class_weight(job_class: str, weight: str):
    if job_class not in VALID_CLASSES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid class '{job_class}'; must be one of {VALID_CLASSES}")
    if weight not in VALID_WEIGHTS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid weight '{weight}'; must be one of {VALID_WEIGHTS}")


def _extract_job_meta(body: dict) -> dict:
    """
    Pull the v2 routing metadata out of a submission body and validate it.
    Everything else in the body is the inference payload, stored verbatim.
    """
    job_class = body.get("class", DEFAULT_CLASS)
    weight = body.get("weight", DEFAULT_WEIGHT)
    _validate_class_weight(job_class, weight)

    deadline_ms = body.get("deadline_ms")
    if deadline_ms is not None:
        try:
            deadline_ms = int(deadline_ms)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400,
                                detail="deadline_ms must be an integer (ms)")
        if job_class != "deadline":
            # A deadline on a non-deadline job is almost certainly a mistake;
            # reject loudly rather than silently ignoring it.
            raise HTTPException(
                status_code=400,
                detail="deadline_ms is only valid for class 'deadline'")
    elif job_class == "deadline":
        raise HTTPException(status_code=400,
                            detail="class 'deadline' requires deadline_ms")

    return {
        "job_class": job_class,
        "weight": weight,
        "capability": body.get("capability"),  # None => resolve at dispatch
        "deadline_ms": deadline_ms,
    }


def _payload_from_body(body: dict) -> dict:
    """
    The inference payload is the body minus the gateway-only routing fields.
    Stored verbatim and replayed to Ollama at dispatch time.
    """
    routing_keys = {"class", "weight", "capability", "deadline_ms"}
    return {k: v for k, v in body.items() if k not in routing_keys}


def _public_view(job: dict) -> dict:
    """
    Shape a stored job row into what a polling client should see. Hides internal
    columns that aren't meaningful to the caller and keeps the response stable.
    """
    if job is None:
        return None
    return {
        "id": job["id"],
        "batch_id": job.get("batch_id"),
        "status": job["status"],
        "class": job["job_class"],
        "weight": job["weight"],
        "model": job.get("model"),
        "node": job.get("node"),
        "result": job.get("result"),
        "error": job.get("error"),
        "submitted_at": job.get("submitted_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


# --- Endpoints --------------------------------------------------------------

@router.post("/api/jobs")
async def submit_job(request: Request):
    """
    Submit one async job. Returns a job id immediately; the job is queued and
    will run once the dispatcher exists. Body carries the inference payload plus
    optional routing fields: class, weight, capability, deadline_ms.
    """
    client = _auth(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    meta = _extract_job_meta(body)
    payload = _payload_from_body(body)
    if not payload:
        raise HTTPException(status_code=400,
                            detail="empty payload — nothing to run")
    model = payload.get("model")

    # Admission control: reject at arrival if it cannot be served acceptably.
    reject = _admission_check(meta["job_class"], payload, meta["deadline_ms"])
    if reject:
        log.info(f"jobs: REJECTED submit client={client} "
                 f"class={meta['job_class']}: {reject}")
        raise HTTPException(status_code=503, detail=reject)

    job_id = STORE.create(
        client=client,
        job_class=meta["job_class"],
        weight=meta["weight"],
        payload=payload,
        capability=meta["capability"],
        model=model,
        deadline_ms=meta["deadline_ms"],
    )
    log.info(f"jobs: submit id={job_id} client={client} "
             f"class={meta['job_class']} weight={meta['weight']} model={model}")
    return {"id": job_id, "status": "queued"}


@router.post("/api/jobs/batch")
async def submit_batch(request: Request):
    """
    Submit N independent single-inference jobs in one atomic call — the HireDesk
    shape. Body: {"class","weight","capability"?,"deadline_ms"?, "items":[...]}.
    Each item is one inference payload. Class/weight/capability apply to all N;
    they are batch-level, not per-item. Returns the batch id and all job ids.
    """
    client = _auth(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400,
                            detail="'items' must be a non-empty list of payloads")
    if not all(isinstance(it, dict) for it in items):
        raise HTTPException(status_code=400,
                            detail="each item must be a JSON object (a payload)")

    meta = _extract_job_meta(body)

    # Admission for a batch: checked once against the batch's declared class and
    # the first item's payload as a representative shape/size. ANY rejection is
    # honored and rejects the whole batch atomically -- exactly like a single job
    # of that class would be rejected. (Batches are NOT guaranteed throughput-only
    # -- class is caller-declared, same as a single job; see _extract_job_meta.)
    reject = _admission_check(meta["job_class"], items[0], meta["deadline_ms"])
    if reject:
        log.info(f"jobs: REJECTED batch client={client} count={len(items)}: {reject}")
        raise HTTPException(status_code=503, detail=reject)

    # Model can be stated at batch level or per item; batch level wins if given.
    batch_model = body.get("model")
    for it in items:
        if not it:
            raise HTTPException(status_code=400, detail="empty item in batch")

    # Determine one model for the batch if consistent; store None otherwise and
    # let each item's own payload["model"] govern at dispatch.
    model = batch_model
    if model is None:
        models = {it.get("model") for it in items}
        model = models.pop() if len(models) == 1 else None

    batch_id, ids = STORE.create_batch(
        client=client,
        job_class=meta["job_class"],
        weight=meta["weight"],
        payloads=items,
        capability=meta["capability"],
        model=model,
        deadline_ms=meta["deadline_ms"],
    )
    log.info(f"jobs: batch id={batch_id} client={client} "
             f"class={meta['job_class']} weight={meta['weight']} count={len(ids)}")
    return {"batch_id": batch_id, "count": len(ids), "ids": ids,
            "status": "queued"}


@router.get("/api/jobs/batch/{batch_id}")
async def get_batch(batch_id: str, request: Request):
    """Poll a whole batch. Returns each member plus a status rollup."""
    _auth(request)
    jobs = STORE.get_batch(batch_id)
    if not jobs:
        raise HTTPException(status_code=404, detail="batch not found")
    rollup = {}
    for j in jobs:
        rollup[j["status"]] = rollup.get(j["status"], 0) + 1
    done = all(j["status"] in ("succeeded", "failed", "rejected", "cancelled")
               for j in jobs)
    return {
        "batch_id": batch_id,
        "count": len(jobs),
        "complete": done,
        "status_counts": rollup,
        "jobs": [_public_view(j) for j in jobs],
    }


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    """Poll one job's status and result."""
    _auth(request)
    job = STORE.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _public_view(job)


@router.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str, request: Request):
    """
    Cancel a job (or signal the caller gave up). Only works while queued or
    running; a finished job returns 409. Actually stopping in-flight inference
    is the dispatcher's job later — here this records intent.
    """
    _auth(request)
    job = STORE.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    ok = STORE.cancel(job_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"job is '{job['status']}' and cannot be cancelled")
    return {"id": job_id, "status": "cancelled"}


@router.get("/api/jobs")
async def jobs_summary(request: Request):
    """Cheap health/status number: counts by status. Not a full listing."""
    _auth(request)
    return {"counts": STORE.counts_by_status()}
