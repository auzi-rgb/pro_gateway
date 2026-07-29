# AI Gateway — Current State

**As of:** 2026-07-23 (sections 1–6); Section 7 added 2026-07-27
**This describes what is actually built and running.** Everything here has been
verified by running it, not assumed.

> Term definitions are canonical in `00-START-HERE.md` (glossary). This doc does not redefine them.

---

## 1. Infrastructure

### Servers

| Host | IP | Role |
|---|---|---|
| ai-app-server | 192.168.44.9 | Gateway (Docker), Open WebUI, load tester, other apps |
| ai-node-01 | 192.168.44.10 | GB10 inference node |
| ai-node-GB10 | 192.168.44.11 | GB10 inference node |
| ai-node-03 | 192.168.44.14 | GB10 inference node |
| ai-node-04 | 192.168.44.12 | GB10 inference node |

All four nodes are identical Dell GB10 hardware, 128 GB unified memory
(reports as ~121 GiB usable). SSH user is `george` on nodes 01/03/04 and
`admin` on ai-node-GB10.

The GB10 uses **unified memory** — there is no separate VRAM pool. CPU and GPU
share the same 128 GB. This matters: model memory estimates compete with
everything else on the box.

### Node configuration (standardized, identical on all four)

`/etc/systemd/system/ollama.service.d/override.conf`

```ini
[Service]
Environment="OLLAMA_MODELS=/usr/share/ollama/.ollama/models"
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=3"
Environment="OLLAMA_MAX_QUEUE=8"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_CONTEXT_LENGTH=32768"
```

**Note the models path.** ai-node-GB10 uses `/var/lib/ollama/models`; nodes
01/03/04 use `/usr/share/ollama/.ollama/models`. Setting the wrong path causes
`mkdir permission denied` and an infinite restart loop. This took down all three
volume nodes once — check the path per node before changing this file.

Model on all four: `mistral-nemo:12b`, pinned warm, ~21.8 GB resident at 32K
context. Cold load is ~50 s; warm requests load in ~0.18 s. `KEEP_ALIVE=-1`
means it never unloads.

ai-node-GB10 also holds larger coder models (qwen-code 30b, mistral-small 24b)
in normal operation, but was temporarily set to the volume-node config for
clean capacity measurement. **It should be restored to a multi-model config**
(`MAX_LOADED_MODELS=3`, no keep-alive) — see remaining work.

### Firewall

Both node types: port 22 open, port 11434 open only from 192.168.44.9,
everything else blocked.

---

## 2. Gateway

**Path:** `~/ai-stack/gateway/` on ai-app-server
**Runs as:** Docker Compose service `fastapi-gateway`
**Restart:** `cd ~/ai-stack && docker compose up -d --force-recreate fastapi-gateway`
(use `--force-recreate`, not `restart`, for env var changes)

**Compile check before restarting** (the gateway dir is not writable by the
normal user, so compile to /tmp):

```bash
python3 -c "import py_compile; py_compile.compile('/home/george/ai-stack/gateway/main.py', cfile='/tmp/gwcheck.pyc', doraise=True); print('COMPILE OK')"
```

### What was changed (v1 session)

**1. Load balancer round-robin fix.** `best_available()` previously used
`min(candidates, key=...)` on load ratio, which returned the first node in list
order whenever nodes tied — and idle nodes always tie at ratio 0. Result: every
request cascaded to the first preferred node (ai-node-GB10), which took 3,125
requests while ai-node-04 took 287.

Fixed by finding the minimum ratio, collecting all nodes tied at it, and
rotating among them with a global `_rr_counter`, sorted by name for determinism.
Distribution is now 26/26/26/23 under load.

**2. Priority scheduler with aging.** Replaced the per-request poll loop in
`/api/chat` (which called `pick_node()` every 0.5 s until a slot freed) with a
real priority queue.

- `QueueTicket` class — tier, model, client, arrival time, asyncio.Event
- `_queue` list + `_queue_lock`
- `dispatcher_loop()` — runs every 100 ms, scores all waiting tickets, sorts
  descending, assigns free slots to the highest scorers, expires timed-out
  tickets
- `acquire_slot(model, client, tier)` — shared function; enqueues, awaits,
  returns `(node, wait_seconds)`. **The returned node already has
  `active_requests` incremented** — the caller must decrement it in a `finally`.
