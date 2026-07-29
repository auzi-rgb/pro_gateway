"""
admission.py — admission control for the AI Gateway (v2).

Decides, at submission time, whether a job can be served acceptably. Rejecting
at arrival is cheap; letting a job wait uselessly is not. Per class:

  interactive  reject if estimated wait > its short target (fail fast)
  deadline     reject if estimated wait + estimated generation > its deadline
  throughput   never rejected for slowness; only the global queue ceiling applies

DESIGN PRINCIPLE: no frozen assumptions.
    The one input that would otherwise be a hardcoded guess — token throughput —
    is MEASURED LIVE from recent completions. The dispatcher feeds each finished
    job's (tokens, duration) into the tracker here; throughput is computed from a
    rolling window of real work. This is self-healing: a node failing, recovering,
    the fleet getting busy, or the model changing all move the measured number on
    their own, so admission decisions track reality without any config change.

    The ONLY constants are:
      - a safety floor on throughput, purely to prevent divide-by-zero and absurd
        estimates when almost no data exists;
      - an expected-output-tokens default, used only for a queued job that did not
        state num_predict (its true output length is unknowable until it runs —
        this is honest estimation, not an assumption that can rot).

    Cold start (just after restart, empty window) uses a SEED throughput. The seed
    is the last-known-good value persisted across restarts, NOT a config guess —
    so even the cold window uses real historical measurement. The moment real
    completions arrive (seconds under any load), the live window takes over and
    the seed is never consulted again.

This module is pure/measured: it has no side effects on jobs or nodes. It reads
queue state and its own throughput window and returns a decision. Testable in
isolation.
"""

import json
import time
import logging
import threading
from collections import deque

log = logging.getLogger("gateway")

# --- The only true constants ------------------------------------------------
# A floor so a near-empty window can't produce a divide-by-zero or a wildly
# optimistic estimate. Deliberately conservative (slow) so uncertainty errs
# toward rejecting/​queuing rather than over-promising.
THROUGHPUT_FLOOR_TOK_S = 5.0
# Default assumed output length for a queued job that didn't state num_predict.
# An estimate of unknowable future output, not a frozen fact about the system.
DEFAULT_EXPECTED_OUTPUT_TOKENS = 150


