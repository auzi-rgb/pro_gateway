# 00 — START HERE

**The single entry point for the AI Gateway project.** Read this first. It tells
you what the system is, what state it is in, what not to break, what is already
decided, how work is done here, and where to look for detail. It replaces the
older handoff README.

**System:** self-hosted AI inference gateway (FastAPI, Docker) on `ai-app-server`
(192.168.44.9), proxying to four Dell GB10 nodes running Ollama.
**Goal:** demonstrate that a gateway makes a fleet of GB10 nodes viable, so more
can be purchased. The gateway and nodes are a pilot not yet fully endorsed by IT
management.
**Current phase:** v2 async path + admission control **built and verified live**.
The hashed key store is **built and tested but not activated** — activating it is
the cutover. Next build: rewire `check_api_key` onto the key store (the last
backend piece; high-risk, auth hot path). See the status ledger and work-in-progress.

---

## How to read this project

- These `.md` files are the shared context. Read `00` (this file) first, then open
  the specific doc you need from the **file map** below — you should not need to
  read all of them.
- **Ground-truth rule:** docs describe what is *verified live*. The code on the
  server is the real state. **When they disagree, trust the server**, and
  **instrument before theorizing** — every significant bug in this project was
  found by measuring, not reasoning, several after a confident theory was wrong.
  This is the core working discipline; keep it.
- **If you are an AI assistant picking this up:** you have no memory of prior
  sessions — everything you need is in these files and on the server. Do not
  assume a capability or a piece of state exists; check the server. Prefer small,
  independently testable changes. Do not modify the live `/api/chat` path (see
  Do-not-break). When you finish a piece, update the status ledger and the
  changelog in the same step, so the docs never drift from reality.

---

## Status ledger

State is one of: **LIVE** (deployed and exercised on the server), **BUILT**
(written and unit-tested, not yet deployed), **DESIGNED** (specified only).

| Component | State | Where | Verified by |
|---|---|---|---|
| Load-balancer round-robin fix | LIVE | `main.py` `best_available` / `_free_node_for` | distribution 26/26/26/23 under load |
| v1 priority scheduler + aging (`/api/chat`) | LIVE | `main.py` `acquire_slot`, `dispatcher_loop` | 0% error rate under load; priority staircase |
| Capacity-measurement harness | LIVE | `~/load-tester/` | ~312 tok/s across 4 nodes, 97% linear |
| Job store | LIVE | `jobstore.py` (`/app/jobs.db`, WAL) | 47-case test in container |
| Async endpoints | LIVE | `jobs_api.py` (`/api/jobs*`) | 40-case test + live submit/poll |
| Async dispatcher (class/weight ordering) | LIVE | `dispatcher.py` | 24-case test + live end-to-end job |
| Admission control (self-healing, live throughput) | LIVE | `admission.py` | 30 + 11 tests + live |
| Hashed key store (`allowed_classes` set, strict pairing) | BUILT | `keystore.py` | 42-case test + verified on server; NOT activated |
| `check_api_key` rewiring + admin key endpoints | DESIGNED | (step 2c activation = cutover) | — |
| Capability aliases (`standard`/`high`) | DESIGNED | column exists in stores | — |
| Client migration (revoke/reissue) | DESIGNED | (step 2f, cutover) | — |
| Unify the two dispatch loops | DESIGNED | longer-term | — |

---

## Do-not-break

- **`/api/chat` is live production traffic** (Open WebUI and HireDesk). It uses
  the v1 scheduler. The entire v2 async path is *additive* and must stay that way
  until an explicit cutover. Do not modify the chat path while building v2.
- **The shared slot counter `node.active_requests`** is the one thing the chat
  scheduler and the async dispatcher share. Reserve a slot (`+= 1`) in the same
  synchronous stretch as the free-check, with **no `await` between them**, or the
  two loops can double-book a node. See `05-architecture.md`.
- **Explicit model tags only** (`mistral-nemo:12b`), never `:latest` — mismatched
  tags fragment the pool because routing is by exact name.