- `_free_node_for(model)` — node selection with the same round-robin tiebreak

Score formula, with aging as a backstop:

```
if waited <= aging_grace_seconds:  score = base_weight
else:                              score = base_weight + (waited - grace) * aging_rate
```

**3. `proxy_request()` gained a `skip_reserve` parameter.** It normally
increments and decrements `active_requests` itself. Since `acquire_slot` already
reserved the slot, the chat handler passes `skip_reserve=True` to avoid
double-counting.

**4. Dispatch instrumentation.** `_dispatch_log` (a 2000-entry deque),
`_dispatch_counts`, `_reject_counts`, and endpoint
`GET /admin/scheduler/dispatch`. Records per dispatch: tier, client, wait
seconds, score, queue depth, queue composition by tier, runner-up, node chosen.
**This is what found every real bug.** Keep it.

**5. New endpoint** `GET /admin/scheduler` — live queue state and config
(public, like the other live-view endpoints).

### Current scheduler config (`config.json`)

```json
"scheduler": {
  "enabled": true,
  "max_queue_depth": 200,
  "dispatch_interval_ms": 100,
  "tiers": {
    "1": {"base_weight": 1000, "aging_rate_per_sec": 1.0,  "aging_grace_seconds": 0,  "timeout_seconds": 60},
    "2": {"base_weight": 500,  "aging_rate_per_sec": 5.0,  "aging_grace_seconds": 20, "timeout_seconds": 120},
    "3": {"base_weight": 100,  "aging_rate_per_sec": 10.0, "aging_grace_seconds": 45, "timeout_seconds": 180}
  }
}
```

### Endpoint conversion status (v1)

| Endpoint | Uses scheduler? |
|---|---|
| `POST /api/chat` | **Yes** — both streaming and non-streaming |
| `POST /api/generate` | No — still old `pick_node` + semaphore |
| `POST /v1/chat/completions` | No — still old path |
| `POST /api/embed`, `/api/embeddings` | No — intentionally; short high-volume requests should not queue behind long generations |

---

## 3. Load tester

**Path:** `~/load-tester/` on ai-app-server
**Port:** 8093 (internal), nginx at `http://192.168.44.9/load-tester/`
**Service:** systemd `load-tester.service`
**Restart:** `sudo systemctl restart load-tester`

### What was added (v1 session)

**Capacity sweep engine** — measures real capacity, not request counts.
- `sweep_one_request()` — streams a request, captures true TTFT (send → first
  chunk carrying content) and real `eval_count`/`eval_duration` from Ollama's
  final chunk
- `sweep_run_level()` — runs one concurrency level to a target completion count,
  with attempt and wall-clock ceilings so a saturated level cannot hang
- Locked benchmark prompt, `temperature: 0`, `seed: 42`, `num_predict: 150`
- Knee detection: highest concurrency where TTFT p95 stays under the SLO
- Endpoints: `POST /api/sweep/start`, `/api/sweep/stop`, `GET /api/sweep/status`

**Capacity panel** (UI, top of page) — presets (Interactive / Throughput /
Stress), duration, and a result card showing sustained throughput, max
concurrent within SLO, and per-node scaling.
- Endpoints: `POST /api/capacity/start`, `/stop`, `GET /api/capacity/status`,
  `GET /api/capacity/presets`
- Polls status every 2 s (does not use the SSE stream, to avoid interfering
  with the existing load-test display)

**Load simulation changes** — worker caps raised 20 → 200; a Response Length
selector (20 / 150 / 400 tokens) that sets `num_predict`; a real generating
prompt (`TEST_PROMPT`) instead of "reply with one word"; ramp interval fixed to
reach full concurrency in the first half of the test; httpx client timeout
raised 90 s → 180 s so it exceeds the gateway's maximum queue wait.

### Important gotchas

- **`tokens_per_sec` must be aggregate**, not per-request. Per-request speed
  *falls* as concurrency rises (streams share the GPU) even while total
  throughput climbs. Aggregate = total tokens / level wall-clock seconds.
- **Error payloads can return HTTP 200.** Ollama returns
  `{"error": "..."}` in the stream body with a 200 status. The harness checks
  for an `error` key mid-stream; without that check, failed requests counted as
  successes and reported zero tokens.

---