class ThroughputTracker:
    """
    Rolling record of recent completions -> live aggregate tokens/sec.

    The dispatcher calls record(tokens, duration_s) when a job finishes. We keep
    a time-bounded window and compute throughput as (sum tokens / sum duration)
    over it. Because many jobs run concurrently across the fleet, summing their
    durations would understate wall-clock throughput; instead we track the window
    over WALL-CLOCK time and divide total tokens produced by the wall-clock span,
    which yields true aggregate fleet throughput.

    Thread-safety: record() may be called from dispatcher tasks while the submit
    path reads throughput(). A lock keeps the deque consistent. (asyncio is
    single-threaded, but the lock is cheap insurance and makes the module safe if
    ever called from a thread pool.)
    """

    def __init__(self, window_seconds=60.0, persist_path=None):
        self.window_seconds = window_seconds
        self.persist_path = persist_path
        self._events = deque()   # (finished_at, tokens, duration_s)
        self._lock = threading.Lock()
        self._last_known_good = self._load_seed()

    # --- persistence of last-known-good seed --------------------------------

    def _load_seed(self):
        if not self.persist_path:
            return None
        try:
            with open(self.persist_path) as f:
                v = json.load(f).get("last_known_good_tok_s")
                if v and v > 0:
                    log.info(f"admission: seed throughput {v:.1f} tok/s "
                             f"(persisted last-known-good)")
                    return float(v)
        except (FileNotFoundError, ValueError, KeyError):
            pass
        return None

    def _save_seed(self, value):
        if not self.persist_path:
            return
        try:
            with open(self.persist_path, "w") as f:
                json.dump({"last_known_good_tok_s": value,
                           "saved_at": time.time()}, f)
        except OSError as e:
            log.warning(f"admission: could not persist throughput seed: {e}")

    # --- recording ----------------------------------------------------------

    def record(self, tokens, duration_s, now=None):
        """Feed one completed job's real output. Called by the dispatcher."""
        if tokens is None or duration_s is None or duration_s <= 0 or tokens <= 0:
            return
        now = now or time.time()
        with self._lock:
            self._events.append((now, tokens, duration_s))
            self._prune(now)

    def _prune(self, now):
        cutoff = now - self.window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    # --- reading ------------------------------------------------------------

    def throughput(self, now=None):
        """
        Live aggregate tokens/sec. Uses the rolling window if it has enough data;
        otherwise falls back to the persisted seed; otherwise the floor. Never
        returns below the floor.
        """
        now = now or time.time()
        with self._lock:
            self._prune(now)
            events = list(self._events)

        if len(events) >= 3:
            # Aggregate over wall-clock span of the window, not summed durations,
            # so concurrency across the fleet is reflected correctly.
            span = max(events[-1][0] - events[0][0], 1e-3)
            total_tokens = sum(e[1] for e in events)
            tok_s = total_tokens / span
            # This is a real, current measurement -> it becomes last-known-good.
            if tok_s >= THROUGHPUT_FLOOR_TOK_S:
                if abs((self._last_known_good or 0) - tok_s) > 1.0:
                    self._last_known_good = tok_s
                    self._save_seed(tok_s)
            return max(tok_s, THROUGHPUT_FLOOR_TOK_S)

        # Not enough live data yet (e.g. just after restart): use the seed.
        if self._last_known_good:
            return max(self._last_known_good, THROUGHPUT_FLOOR_TOK_S)
        return THROUGHPUT_FLOOR_TOK_S

    def source(self, now=None):
        """For introspection/dashboard: where the current number comes from."""
        now = now or time.time()
        with self._lock:
            self._prune(now)
            n = len(self._events)
        if n >= 3:
            return "live"
        if self._last_known_good:
            return "seed"
        return "floor"


# --- Shared instance + config (set by init() from main.py) ------------------
# One tracker per process. The dispatcher records completions into it; the
# submit path reads throughput from it. Single source, no circular imports:
# both modules reach it through admission.TRACKER / admission.decide_for_submit.
TRACKER: "ThroughputTracker" = None
_admission_cfg = {}
_enabled = True


def init(config, persist_path="/app/throughput.json"):
    """Called once from main.py startup. Builds the shared tracker."""
    global TRACKER, _admission_cfg, _enabled
    acfg = config.get("admission", {})
    _admission_cfg = acfg
    _enabled = acfg.get("enabled", True)
    window = acfg.get("throughput_window_seconds", 60.0)
    TRACKER = ThroughputTracker(window_seconds=window, persist_path=persist_path)
    log.info(f"admission: initialized (enabled={_enabled}, "
             f"window={window}s, source={TRACKER.source()})")


def record_completion(tokens, duration_s):
    """Dispatcher calls this when a job finishes. Safe no-op if uninitialized."""
    if TRACKER is not None:
        TRACKER.record(tokens, duration_s)


def decide_for_submit(job_class, payload, queued_jobs, free_slots,
                      deadline_ms=None):
    """
    Convenience wrapper used by the submit path: pulls live throughput and config
    from module state and returns an AdmissionResult. If admission is disabled,
    admits everything (the dispatcher still runs jobs; only arrival-rejection is
    off).
    """
    if not _enabled or TRACKER is None:
        return AdmissionResult(True)
    tp = TRACKER.throughput()
    return decide(job_class, payload, queued_jobs, free_slots, tp,
                  _admission_cfg, deadline_ms=deadline_ms)


# --- Estimators (pure) ------------------------------------------------------

