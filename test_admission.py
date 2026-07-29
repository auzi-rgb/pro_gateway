"""
test_admission.py — exercise admission.py in isolation.

No gateway, no nodes. Constructs queue states and throughput conditions directly
and checks the decisions. Crucially, it proves the self-healing property: when
measured throughput drops (as it would if a node failed), the estimated wait
rises and admission tightens on its own — with no config change.

Run: python3 test_admission.py
"""

import os
import time
import tempfile

import admission
from admission import (
    ThroughputTracker, AdmissionResult, decide,
    estimate_wait_seconds, estimate_generation_seconds, expected_output_tokens,
    THROUGHPUT_FLOOR_TOK_S, DEFAULT_EXPECTED_OUTPUT_TOKENS,
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


def job(num_predict=None, model="m"):
    p = {"model": model, "messages": [{"role": "user", "content": "x"}]}
    if num_predict is not None:
        p["num_predict"] = num_predict
    return {"payload": p}


def main():
    print("\n[1] expected_output_tokens")
    check("uses num_predict when present",
          expected_output_tokens({"num_predict": 400}) == 400)
    check("uses options.num_predict",
          expected_output_tokens({"options": {"num_predict": 250}}) == 250)
    check("uses max_tokens alias",
          expected_output_tokens({"max_tokens": 80}) == 80)
    check("falls back to default",
          expected_output_tokens({"model": "m"}) == DEFAULT_EXPECTED_OUTPUT_TOKENS)

    print("\n[2] wait estimation basics")
    # Free slot -> wait ~0 regardless of queue.
    q = [job(150) for _ in range(10)]
    check("free slot => zero wait",
          estimate_wait_seconds(q, free_slots=2, throughput_tok_s=300) == 0.0)
    # No free slot -> pending / throughput. 10 jobs * 150 tok = 1500 tok / 300 = 5s
    w = estimate_wait_seconds(q, free_slots=0, throughput_tok_s=300)
    check("no slot => pending/throughput (1500/300 = 5s)", abs(w - 5.0) < 0.01)

    print("\n[3] SELF-HEALING: lower throughput => longer wait, same queue")
    w_full = estimate_wait_seconds(q, 0, throughput_tok_s=300)   # healthy fleet
    w_degraded = estimate_wait_seconds(q, 0, throughput_tok_s=225)  # a node down
    check("same queue, lower throughput => longer estimated wait",
          w_degraded > w_full)
    check("estimate scales inversely with throughput",
          abs(w_degraded - (1500 / 225)) < 0.01)
    print(f"       healthy(300t/s)={w_full:.2f}s  degraded(225t/s)={w_degraded:.2f}s")

    print("\n[4] interactive: fail fast when wait exceeds target")
    cfg = {"interactive_max_wait_s": 2.0, "global_queue_ceiling": 500}
    # Free slot -> admit.
    r = decide("interactive", job()["payload"],
               q, free_slots=1, throughput_tok_s=300, cfg=cfg)
    check("interactive admitted when a slot is free", r.admit is True)
    # No slot, deep queue -> wait 5s > 2s target -> reject.
    r = decide("interactive", job()["payload"], q, free_slots=0,
               throughput_tok_s=300, cfg=cfg)
    check("interactive rejected when wait exceeds target", r.admit is False)
    check("rejection reason mentions the target", "target" in r.reason)

    print("\n[5] deadline: reject when it cannot be met")
    one = [job(150)]
    # Big budget -> admit. wait(0 free? no, 0 slots) = 150/300=0.5s, gen=0.5s, total 1s < 5s
    r = decide("deadline", job(150)["payload"], one, free_slots=0,
               throughput_tok_s=300, cfg=cfg, deadline_ms=5000)
    check("deadline admitted when budget is ample", r.admit is True)
    # Tiny budget -> reject.
    r = decide("deadline", job(150)["payload"], one, free_slots=0,
               throughput_tok_s=300, cfg=cfg, deadline_ms=300)
    check("deadline rejected when budget too small", r.admit is False)
    check("reason explains the shortfall", "deadline" in r.reason.lower())
    # Missing deadline -> reject cleanly.
    r = decide("deadline", job(150)["payload"], one, free_slots=1,
               throughput_tok_s=300, cfg=cfg, deadline_ms=None)
    check("deadline without budget rejected", r.admit is False)

    print("\n[6] SELF-HEALING end to end: node loss flips a deadline decision")
    # A deadline job that PASSES on a healthy fleet should FAIL when throughput
    # drops — with no config change, purely from the measured number.
    big_q = [job(200) for _ in range(6)]  # 1200 tokens pending
    # healthy: wait 1200/600=2s + gen 200/600=0.33 = 2.33s, budget 3s -> admit
    r_healthy = decide("deadline", job(200)["payload"], big_q, free_slots=0,
                       throughput_tok_s=600, cfg=cfg, deadline_ms=3000)
    # degraded (half fleet): wait 1200/300=4s -> already over 3s -> reject
    r_degraded = decide("deadline", job(200)["payload"], big_q, free_slots=0,
                        throughput_tok_s=300, cfg=cfg, deadline_ms=3000)
    check("deadline admitted on healthy fleet", r_healthy.admit is True)
    check("SAME job rejected when throughput halves (self-healing)",
          r_degraded.admit is False)
    print(f"       healthy admit={r_healthy.admit}  degraded admit={r_degraded.admit}")

    print("\n[7] throughput class: never rejected for slowness")
    huge_q = [job(400) for _ in range(50)]
    r = decide("throughput", job()["payload"], huge_q, free_slots=0,
               throughput_tok_s=THROUGHPUT_FLOOR_TOK_S, cfg=cfg)
    check("throughput admitted even with a huge slow queue", r.admit is True)

    print("\n[8] global ceiling: rejects every class")
    ceil_cfg = {"global_queue_ceiling": 5}
    over = [job() for _ in range(5)]
    for cls in ("interactive", "deadline", "throughput"):
        r = decide(cls, job()["payload"], over, free_slots=1,
                   throughput_tok_s=300, cfg=ceil_cfg,
                   deadline_ms=999999)
        check(f"{cls} rejected at global ceiling", r.admit is False)

    print("\n[9] ThroughputTracker: live measurement")
    t = ThroughputTracker(window_seconds=60.0)
    check("cold tracker returns floor", t.throughput() == THROUGHPUT_FLOOR_TOK_S)
    check("cold source is 'floor'", t.source() == "floor")
    # Record real completions: 100 tokens each over a 1s wall-clock span, 4 jobs.
    now = time.time()
    t.record(100, 0.5, now=now)
    t.record(100, 0.5, now=now + 0.3)
    t.record(100, 0.5, now=now + 0.6)
    t.record(100, 0.5, now=now + 1.0)
    # 400 tokens over ~1.0s wall-clock span => ~400 tok/s aggregate.
    tp = t.throughput(now=now + 1.0)
    check("live throughput reflects aggregate (~400 tok/s)", 300 < tp < 500)
    check("source now 'live'", t.source(now=now + 1.0) == "live")
    print(f"       measured live throughput: {tp:.0f} tok/s")

    print("\n[10] tracker self-heals: window ages out old data")
    # Jump forward past the window; old events prune, throughput reverts to seed.
    future = now + 200
    tp2 = t.throughput(now=future)
    check("after window expires, falls back (not stuck on old live value)",
          t.source(now=future) in ("seed", "floor"))
    # But the last good value was persisted as the seed, so it's not the floor.
    check("seed carries last-known-good, not floor",
          tp2 > THROUGHPUT_FLOOR_TOK_S)
    print(f"       post-expiry throughput: {tp2:.0f} tok/s (source={t.source(now=future)})")

    print("\n[11] seed persists across 'restart'")
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "tp.json")
    t1 = ThroughputTracker(window_seconds=60.0, persist_path=path)
    base = time.time()
    for i in range(5):
        t1.record(120, 0.4, now=base + i * 0.2)
    live = t1.throughput(now=base + 0.8)          # forces a live calc + seed save
    check("live value computed", live > THROUGHPUT_FLOOR_TOK_S)
    # New tracker, same path = simulated restart with empty window.
    t2 = ThroughputTracker(window_seconds=60.0, persist_path=path)
    seeded = t2.throughput()                        # empty window -> uses seed
    check("fresh tracker seeds from persisted last-known-good",
          seeded > THROUGHPUT_FLOOR_TOK_S)
    check("seeded source is 'seed'", t2.source() == "seed")
    print(f"       persisted seed after restart: {seeded:.0f} tok/s")

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
