"""
test_jobs_api.py — exercise jobs_api.py end to end with a real FastAPI app.

Mounts the router on a throwaway app, injects a fake check_api_key, points the
store at a temp DB, and drives every endpoint with TestClient. Touches nothing
real. Run: python3 test_jobs_api.py
"""

import os
import tempfile

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import jobs_api

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


def fake_check_api_key(request: Request):
    # Mimic main.py's signature: returns (client, tier). Reads a header so we
    # can vary the client; defaults to a test client.
    client = request.headers.get("x-test-client", "test-client")
    return client, 2


def build_app():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "jobs_api_test.db")
    app = FastAPI()
    # init() with a config that sets short prune interval so nothing hangs.
    jobs_api.init(
        {"jobs": {"retention_seconds": 3600, "prune_interval_seconds": 9999}},
        check_api_key=fake_check_api_key,
        db_path=db,
    )
    app.include_router(jobs_api.router)
    return TestClient(app)


def main():
    c = build_app()

    print("\n[1] submit single job")
    r = c.post("/api/jobs", json={
        "model": "mistral-nemo:12b",
        "messages": [{"role": "user", "content": "hello"}],
        "class": "throughput", "weight": "normal",
    })
    check("submit returns 200", r.status_code == 200)
    body = r.json()
    check("returns an id", "id" in body and len(body["id"]) == 32)
    check("status queued", body["status"] == "queued")
    jid = body["id"]

    print("\n[2] poll the job")
    r = c.get(f"/api/jobs/{jid}")
    check("poll returns 200", r.status_code == 200)
    j = r.json()
    check("status still queued (no dispatcher yet)", j["status"] == "queued")
    check("class echoed", j["class"] == "throughput")
    check("model captured from payload", j["model"] == "mistral-nemo:12b")
    check("result null while queued", j["result"] is None)

    print("\n[3] defaults when class/weight omitted")
    r = c.post("/api/jobs", json={"model": "m", "prompt": "x"})
    check("submit without class/weight succeeds", r.status_code == 200)
    j = c.get(f"/api/jobs/{r.json()['id']}").json()
    check("defaults to throughput", j["class"] == "throughput")
    check("defaults to normal weight", j["weight"] == "normal")

    print("\n[4] validation rejects junk")
    r = c.post("/api/jobs", json={"model": "m", "class": "urgent", "prompt": "x"})
    check("bad class -> 400", r.status_code == 400)
    r = c.post("/api/jobs", json={"model": "m", "weight": "vip", "prompt": "x"})
    check("bad weight -> 400", r.status_code == 400)
    r = c.post("/api/jobs", json={"class": "throughput"})
    check("empty payload -> 400", r.status_code == 400)

    print("\n[5] deadline class rules")
    r = c.post("/api/jobs", json={"model": "m", "prompt": "x", "class": "deadline"})
    check("deadline without deadline_ms -> 400", r.status_code == 400)
    r = c.post("/api/jobs", json={"model": "m", "prompt": "x",
                                  "class": "throughput", "deadline_ms": 5000})
    check("deadline_ms on non-deadline class -> 400", r.status_code == 400)
    r = c.post("/api/jobs", json={"model": "m", "prompt": "x",
                                  "class": "deadline", "weight": "high",
                                  "deadline_ms": 90000})
    check("valid deadline job -> 200", r.status_code == 200)
    j = c.get(f"/api/jobs/{r.json()['id']}").json()
    check("deadline job class stored", j["class"] == "deadline")

    print("\n[6] routing fields are stripped from payload")
    # Submit with routing fields, then confirm poll doesn't leak them back as
    # payload — we can't see payload via the public view, but we can confirm the
    # job was created and model came from the real payload key, not 'class'.
    r = c.post("/api/jobs", json={
        "model": "coder", "prompt": "y",
        "class": "interactive", "weight": "critical", "capability": "high",
    })
    check("interactive job accepted", r.status_code == 200)
    j = c.get(f"/api/jobs/{r.json()['id']}").json()
    check("model is real model, not a routing field", j["model"] == "coder")

    print("\n[7] batch submit (HireDesk shape)")
    items = [{"model": "mistral-nemo:12b",
              "messages": [{"role": "user", "content": f"resume {i}"}]}
             for i in range(40)]
    r = c.post("/api/jobs/batch", json={
        "class": "throughput", "weight": "normal",
        "capability": "standard", "items": items,
    })
    check("batch returns 200", r.status_code == 200)
    b = r.json()
    check("batch count is 40", b["count"] == 40)
    check("batch returns 40 ids", len(b["ids"]) == 40)
    batch_id = b["batch_id"]

    print("\n[8] poll the batch")
    r = c.get(f"/api/jobs/batch/{batch_id}")
    check("batch poll 200", r.status_code == 200)
    bp = r.json()
    check("batch reports 40 jobs", bp["count"] == 40)
    check("batch not complete (all queued)", bp["complete"] is False)
    check("status rollup shows 40 queued", bp["status_counts"].get("queued") == 40)

    print("\n[9] batch validation")
    r = c.post("/api/jobs/batch", json={"class": "throughput", "items": []})
    check("empty items -> 400", r.status_code == 400)
    r = c.post("/api/jobs/batch", json={"class": "throughput", "items": "nope"})
    check("non-list items -> 400", r.status_code == 400)
    r = c.post("/api/jobs/batch", json={"class": "throughput",
                                        "items": ["not-a-dict"]})
    check("non-object item -> 400", r.status_code == 400)

    print("\n[10] cancel")
    r = c.delete(f"/api/jobs/{jid}")
    check("cancel queued job -> 200", r.status_code == 200)
    check("cancel reports cancelled", r.json()["status"] == "cancelled")
    j = c.get(f"/api/jobs/{jid}").json()
    check("job now cancelled", j["status"] == "cancelled")
    r = c.delete(f"/api/jobs/{jid}")
    check("re-cancel finished job -> 409", r.status_code == 409)

    print("\n[11] not-found handling")
    check("poll missing job -> 404", c.get("/api/jobs/deadbeef").status_code == 404)
    check("cancel missing job -> 404",
          c.delete("/api/jobs/deadbeef").status_code == 404)
    check("poll missing batch -> 404",
          c.get("/api/jobs/batch/deadbeef").status_code == 404)

    print("\n[12] summary endpoint")
    r = c.get("/api/jobs")
    check("summary 200", r.status_code == 200)
    check("summary has counts", "counts" in r.json())

    print("\n[13] restart persistence")
    # New app instance, SAME db -> queued jobs must still be there.
    db_path = jobs_api.STORE.db_path
    app2 = FastAPI()
    jobs_api.init({"jobs": {}}, check_api_key=fake_check_api_key, db_path=db_path)
    app2.include_router(jobs_api.router)
    c2 = TestClient(app2)
    r = c2.get(f"/api/jobs/batch/{batch_id}")
    check("batch survives 'restart'", r.status_code == 200 and r.json()["count"] == 40)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
