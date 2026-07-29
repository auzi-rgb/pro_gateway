# CHANGELOG — AI Gateway

A timeline of what was **tried, what broke, and what we use now**. Each entry is
the short arc; the full reasoning lives in `02-research-and-reasoning.md`. Newest
first.

Format for lessons: **Tried → Problem → Changed to → Verified.** Simple additions
that involved no reversal are marked **Added**.

"Verified live" = deployed on `ai-app-server` and exercised against the running
gateway. v1 entries marked *(reconstructed)* were written after the fact from the
handoff record.

---

## 2026-07-27 — admission control + key store foundation

### Added — admission control (`admission.py`) — LIVE
Per-class rejection at arrival: interactive fails fast over its wait target,
deadline rejects when unmeetable, throughput only at the global ceiling. The wait
estimate uses token throughput MEASURED LIVE from real completions (the dispatcher
feeds each finished job's tokens+duration in), seeded from a persisted
last-known-good across restarts, with only a divide-by-zero floor as a true
constant. Self-healing by design: a node failing/recovering or the model changing
moves the measured number on its own, so admission tracks reality with no config
edit. 30 isolated + 11 integration tests, verified live.

### Lesson — throughput input: measured, not hardcoded
- **Tried (considered):** a hardcoded throughput constant (~312 tok/s) for the
  wait estimate.
- **Problem:** a frozen constant is a claim that rots — it lies the moment a node
  drops, the fleet gets busy, or the model changes (and a 24b switch is already
  planned). It would fail silently, promising deadlines the fleet can't meet.
- **Changed to:** live measurement from recent completions, seeded from persisted
  last-known-good. No frozen assumption; self-correcting.
- **Verified:** tests show the same deadline job admits on a healthy fleet and
  rejects when measured throughput halves — no config change.

### Added — hashed key store (`keystore.py`) — BUILT, not activated
SHA-256 hashed keys in a dedicated `keys.db`, single source, final revoke (no
resurrection). Activation replaces `check_api_key` on the auth hot path, so it is
held for the cutover. 42 tests, verified on server.

### Lesson — class belongs to the work, not the key
- **Tried:** the original v2 design gave each key ONE class.
- **Problem:** a real app can do multiple kinds of work — HireDesk grades resumes
  (throughput) AND chats about them (interactive). One class per key can't express
  that; strict pairing would block one of HireDesk's two functions.
- **Changed to:** a key carries an `allowed_classes` SET; the request declares its
  class; the gateway checks membership + transport. Weight/capability stay single
  per key. Integrity via the integration guide's class-declaration spec (a
  mis-declared class only reorders the app's own work, since weight is fixed on
  the key).
- **Verified:** keystore tests prove HireDesk's `{interactive, throughput}` key
  works on both endpoints and is refused on wrong transport / ungranted class.

### Lesson — SHA-256 for keys, not bcrypt (which the gateway uses for passwords)
- **Tried (considered):** reuse bcrypt for consistency with user passwords.
- **Problem:** passwords are low-entropy and need a slow hash; API keys are
  256-bit random tokens (uncrackable regardless of speed) checked on every
  request — bcrypt would tax the hot path ~100ms for zero benefit.
- **Changed to:** SHA-256 (fast, secure for high-entropy input). Standard for
  API keys.

---

## 2026-07-27 — v2 async path

### Added — persistent job store (`jobstore.py`) — LIVE
SQLite at `/app/jobs.db` in WAL mode, with a guarded lifecycle
(`queued -> running -> succeeded/failed/rejected/cancelled`), batch insert, prune,
and orphan recovery on restart. Verified with 47 isolated tests in the container.

### Added — async endpoints (`jobs_api.py`) — LIVE
`POST /api/jobs`, `/api/jobs/batch`, `GET /api/jobs/{id}`,
`GET /api/jobs/batch/{id}`, `DELETE /api/jobs/{id}`, `GET /api/jobs`. Class and
weight are read from the request body and validated. Verified with 40 end-to-end
tests plus a live submit/poll. `/api/chat` untouched.

### Added — async dispatcher (`dispatcher.py`) — LIVE
Runs queued jobs across the fleet in class order (interactive -> deadline ->
throughput), weight within class, aging backstop on throughput only. A separate
loop from the chat scheduler, coordinating only through the shared
`node.active_requests` counter. Verified with 24 tests and a live end-to-end job
(dispatched in ~47 ms, ran on ai-node-03, result retrieved).

### Lesson — three-tier priority (T1/T2/T3)
- **Tried:** a single three-tier priority scheduler for all traffic.
- **Problem:** it worked, but solved the wrong problem — it assumed every request
  was chat-like, differing only in importance. Real traffic differs in *how it
  fails* (a late ticket comment is worthless; a slow batch is fine), which one
  priority dimension cannot express. Tuning starved either the middle or bottom
  tier; three rounds proved it.
- **Changed to:** two dimensions — **class** (how it fails -> behavior) and
  **weight** (who is asking -> order within class).
- **Verified:** class-ordered dispatch confirmed live (`inter, dead, crit, low`).
- -> `02-research-and-reasoning.md` sections 4, 6.