- **Per-node model blob path differs** — `ai-node-GB10` uses
  `/var/lib/ollama/models`; nodes 01/03/04 use `/usr/share/ollama/.ollama/models`.
  Wrong path = permission-denied restart loop.

---

## Settled decisions (do not re-litigate — reasoning in the pointed-to doc)

- **Two dimensions: class and weight.** Class = engineering (what makes a result
  worthless → behavior). Weight = policy (who is asking → order within a class).
  → `03-gateway-v2-design.md` §2, `02-research-and-reasoning.md` §6.
- **Weight never crosses class.** Reclassify a workflow (e.g. give it a deadline)
  rather than globalizing weight. → `05-architecture.md` decision log.
- **Interactive-first is safe because interactive fails fast.** One decision, not
  two. → `05-architecture.md`.
- **HireDesk = batch-submit-of-singles.** One submission, N independent jobs, no
  multi-item execution path in the dispatcher. → `05-architecture.md`.
- **Async dispatcher is a separate loop from the chat scheduler (for now).**
  Keeps the proven chat path untouched; unify later. → `05-architecture.md`.
- **API keys: SHA-256 hashes in a `keys.db`, single source, final revoke.** Keys
  carry an `allowed_classes` SET (one app can do multiple kinds of work — e.g.
  HireDesk = `{interactive, throughput}`); weight and capability single per key.
  Strict endpoint/class pairing. SHA-256 not bcrypt (high-entropy keys, hot path).
  Built (`keystore.py`); activation is the cutover. → `06-operational-notes.md`,
  `04-remaining-work.md` §2c.
- **Capability aliases over hardcoded model names.** Lets topology change without
  app changes. → `05-architecture.md`, `04-remaining-work.md` §7.
- **Capacity is measured in sustained tok/s at fixed concurrency under an SLO**,
  not requests/min. → `02-research-and-reasoning.md` §3.

---

## How work is done here

- **Build in a sandbox, prove in isolation, then wire in.** Each v2 module was
  written and tested against fakes before touching the gateway (job store 47
  tests, endpoints 40, dispatcher 24). Keep doing this.
- **Patch `main.py` with a guarded script**, never by hand: backup →
  match exact anchors → compile to `/tmp` → only swap in if it compiles. Backups
  are `main.py.bak-*`. Review the `diff` before restarting.
- **Deploy mechanics:** `~/ai-stack/gateway/` is bind-mounted to `/app` in the
  container (`./gateway:/app`), so new `.py` files appear without an image
  rebuild. Reload with:
  `cd ~/ai-stack && docker compose up -d --force-recreate fastapi-gateway`
  (use `--force-recreate`, not `restart`, for env/code changes).
- **Run module tests in the container** (same Python the gateway uses):
  `docker compose exec fastapi-gateway python3 /app/test_<module>.py`
- **Config:** never hardcode a setting; every setting should be visible and live
  in `config.json` with a sensible default in code.

---

## File map — which doc answers which question

| Question | Read |
|---|---|
| What is this, what state is it in, what's next | `00-START-HERE.md` (this file) |
| Exactly what is built and running, configs, numbers | `01-current-state.md` |
| *Why* the design is the way it is; research; what tests proved | `02-research-and-reasoning.md` |
| The target v2 architecture (class/weight, async, admission) | `03-gateway-v2-design.md` |
| What's done and what's left, ordered, with open questions | `04-remaining-work.md` |
| How the code is organized and why (module map, decision log) | `05-architecture.md` |
| Known issues and operational gotchas (esp. keys, deploy) | `06-operational-notes.md` |
| The timeline of what was tried, what broke, what we use now | `CHANGELOG.md` |

---

## Glossary (canonical — other docs point here; define terms only here)

- **Admission control** — deciding at arrival whether to accept a request, rather
  than accepting it and failing later. Cheap to reject at arrival, expensive after
  a long wait. (v2, not yet built.)
- **Aging backstop** — increasing a waiting request's effective priority after it
  has waited past a grace period, so it cannot be starved forever. A backstop, not
  routine fairness: normal ordering stays strict until the grace elapses. Applied
  to `throughput` only.
