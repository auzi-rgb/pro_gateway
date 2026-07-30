"""
test_admission_integration.py — prove admission actually gates the submit
endpoints end to end (not just that it doesn't break existing tests).

Wires jobs_api with a real admission tracker and a controllable free_slots_fn,
then submits jobs and checks they are admitted or rejected as expected.

Run: python3 test_admission_integration.py
"""

import os
import time
import tempfile

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import jobs_api
import admission

PASS = 0
FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   - {label}")
    else:
        FAIL += 1
        print(f"  FAIL - {label}")


def fake_auth(request: Request):
    return "test-client", 2


# controllable free-slot count
_free = {"n": 0}
def free_slots_fn():
    return _free["n"]


def build(admission_cfg):
    tmp = tempfile.mkdtemp()
    app = FastAPI()
    jobs_api.init(
        {"jobs": {"prune_interval_seconds": 9999}},
        check_api_key=fake_auth,
        db_path=os.path.join(tmp, "jobs.db"),
        free_slots_fn=free_slots_fn,
    )
    admission.init(
        {"admission": admission_cfg},
        persist_path=os.path.join(tmp, "tp.json"),
    )
    app.include_router(jobs_api.router)
    return TestClient(app)


def seed_throughput(tok_s):
    """Force a known live throughput by recording synthetic completions."""
    now = time.time()
    # Record enough events over a 1s span to hit the 'live' branch (>=3).
    n = 10
    per = tok_s / n  # tokens per event so total/1s ~= tok_s
    for i in range(n):
        admission.TRACKER.record(max(int(per), 1), 0.1, now=now + i * (1.0 / n))