def expected_output_tokens(payload, default=DEFAULT_EXPECTED_OUTPUT_TOKENS):
    """
    Best estimate of a job's output length before it runs. Uses num_predict /
    max_tokens if the caller stated one; otherwise the default. Genuinely an
    estimate — real length is unknowable until generation happens.
    """
    if not isinstance(payload, dict):
        return default
    opts = payload.get("options") or {}
    for key in ("num_predict", "max_tokens"):
        v = payload.get(key, opts.get(key))
        if isinstance(v, int) and v > 0:
            return v
    return default


def pending_output_tokens(queued_jobs, running_count):
    """
    Total output tokens estimated to be ahead of a newly-arriving job: the sum of
    expected output over everything currently queued. (Running jobs are handled
    by slot availability, below, not counted here.)
    """
    return sum(expected_output_tokens(j.get("payload") or {}) for j in queued_jobs)


def estimate_wait_seconds(queued_jobs, free_slots, throughput_tok_s):
    """
    Estimated time before a newly-arriving job STARTS generating.

    If a slot is free right now, wait is ~0 — it dispatches next tick. Otherwise
    it waits behind the queue: the pending output tokens ahead of it, divided by
    the measured aggregate throughput.
    """
    if free_slots > 0:
        return 0.0
    pending = pending_output_tokens(queued_jobs, 0)
    if throughput_tok_s <= 0:
        throughput_tok_s = THROUGHPUT_FLOOR_TOK_S
    return pending / throughput_tok_s


def estimate_generation_seconds(payload, throughput_tok_s):
    """Estimated time to generate this job's own output once it starts."""
    out = expected_output_tokens(payload)
    if throughput_tok_s <= 0:
        throughput_tok_s = THROUGHPUT_FLOOR_TOK_S
    return out / throughput_tok_s


# --- The decision -----------------------------------------------------------

class AdmissionResult:
    __slots__ = ("admit", "reason", "estimated_wait_s")

    def __init__(self, admit, reason=None, estimated_wait_s=0.0):
        self.admit = admit
        self.reason = reason
        self.estimated_wait_s = estimated_wait_s

    def __repr__(self):
        return (f"AdmissionResult(admit={self.admit}, reason={self.reason!r}, "
                f"wait={self.estimated_wait_s:.2f}s)")


def decide(job_class, payload, queued_jobs, free_slots, throughput_tok_s,
           cfg, deadline_ms=None):
    """
    Return an AdmissionResult. Pure: no side effects. The caller records a
    rejection (mark_rejected) and returns the reason to the client.

    cfg keys (all optional, with defaults):
      interactive_max_wait_s : reject interactive above this estimated wait (2.0)
      global_queue_ceiling   : reject anything if queue is at/above this (500)
    """
    interactive_max_wait = cfg.get("interactive_max_wait_s", 2.0)
    ceiling = cfg.get("global_queue_ceiling", 500)

    # Global backstop first: applies to every class, stops unbounded growth.
    if len(queued_jobs) >= ceiling:
        return AdmissionResult(
            False,
            f"queue at global ceiling ({len(queued_jobs)}/{ceiling}); capacity short",
        )

    wait = estimate_wait_seconds(queued_jobs, free_slots, throughput_tok_s)

    if job_class == "interactive":
        if wait > interactive_max_wait:
            return AdmissionResult(
                False,
                f"estimated wait {wait:.1f}s exceeds interactive target "
                f"{interactive_max_wait:.1f}s — try again shortly",
                wait,
            )
        return AdmissionResult(True, estimated_wait_s=wait)

    if job_class == "deadline":
        gen = estimate_generation_seconds(payload, throughput_tok_s)
        total = wait + gen
        budget = (deadline_ms or 0) / 1000.0
        if budget <= 0:
            return AdmissionResult(False, "deadline job missing a valid deadline")
        if total > budget:
            return AdmissionResult(
                False,
                f"cannot meet deadline: estimated {total:.1f}s "
                f"(wait {wait:.1f}s + gen {gen:.1f}s) > budget {budget:.1f}s",
                wait,
            )
        return AdmissionResult(True, estimated_wait_s=wait)

    # throughput: never rejected for slowness; ceiling already checked above.
    return AdmissionResult(True, estimated_wait_s=wait)
