"""
test_dispatcher.py — exercise dispatcher.py with a fake fleet and fake HTTP.

No real Ollama, no real gateway. Builds fake NodeState-like objects that honor
the active_requests/max_concurrent contract, monkeypatches httpx so inference is
simulated with a controllable delay, and drives the real dispatcher loop against
a temp job store.

Run: python3 test_dispatcher.py
"""

import os
import time
import json
import asyncio
import tempfile

import httpx

import dispatcher
from jobstore import JobStore

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


# --- Fake fleet -------------------------------------------------------------

class FakeNode:
    def __init__(self, name, models, max_concurrent=3):
        self.name = name
        self.url = f"http://{name}:11434"
        self.available_models = models
        self.max_concurrent = max_concurrent
        self.active_requests = 0
        self.enabled = True
        self.healthy = True
        self.circuit_open_until = 0.0
        # track the high-water mark to prove we never exceed max_concurrent
        self.peak = 0

    def bump_peak(self):
        self.peak = max(self.peak, self.active_requests)


def make_free_node_for(nodes):
    """A stand-in for main.py's _free_node_for, same contract."""
    rr = {"n": 0}

    def _free_node_for(model):
        target = model
        cands = []
        for node in nodes:
            if not node.enabled or not node.healthy:
                continue
            if node.circuit_open_until > time.time():
                continue
            if target and target not in node.available_models:
                continue
            if node.active_requests < node.max_concurrent:
                cands.append(node)
        if not cands:
            return None
        min_ratio = min(n.active_requests / n.max_concurrent for n in cands)
        tied = [n for n in cands
                if abs(n.active_requests / n.max_concurrent - min_ratio) < 1e-9]
        if len(tied) == 1:
            return tied[0]
        rr["n"] += 1
        tied.sort(key=lambda n: n.name)
        return tied[rr["n"] % len(tied)]

    return _free_node_for


# --- Fake HTTP: simulate an Ollama call with a delay ------------------------

class FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeAsyncClient:
    """Replaces httpx.AsyncClient in the dispatcher. Configurable behavior."""
    # class-level knobs the tests set
    delay = 0.05
    error_for = set()      # node names that return an Ollama-style error
    raise_for = set()      # node names that raise a transport exception
    calls = []             # (node_host, payload) for inspection

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        host = url.split("//")[1].split(":")[0]
        FakeAsyncClient.calls.append((host, json))
        # find the node object to track concurrency peak
        node = _NODES_BY_HOST.get(host)
        if node:
            node.bump_peak()
        await asyncio.sleep(FakeAsyncClient.delay)
        if host in FakeAsyncClient.raise_for:
            raise httpx.ConnectError("simulated transport failure")
        if host in FakeAsyncClient.error_for:
            return FakeResponse({"error": "simulated ollama error"})
        return FakeResponse({"message": {"content": f"answer from {host}"},
                             "eval_count": 10})


_NODES_BY_HOST = {}


def setup(nodes, cfg=None):
    """Point the dispatcher at a fresh store + fake fleet."""
    global _NODES_BY_HOST
    tmp = tempfile.mkdtemp()
    store = JobStore(db_path=os.path.join(tmp, "disp_test.db"))
    _NODES_BY_HOST = {n.url.split("//")[1].split(":")[0]: n for n in nodes}
    dispatcher.init(cfg or {}, store, nodes,
                    make_free_node_for(nodes), "default-model")
    # swap in fake HTTP
    dispatcher.httpx.AsyncClient = FakeAsyncClient
    FakeAsyncClient.calls = []
    FakeAsyncClient.error_for = set()
    FakeAsyncClient.raise_for = set()
    FakeAsyncClient.delay = 0.05
    return store


async def drain(store, timeout=5.0):
    """Run the dispatcher until no queued/running jobs remain, or timeout."""
    await dispatcher.start()
    t0 = time.time()
    while time.time() - t0 < timeout:
        counts = store.counts_by_status()
        if not counts.get("queued") and not counts.get("running"):
            break
        await asyncio.sleep(0.02)
    await dispatcher.stop()


# --- Tests ------------------------------------------------------------------