def main():
    print("\n[1] admission OFF (default) admits everything")
    c = build({"enabled": False})
    _free["n"] = 0
    r = c.post("/api/jobs", json={"model": "m", "messages": [{"role":"u","content":"x"}],
                                  "class": "interactive"})
    check("interactive admitted when admission disabled", r.status_code == 200)

    print("\n[2] interactive fails fast when no slot and a genuine interactive backlog exists")
    c = build({"enabled": True, "interactive_max_wait_s": 2.0,
               "global_queue_ceiling": 500})
    seed_throughput(300)  # ~300 tok/s
    _free["n"] = 0
    # Fill the queue with INTERACTIVE jobs -- a throughput backlog no longer
    # counts toward an interactive arrival's wait estimate (the admission.py
    # class-rank fix), so this must be same-class backlog to genuinely test
    # the "wait exceeds target" path. Self-limiting: once the interactive
    # queue is deep enough to exceed the 2s target, further submissions in
    # this loop are themselves rejected and never join the queue, so it
    # naturally stabilizes rather than growing without bound.
    for i in range(20):
        c.post("/api/jobs", json={"model": "m", "num_predict": 150,
                                  "messages": [{"role":"u","content":str(i)}],
                                  "class": "interactive"})
    r = c.post("/api/jobs", json={"model": "m", "num_predict": 150,
                                  "messages": [{"role":"u","content":"chat"}],
                                  "class": "interactive"})
    check("interactive REJECTED (503) when wait exceeds target",
          r.status_code == 503)
    check("rejection detail mentions target", "target" in r.json()["detail"])

    print("\n[3] interactive admitted when a slot is free")
    _free["n"] = 3   # free slots -> wait ~0
    r = c.post("/api/jobs", json={"model": "m", "num_predict": 150,
                                  "messages": [{"role":"u","content":"chat"}],
                                  "class": "interactive"})
    check("interactive admitted (200) with free slots", r.status_code == 200)

    print("\n[4] deadline rejected when it cannot be met")
    c = build({"enabled": True, "global_queue_ceiling": 500})
    seed_throughput(300)
    _free["n"] = 0
    # Fill with DEADLINE-class filler (generous deadline_ms so they admit
    # easily) -- a throughput backlog no longer counts toward a deadline
    # arrival's wait estimate either (same class-rank fix as above), so this
    # must be same-or-higher-rank backlog to genuinely test the "unmeetable"
    # path.
    for i in range(20):
        c.post("/api/jobs", json={"model": "m", "num_predict": 150,
                                  "messages": [{"role":"u","content":str(i)}],
                                  "class": "deadline", "deadline_ms": 600000})
    # deep same-rank queue -> long wait -> tight deadline can't be met
    r = c.post("/api/jobs", json={"model": "m", "num_predict": 150,
                                  "messages": [{"role":"u","content":"urgent"}],
                                  "class": "deadline", "deadline_ms": 1000})
    check("deadline REJECTED (503) when unmeetable", r.status_code == 503)
    check("reason explains deadline", "deadline" in r.json()["detail"].lower())

    print("\n[5] deadline admitted with ample budget")
    r = c.post("/api/jobs", json={"model": "m", "num_predict": 150,
                                  "messages": [{"role":"u","content":"ok"}],
                                  "class": "deadline", "deadline_ms": 600000})
    check("deadline admitted (200) with large budget", r.status_code == 200)

    print("\n[6] throughput never rejected for slowness")
    # Even with the deep slow queue, throughput is admitted.
    r = c.post("/api/jobs", json={"model": "m", "num_predict": 400,
                                  "messages": [{"role":"u","content":"batch"}],
                                  "class": "throughput"})
    check("throughput admitted despite deep queue", r.status_code == 200)

    print("\n[7] global ceiling rejects even throughput")
    c = build({"enabled": True, "global_queue_ceiling": 5})
    seed_throughput(300)
    _free["n"] = 0
    for i in range(5):
        c.post("/api/jobs", json={"model": "m", "messages": [{"role":"u","content":str(i)}],
                                  "class": "throughput"})
    r = c.post("/api/jobs", json={"model": "m", "messages": [{"role":"u","content":"over"}],
                                  "class": "throughput"})
    check("throughput REJECTED at global ceiling", r.status_code == 503)
    check("reason mentions ceiling", "ceiling" in r.json()["detail"])

    print("\n[8] batch rejected only at ceiling, not for slowness")
    c = build({"enabled": True, "global_queue_ceiling": 3})
    seed_throughput(300)
    _free["n"] = 0
    # Under ceiling: a batch of 40 slow items should still be ADMITTED
    # (throughput not rejected for slowness).
    items = [{"model": "m", "num_predict": 400,
              "messages": [{"role":"u","content":str(i)}]} for i in range(40)]
    # queue currently empty (0 < 3 ceiling) so batch admits
    r = c.post("/api/jobs/batch", json={"class": "throughput", "items": items})
    check("batch admitted under ceiling despite slow items", r.status_code == 200)

    print("\n[9] deadline batch REJECTED when unmeetable (not silently admitted -- the bug fix)")
    c = build({"enabled": True, "global_queue_ceiling": 500})
    seed_throughput(300)
    _free["n"] = 0
    # Fill with DEADLINE-class filler (generous deadline_ms so they admit
    # easily) -- same-rank backlog is what should genuinely delay a new
    # deadline arrival (see the admission.py class-rank fix above).
    for i in range(20):
        c.post("/api/jobs", json={"model": "m", "num_predict": 150,
                                  "messages": [{"role":"u","content":str(i)}],
                                  "class": "deadline", "deadline_ms": 600000})
    # deep same-rank queue -> long wait -> tight deadline can't be met, and this
    # rejection reason contains "deadline", not "ceiling" -- proves it's honored either way.
    items = [{"model": "m", "num_predict": 150,
              "messages": [{"role":"u","content":str(i)}]} for i in range(5)]
    r = c.post("/api/jobs/batch", json={"class": "deadline", "deadline_ms": 1000,
                                        "items": items})
    check("deadline batch REJECTED (503) when unmeetable, not admitted",
          r.status_code == 503)
    check("rejection reason explains the deadline shortfall (not just 'ceiling')",
          "deadline" in r.json()["detail"].lower())

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