## 4. Measured results

### Per node (one GB10, nemo:12b, 32K context, 3 slots)

- Sustained throughput ceiling: **~80 tok/s**
- Per-stream generation speed: ~30 tok/s
- Latency knee (TTFT p95 < 2 s): **2–3 concurrent**

### Full pool (four nodes)

| Concurrency | tokens/sec | TTFT p95 |
|---|---|---|
| 1 | 28 | — |
| 4 | 118 | — |
| 8 | 195 | — |
| 12 | **249** | **779 ms** |
| 16 | 277 | 5,582 ms |
| 20 | 312 | 5,835 ms |

- Peak sustained: **~312 tok/s** (≈3.9× single node — **97% linear scaling**)
- Latency knee: **12 concurrent**
- Each additional GB10 adds ≈ **78 tok/s and ~3 concurrent within SLO**

**This is the business-case number.** Capacity planning is linear: N nodes ≈
N × 80 tok/s.

### Load balancer fix, before and after

| | Before | After |
|---|---|---|
| ai-node-GB10 | 3,125 requests | 26% |
| ai-node-01 | 826 | 26% |
| ai-node-03 | 481 | 26% |
| ai-node-04 | 287 | 23% |

### Scheduler, before and after (identical load: Spike, 60 s, 150 tokens, 10/20/30 workers)

| | No scheduler | With scheduler |
|---|---|---|
| Completed | 164 | 186 |
| Failed | 74 | **0** |
| Error rate | 31.1% | **0%** |

### Priority staircase (Spike, 120 s, 400 tokens, 8/12/16 workers = 36 vs 12 slots)

| Tier | Dispatched | Avg wait | p95 latency |
|---|---|---|---|
| T1 | 74 | **0.74 s** | 19,724 ms |
| T2 | 51 | 19.24 s | 40,009 ms |
| T3 | 16 | 50.93 s | 68,187 ms |

Strict priority ordering confirmed. Aging visibly engages — T3 tickets dispatch
at scores of 567–911 after passing their 45 s grace, beating T2's base of 500.

### Load level calibration

| Total workers | Behavior |
|---|---|
| ~36 | **Useful window** — real contention, scheduler exercised |
| ~60 | No contention (0.05 s waits) — proves nothing |
| ~120 | Admission control saturates, 98% rejections |

---

## 5. Real client usage (from rotated request logs)

Read all rotated files, not just `requests.log` — heavy testing rolls it fast.

| Client | Requests | Last seen | Status |
|---|---|---|---|
| Jira AI Triage Bot | 5,537 | 2026-07-23 | **Active** — highest real volume |
| hiredesk | 2,760 | 2026-07-22 | **Active** |
| openwebui | 947 | 2026-07-17 | **Active** — only true chat client |
| Asset Tracker | 11 | 2026-07-15 | **Active** — city inventory system |
| cloud-reference | 37 | 2026-07-06 | Revoke — AZ-900 study tool, exam passed |
| IT Eval | 55 | 2026-05-14 | Revoke |
| unplanned-intake | 5 | 2026-05-04 | Revoke |
| Markdown Hub | 3 | 2026-04-30 | Revoke |
| webapp-01 | 2 | 2026-04-29 | Revoke |
| Load tester keys (×3), sweep engine | ~55,000 | — | Test traffic; revoke or tag |

---

## 6. Known issues and loose ends (v1 baseline)

> Key-management issues are now documented in depth in `06-operational-notes.md`.

- **Temp key `sweep engine` is still live** — revoke it.
- **Hard routing rules exist in config** (`routing_rules.models` and
  `.clients`) and were observed not reliably pinning traffic. Investigate or
  remove. (Root cause since found: `routing_rules` is consumed only in
  `pick_node`, which `/api/chat` no longer uses — see `04-remaining-work.md`.)
- **Dashboard has no authentication** on the live view (by design) but the
  admin dashboard does.
- **Test traffic pollutes usage stats** — ~55,000 synthetic requests are mixed
  into the same log as real usage.
- **ai-node-GB10 is in volume-node config**, not its intended multi-model role.
- Backups from the v1 session exist as `main.py.bak-*` and `config.json.bak-*`
  in the gateway and load-tester directories.

---

## 7. v2 async job path (built and verified live 2026-07-27)