- **Capability alias** — an app requests `capability: standard | high` instead of
  a raw model name; the gateway resolves it to whatever model is deployed for that
  tier. Decouples apps from topology. (Planned; column exists.)
- **Class** — the technical property of a request: what makes its result
  worthless. One of `interactive` (a human waits — fail fast), `deadline` (a
  wall-clock deadline — admission-checked), `throughput` (only ever slower — never
  rejected for waiting). Class determines *behavior*.
- **Cutover** — the planned outage during which all keys are revoked and reissued
  with class + weight, moving clients onto v2.
- **Dispatcher** — the loop (`dispatcher.py`) that pulls queued jobs, orders them
  by class then weight, reserves node slots, runs the inference, stores results.
  Separate from the v1 chat scheduler.
- **Job** — an async unit of work: submitted to `/api/jobs`, stored in SQLite,
  queued, dispatched, polled for a result. Persisted so a restart does not strand
  it.
- **Knee** — the highest concurrency at which the system still meets its SLO. Past
  it, latency climbs sharply while throughput stops improving. Measured at 12
  concurrent for the four-node pool.
- **Node** — one Dell GB10 running Ollama. Four: ai-node-01 (.10),
  ai-node-GB10 (.11), ai-node-03 (.14), ai-node-04 (.12). 128 GB unified memory.
- **Orphan recovery** — on startup, any job left `running` (a crash mid-inference)
  is moved back to `queued` so the dispatcher runs it again. Safe because
  inference is idempotent here.
- **Slot** — one concurrent in-flight request on one node. Each node = 3, pool =
  12. Tracked by `node.active_requests`, shared by both dispatch loops.
- **SLO** — service level objective; the acceptable/too-slow line. Here: TTFT p95
  under 2 seconds.
- **Sustained throughput** — total tokens/sec the pool produces at saturation.
  ~312 tok/s across four nodes, ~80 per node.
- **TTFT** — time to first token. What a waiting human feels; the metric that
  matters for chat.
- **Weight** — the organizational property of a request: who is asking. Orders
  requests *within* a class. `critical` / `high` / `normal` / `low`. Set by IT;
  never crosses class boundaries.

---

## Work in progress

> **When nothing is mid-build, this section reads: "No work in progress; system
> is in steady state."** Otherwise it names the current piece and where to resume.

**Current:** v2 async path + admission control are complete and live. The hashed
key store (`keystore.py`, `allowed_classes` set, strict pairing) is built, tested
(42), and verified on the server — but NOT activated.

**Next build: rewire `check_api_key` onto the key store (step 2c activation).**
This is the last backend piece and the HIGHEST-RISK one — it is on the auth hot
path (every request, including live `/api/chat`), so a bug is a total lockout, not
a quiet failure. Do it in a FRESH session, at full attention, ideally on the
cutover window.

Plan (agreed):
- Build the new `check_api_key` in isolation with a test harness; it hashes the
  bearer token, looks up `keystore`, returns `allowed_classes` + weight +
  capability, and enforces `endpoint_class_ok` in the `/api/chat` and `/api/jobs*`
  handlers (strict pairing from day one).
- **Option 3 swap:** hard replacement of the old env/config key path, with an
  empty-keystore safety net (loud error / one-time fallback instead of silent
  lockout) and rollback via the `main.py` backup.
- Add admin key endpoints (create/list/revoke/update) — used by the cutover
  reissue and the future Settings UI.
- **Activation = cutover:** because it is a clean replacement, the moment it goes
  live all old keys stop working. So activation and the key migration (§2f) are
  the same event — done on the planned outage day, keys reissued with correct
  `allowed_classes`/weight/capability (HireDesk = `{interactive, throughput}`,
  weight normal, capability high).

Spec: `04-remaining-work.md` §2c, `06-operational-notes.md` (key decision),
`keystore.py` header. After this: cutover (§2f), then the big UI update (Settings
page first), then client app updates + the integration guide (must include the
class-declaration spec for mixed apps).
