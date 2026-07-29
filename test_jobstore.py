"""
test_jobstore.py — exercise jobstore.py against a real temp SQLite file.

Run in the container:  python3 test_jobstore.py
No pytest dependency — plain asserts so it runs anywhere the gateway runs.
Uses a temp DB, so it touches nothing real.
"""

import os
import time
import tempfile

from jobstore import (
    JobStore,
    STATUS_QUEUED, STATUS_RUNNING, STATUS_SUCCEEDED,
    STATUS_FAILED, STATUS_REJECTED, STATUS_CANCELLED,
)

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


def main():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "jobs_test.db")
    js = JobStore(db_path=db)

    print("\n[1] create + get")
    jid = js.create(
        client="hiredesk", job_class="throughput", weight="normal",
        payload={"model": "mistral-nemo:12b", "messages": [{"role": "user", "content": "hi"}]},
        capability="standard", model="mistral-nemo:12b",
    )
    job = js.get(jid)
    check("job created with queued status", job["status"] == STATUS_QUEUED)
    check("payload round-trips as dict", isinstance(job["payload"], dict))
    check("payload content preserved", job["payload"]["messages"][0]["content"] == "hi")
    check("class/weight/capability stored",
          job["job_class"] == "throughput" and job["weight"] == "normal"
          and job["capability"] == "standard")
    check("submitted_at set, started/finished null",
          job["submitted_at"] and job["started_at"] is None
          and job["finished_at"] is None)
    check("batch_id null for single create", job["batch_id"] is None)

    print("\n[2] normal lifecycle: queued -> running -> succeeded")
    check("mark_running from queued succeeds", js.mark_running(jid, "ai-node-01") is True)
    job = js.get(jid)
    check("status now running", job["status"] == STATUS_RUNNING)
    check("node recorded", job["node"] == "ai-node-01")
    check("started_at set", job["started_at"] is not None)
    check("mark_succeeded from running succeeds",
          js.mark_succeeded(jid, {"message": {"content": "hello"}}) is True)
    job = js.get(jid)
    check("status now succeeded", job["status"] == STATUS_SUCCEEDED)
    check("result round-trips", job["result"]["message"]["content"] == "hello")
    check("finished_at set", job["finished_at"] is not None)

    print("\n[3] transition guards block illegal moves")
    # Already succeeded — nothing should move it.
    check("mark_running on terminal job is blocked", js.mark_running(jid, "x") is False)
    check("mark_succeeded again is blocked", js.mark_succeeded(jid, {}) is False)
    check("cancel on terminal job is blocked", js.cancel(jid) is False)
    # A fresh queued job cannot jump straight to succeeded (must be running).
    jid2 = js.create("openwebui", "interactive", "normal", {"x": 1})
    check("mark_succeeded from queued is blocked (must run first)",
          js.mark_succeeded(jid2, {}) is False)
    check("job still queued after blocked transition",
          js.get(jid2)["status"] == STATUS_QUEUED)

    print("\n[4] reject path (admission control)")
    jid3 = js.create("hiredesk", "deadline", "high", {"x": 1}, deadline_ms=90000)
    check("deadline_ms stored", js.get(jid3)["deadline_ms"] == 90000)
    check("mark_rejected from queued succeeds",
          js.mark_rejected(jid3, "estimated wait exceeds deadline") is True)
    job = js.get(jid3)
    check("status rejected", job["status"] == STATUS_REJECTED)
    check("reject reason in error field",
          "deadline" in (job["error"] or ""))
    check("cannot reject a running job",
          js.mark_rejected(jid, "too late") is False)  # jid is succeeded

    print("\n[5] cancel path")
    jid4 = js.create("openwebui", "throughput", "low", {"x": 1})
    check("cancel from queued succeeds", js.cancel(jid4) is True)
    check("status cancelled", js.get(jid4)["status"] == STATUS_CANCELLED)
    jid5 = js.create("openwebui", "throughput", "low", {"x": 1})
    js.mark_running(jid5, "ai-node-03")
    check("cancel from running succeeds (caller gave up mid-run)",
          js.cancel(jid5) is True)

    print("\n[6] fail path from both states")
    jid6 = js.create("hiredesk", "throughput", "normal", {"x": 1})
    check("fail from queued succeeds", js.mark_failed(jid6, "node exploded") is True)
    check("error recorded", js.get(jid6)["error"] == "node exploded")
    jid7 = js.create("hiredesk", "throughput", "normal", {"x": 1})
    js.mark_running(jid7, "ai-node-04")
    check("fail from running succeeds", js.mark_failed(jid7, "stream broke") is True)

    print("\n[7] batch create is atomic and independent")
    payloads = [{"model": "mistral-nemo:12b", "messages": [{"role": "user", "content": f"resume {i}"}]}
                for i in range(40)]
    batch_id, ids = js.create_batch(
        client="hiredesk", job_class="throughput", weight="normal",
        payloads=payloads, capability="standard", model="mistral-nemo:12b")
    check("batch returns 40 ids", len(ids) == 40)
    batch_jobs = js.get_batch(batch_id)
    check("get_batch returns all 40", len(batch_jobs) == 40)
    check("all share the batch_id", all(j["batch_id"] == batch_id for j in batch_jobs))
    check("each is an independent queued job",
          all(j["status"] == STATUS_QUEUED for j in batch_jobs))
    check("batch preserves order",
          batch_jobs[0]["payload"]["messages"][0]["content"] == "resume 0"
          and batch_jobs[39]["payload"]["messages"][0]["content"] == "resume 39")
    # Dispatch one batch member; the rest are untouched — proves independence.
    js.mark_running(ids[0], "ai-node-01")
    js.mark_succeeded(ids[0], {"ok": True})
    still_queued = [j for j in js.get_batch(batch_id) if j["status"] == STATUS_QUEUED]
    check("dispatching one member leaves 39 queued", len(still_queued) == 39)

    print("\n[8] list_queued + counts_by_status")
    counts = js.counts_by_status()
    check("counts_by_status returns a dict with queued key", STATUS_QUEUED in counts)
    queued = js.list_queued()
    check("list_queued returns only queued jobs",
          all(j["status"] == STATUS_QUEUED for j in queued))
    check("list_queued is oldest-first",
          all(queued[i]["submitted_at"] <= queued[i + 1]["submitted_at"]
              for i in range(len(queued) - 1)))

    print("\n[9] orphan recovery")
    orphan = js.create("hiredesk", "throughput", "normal", {"x": 1})
    js.mark_running(orphan, "ai-node-01")  # simulate process death mid-run
    n = js.requeue_orphans()
    check("requeue_orphans moved the running job back", n >= 1)
    job = js.get(orphan)
    check("orphan is queued again", job["status"] == STATUS_QUEUED)
    check("orphan node/started cleared",
          job["node"] is None and job["started_at"] is None)

    print("\n[10] prune")
    # Make a terminal job and backdate its finished_at by 2 hours.
    old = js.create("openwebui", "throughput", "low", {"x": 1})
    js.cancel(old)
    with js.get_conn() as conn:
        conn.execute("UPDATE jobs SET finished_at = ? WHERE id = ?",
                     (time.time() - 7200, old))
    removed = js.prune(older_than_seconds=3600)
    check("prune removed the 2h-old terminal job", removed >= 1)
    check("pruned job is gone", js.get(old) is None)
    # A fresh terminal job should NOT be pruned.
    fresh = js.create("openwebui", "throughput", "low", {"x": 1})
    js.cancel(fresh)
    removed2 = js.prune(older_than_seconds=3600)
    check("prune leaves recently-finished jobs", js.get(fresh) is not None)
    # A queued job is never pruned regardless of age.
    check("queued jobs survive prune", len(js.list_queued()) > 0)

    print("\n[11] WAL mode actually engaged")
    with js.get_conn() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("journal_mode is wal", mode.lower() == "wal")

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