The asynchronous job path is running in production. `/api/chat` was **not
modified** — this is all additive. Three new modules, wired into `main.py` at
startup.

### 7.1 `jobstore.py` — persistent job store

- SQLite at `/app/jobs.db` (host: `~/ai-stack/gateway/jobs.db`), **WAL mode** so
  poll reads do not block status writes.
- Lifecycle: `queued -> running -> succeeded / failed / rejected / cancelled`.
- Transition guards: every state change is conditional on the current state, so
  a cancel racing a dispatch cannot double-run or move a job backwards.
- `create_batch()` inserts N independent single-inference jobs under one
  `batch_id` in one transaction (the HireDesk shape).
- `requeue_orphans()` on startup moves any `running` job (crash mid-inference)
  back to `queued`.
- `prune()` deletes terminal jobs older than the retention window.
- Columns carry all v2 fields now (`job_class`, `weight`, `capability`,
  `deadline_ms`, `batch_id`) so later steps need no schema migration.
- **Verified:** 47-case test run in the container, all passing.

### 7.2 `jobs_api.py` — async endpoints

Mounted router. Auth reuses `main.py`'s `check_api_key` via injection.

| Endpoint | Purpose |
|---|---|
| `POST /api/jobs` | Submit one job; returns id immediately |
| `POST /api/jobs/batch` | Submit N jobs (`items:[...]`) under one batch_id |
| `GET /api/jobs/{id}` | Poll one job's status and result |
| `GET /api/jobs/batch/{batch_id}` | Poll a whole batch with a status rollup |
| `DELETE /api/jobs/{id}` | Cancel (also the caller-gave-up case) |
| `GET /api/jobs` | Counts by status (cheap health number) |

- Class/weight/capability/deadline_ms are read from the request body
  (client-stated at this stage) and validated against the allowed vocab; junk
  returns 400. Defaults when omitted: `class=throughput`, `weight=normal`.
- `deadline` class requires `deadline_ms`; `deadline_ms` on any other class is
  rejected.
- A prune task runs on a timer (default every 300 s, retention 86400 s), both
  configurable via a `jobs` block in `config.json`.
- **Verified:** 40-case end-to-end test (TestClient) in the container, plus a
  live submit/poll against the running gateway.

### 7.3 `dispatcher.py` — async execution engine

- A **separate loop** from the chat scheduler. Cooperates only through the shared
  `node.active_requests` counter; no shared lock. Slot reservation is done in one
  synchronous stretch (no `await` between "is it free?" and the increment) so the
  two loops cannot double-book a slot.
- Reuses `main.py`'s `_free_node_for()` for node selection (least-loaded,
  round-robin among ties) — so model-aware routing and health/circuit filtering
  come for free and stay consistent with the chat path.
- Ordering: `interactive` first, then `deadline` by earliest deadline, then
  `throughput` by weight then arrival, with an **aging backstop on `throughput`
  only** (config: `aging_grace_seconds` default 30, `aging_rate_per_sec` default
  10).
- Each job runs as its own task, so many run concurrently across the fleet. Node
  errors (including Ollama's error-with-HTTP-200) and transport exceptions both
  mark the job failed and release the slot in a `finally`.
- **Verified:** 24-case test (ordering `['inter','dead','crit','low']`,
  fleet-spread to peak 3/3/3/3 without exceeding slots, cancel race, error
  handling, no slot leak). Live end-to-end: a submitted job dispatched in ~47 ms,
  ran on ai-node-03, result retrieved by poll.

### 7.4 Config blocks (optional; defaults apply if absent)

```json
"jobs": {
  "retention_seconds": 86400,
  "prune_interval_seconds": 300
},
"dispatcher": {
  "interval_ms": 100,
  "aging_grace_seconds": 30.0,
  "aging_rate_per_sec": 10.0,
  "node_http_timeout_seconds": 300.0
}
```

### 7.5 Startup wiring (in `main.py` `startup()`, after nodes populated)

```python
jobs_api.init(CONFIG, check_api_key=check_api_key)
await jobs_api.start_prune_task()
dispatcher.init(CONFIG, jobs_api.STORE, nodes, _free_node_for, DEFAULT_MODEL)
await dispatcher.start()
```

Backups from this work: `main.py.bak-jobsapi-*`, `main.py.bak-dispatcher-*`.