async def test_basic_run():
    print("\n[1] jobs run: queued -> succeeded")
    nodes = [FakeNode("n1", ["m"]), FakeNode("n2", ["m"])]
    store = setup(nodes)
    jid = store.create("c", "throughput", "normal",
                       {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                       model="m")
    await drain(store)
    job = store.get(jid)
    check("job succeeded", job["status"] == "succeeded")
    check("result stored", job["result"]["message"]["content"].startswith("answer"))
    check("node recorded", job["node"] in ("n1", "n2"))
    check("started and finished timestamps set",
          job["started_at"] and job["finished_at"])


async def test_fleet_concurrency():
    print("\n[2] work spreads across the fleet, never exceeds slots")
    nodes = [FakeNode("n1", ["m"], 3), FakeNode("n2", ["m"], 3),
             FakeNode("n3", ["m"], 3), FakeNode("n4", ["m"], 3)]
    store = setup(nodes)
    FakeAsyncClient.delay = 0.15  # hold slots long enough to overlap
    for i in range(24):
        store.create("c", "throughput", "normal",
                     {"model": "m", "messages": [{"role": "user", "content": str(i)}]},
                     model="m")
    await drain(store, timeout=10.0)
    counts = store.counts_by_status()
    check("all 24 jobs succeeded", counts.get("succeeded") == 24)
    check("no node ever exceeded max_concurrent",
          all(n.peak <= n.max_concurrent for n in nodes))
    used = [n for n in nodes if n.peak > 0]
    check("work used more than one node", len(used) > 1)
    print(f"       per-node peak concurrency: {{{', '.join(f'{n.name}:{n.peak}' for n in nodes)}}}")


async def test_ordering():
    print("\n[3] class/weight ordering (single slot forces strict order)")
    # One node, one slot, slow calls => strictly serial => dispatch order is
    # observable as completion order.
    nodes = [FakeNode("solo", ["m"], 1)]
    store = setup(nodes)
    FakeAsyncClient.delay = 0.05
    # Submit in deliberately "wrong" order; dispatcher must reorder.
    j_low = store.create("c", "throughput", "low",
                         {"model": "m", "messages": [{"role":"u","content":"low"}]}, model="m")
    time.sleep(0.01)
    j_crit = store.create("c", "throughput", "critical",
                          {"model": "m", "messages": [{"role":"u","content":"crit"}]}, model="m")
    time.sleep(0.01)
    j_inter = store.create("c", "interactive", "normal",
                           {"model": "m", "messages": [{"role":"u","content":"inter"}]}, model="m")
    time.sleep(0.01)
    j_dead = store.create("c", "deadline", "normal",
                          {"model": "m", "messages": [{"role":"u","content":"dead"}]},
                          model="m", deadline_ms=60000)
    await drain(store, timeout=10.0)
    # Reconstruct completion order from finished_at.
    jobs = [store.get(j) for j in (j_low, j_crit, j_inter, j_dead)]
    order = sorted(jobs, key=lambda j: j["finished_at"])
    names = [j["payload"]["messages"][0]["content"] for j in order]
    check("all four ran", all(j["status"] == "succeeded" for j in jobs))
    check("interactive dispatched first", names[0] == "inter")
    check("deadline dispatched second", names[1] == "dead")
    # remaining two are throughput: critical must beat low
    check("critical throughput before low throughput",
          names.index("crit") < names.index("low"))
    print(f"       completion order: {names}")


async def test_cancel_before_run():
    print("\n[4] cancel while queued: never dispatched")
    nodes = [FakeNode("n1", ["m"], 1)]
    store = setup(nodes)
    FakeAsyncClient.delay = 0.2
    # Fill the slot with a blocker, queue a second, cancel the second before
    # the slot frees.
    blocker = store.create("c", "throughput", "normal",
                           {"model": "m", "messages": [{"role":"u","content":"block"}]}, model="m")
    victim = store.create("c", "throughput", "low",
                          {"model": "m", "messages": [{"role":"u","content":"victim"}]}, model="m")
    await dispatcher.start()
    await asyncio.sleep(0.05)          # blocker now running, victim still queued
    ok = store.cancel(victim)
    await drain_existing(store)
    await dispatcher.stop()
    check("cancel of queued job succeeded", ok is True)
    check("victim ended cancelled", store.get(victim)["status"] == "cancelled")
    check("victim never got a node", store.get(victim)["node"] is None)
    check("blocker still succeeded", store.get(blocker)["status"] == "succeeded")


async def drain_existing(store, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        counts = store.counts_by_status()
        if not counts.get("queued") and not counts.get("running"):
            break
        await asyncio.sleep(0.02)


async def test_node_error():
    print("\n[5] node returns error-with-200 => job failed, not stuck")
    nodes = [FakeNode("bad", ["m"], 2)]
    store = setup(nodes)
    FakeAsyncClient.error_for = {"bad"}
    jid = store.create("c", "throughput", "normal",
                       {"model": "m", "messages": [{"role":"u","content":"x"}]}, model="m")
    await drain(store)
    job = store.get(jid)
    check("job marked failed", job["status"] == "failed")
    check("error captured", "ollama error" in (job["error"] or ""))
    check("slot released after failure", nodes[0].active_requests == 0)


async def test_transport_exception():
    print("\n[6] transport exception => job failed, slot released")
    nodes = [FakeNode("dead", ["m"], 2)]
    store = setup(nodes)
    FakeAsyncClient.raise_for = {"dead"}
    jid = store.create("c", "throughput", "normal",
                       {"model": "m", "messages": [{"role":"u","content":"x"}]}, model="m")
    await drain(store)
    job = store.get(jid)
    check("job marked failed", job["status"] == "failed")
    check("slot released", nodes[0].active_requests == 0)


async def test_model_routing():
    print("\n[7] jobs only go to nodes that have their model")
    nodes = [FakeNode("small", ["nemo"], 2), FakeNode("big", ["nemo", "coder"], 2)]
    store = setup(nodes)
    FakeAsyncClient.delay = 0.05
    j_coder = store.create("c", "throughput", "normal",
                           {"model": "coder", "messages": [{"role":"u","content":"c"}]},
                           model="coder")
    await drain(store)
    job = store.get(j_coder)
    check("coder job ran on the only node with coder", job["node"] == "big")
    check("coder job succeeded", job["status"] == "succeeded")


async def test_no_slot_leak():
    print("\n[8] after all work drains, every slot is free")
    nodes = [FakeNode("n1", ["m"], 3), FakeNode("n2", ["m"], 3)]
    store = setup(nodes)
    FakeAsyncClient.delay = 0.02
    for i in range(30):
        store.create("c", "throughput", "normal",
                     {"model": "m", "messages": [{"role":"u","content":str(i)}]}, model="m")
    await drain(store, timeout=10.0)
    check("all nodes back to active_requests=0",
          all(n.active_requests == 0 for n in nodes))
    check("all 30 succeeded", store.counts_by_status().get("succeeded") == 30)


async def main():
    await test_basic_run()
    await test_fleet_concurrency()
    await test_ordering()
    await test_cancel_before_run()
    await test_node_error()
    await test_transport_exception()
    await test_model_routing()
    await test_no_slot_leak()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
