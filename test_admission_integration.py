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

    print("\n[2] interactive fails fast when no slot and deep queue")
    c = build({"enabled": True, "interactive_max_wait_s": 2.0,
               "global_queue_ceiling": 500})
    seed_throughput(300)  # ~300 tok/s
    _free["n"] = 0
    # Fill the queue with throughput jobs so pending work is high.
    for i in range(20):
        c.post("/api/jobs", json={"model": "m", "num_predict": 150,
                                  "messages": [{"role":"u","content":str(i)}],
                                  "class": "throughput"})
    # 20 * 150 = 3000 tokens pending / 300 = 10s wait > 2s target -> reject
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
    for i in range(20):
        c.post("/api/jobs", json={"model": "m", "num_predict": 150,
                                  "messages": [{"role":"u","content":str(i)}],
                                  "class": "throughput"})
    # deep queue -> long wait -> tight deadline can't be met
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

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