### Lesson — queue timeouts
- **Tried:** timeouts on queued requests to bound waiting.
- **Problem:** timeouts existed only because requests held an open HTTP
  connection while waiting; the socket gave up, not the gateway. Raising the
  timeout just moved the cliff. It answered "waited too long?" when the real
  question is "will this ever be served?"
- **Changed to:** the **async job pattern** — submit returns a job id immediately,
  the client polls, there is no connection to time out, so a queued job cannot
  fail merely for waiting.
- **Verified:** jobs sit queued indefinitely with no failures; dispatched when a
  slot frees.
- -> `02-research-and-reasoning.md` section 5.

### Lesson — key storage / revoke (identified, fix pending)
- **Tried:** API keys loaded from two sources (env vars + `config.json`).
- **Problem:** dashboard "revoke" only touches `config.json` and memory, so
  env-var keys resurrect on restart — a placebo. Also produced a duplicate
  `HireDesk`/`hiredesk` client and a dead `GATEWAY_API_KEY_T` mapping. Confirmed
  live.
- **Changed to (planned, step 2c):** store key **hashes** in the DB, single
  source, like user passwords. Revoke becomes final; the three bugs disappear.
- **Verified:** dashboard-created key revoke confirmed final live; env-key
  resurrection confirmed live. Full fix not yet built.
- -> `06-operational-notes.md`.

---

## Prior to 2026-07-27 — v1 baseline *(reconstructed)*

### Lesson — load balancing
- **Tried:** `min()` on each node's load ratio to pick the least-loaded node.
- **Problem:** `min()` returns the first item on a tie, and idle nodes all tie at
  ratio 0 — so every request cascaded to the first node until it filled, then
  spilled. Distribution was 3125 / 826 / 481 / 287, a waterfall not a balancer.
- **Changed to:** collect all nodes tied at the minimum ratio and rotate among
  them with a round-robin counter, sorted by name for determinism.
- **Verified:** distribution 26/26/26/23 under load.
- -> `02-research-and-reasoning.md` section 2.

### Lesson — node parallelism
- **Tried:** assumed the four "identical hardware" nodes were configured
  identically.
- **Problem:** `ai-node-GB10` was missing `OLLAMA_NUM_PARALLEL`, so it served
  requests strictly one at a time — a second concurrent request waited ~15 s.
  Identical hardware, non-identical software.
- **Changed to:** standardized `override.conf` across all four nodes.
- **Verified:** even concurrency behavior across the fleet.

### Lesson — memory constraint
- **Tried:** raising `NUM_PARALLEL` to 6 for more concurrency.
- **Problem:** Ollama refused the model — estimated 141.8 GiB against 132.5 GiB
  available. The cause was nemo's default 262K context multiplied across parallel
  slots, not parallelism itself.
- **Changed to:** cap `OLLAMA_CONTEXT_LENGTH` to 32K (~24k words, far beyond what
  these apps send). Footprint dropped from 113 GB to 27.5 GB.
- **Verified:** three parallel slots fit comfortably per node.

### Lesson — model naming
- **Tried:** routing by model name across the fleet.
- **Problem:** three nodes had `mistral-nemo:latest`, one had `mistral-nemo:12b`.
  Ollama treats these as different models and the gateway routes by exact name, so
  no single name reached all four nodes.
- **Changed to:** explicit version tags everywhere; never `:latest`.
- **Verified:** one model name reaches all four nodes.

### Lesson — mislabeled API keys
- **Tried:** tuning the scheduler using the load tester's "T1/T2/T3" keys.
- **Problem:** the keys were mislabeled — the tester's "T1" key did not exist in
  the gateway, "T2" held a tier-3 key, "T3" was missing. Three rounds of tuning
  were measured on traffic whose labels did not match its tiers. The dispatch
  instrumentation caught it (`{'2':167,'3':22}` — tier 1 never appeared).
- **Changed to:** corrected key labels; kept the dispatch instrumentation as a
  permanent check.
- **Verified:** dispatch counts matched intended tiers.
- **Lasting rule:** keys defined in multiple places drift — a recurring theme
  (see the 2026-07-27 key lesson). Consolidate to one source.

### Lesson — a latency metric that lied
- **Tried:** reading a smooth, linear TTFT curve from one sweep as a good result.
- **Problem:** it was measuring how fast the node returned an out-of-memory error;
  tokens/sec was zero throughout.
- **Changed to:** always cross-check a latency metric against a throughput metric;
  detect Ollama's error-with-HTTP-200 in the stream body.
- **Verified:** the harness now flags error-in-stream and reports aggregate
  throughput.
- -> `02-research-and-reasoning.md` sections 2, 3.

### Added — capacity as a defined number
Sustained tokens/sec at fixed concurrency while TTFT p95 stays under the SLO, with
a fixed benchmark prompt, temperature 0, fixed seed, capped `num_predict`, warmup
discarded, aggregate (not per-request) throughput. Result: ~80 tok/s per node,
~312 across four, 97% linear scaling, knee at 12 concurrent. This is the
business-case number. -> `02-research-and-reasoning.md` section 3.
