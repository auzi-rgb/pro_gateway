# AI Gateway — Remaining Work

> Term definitions are canonical in `00-START-HERE.md` (glossary). This doc does not redefine them.

**Updated 2026-07-27.** Completed items moved to the top (kept, not deleted, so
the history reads). Remaining work follows, ordered roughly by dependency.

---

## DONE — verified live 2026-07-27

- **2a. SQLite job store** (`jobstore.py`) — persistent, WAL, transition guards,
  batch insert, prune, orphan recovery. 47 tests passing.
- **2b. Async endpoints** (`jobs_api.py`) — submit, batch, poll, batch-poll,
  cancel, summary. Body-driven class/weight with validation. 40 tests passing +
  live submit/poll.
- **2e (dispatch half). Class-based dispatcher** (`dispatcher.py`) — interactive
  → deadline (earliest) → throughput (weight, arrival, FIFO within a tier).
  Separate loop, shares the slot counter with the chat scheduler.
  24 tests passing + live end-to-end job on real Ollama. **2026-08-28:** the
  original score-based aging backstop was found broken by load-tester
  validation (tuple ordering meant it could never actually promote `low`
  across a weight tier) and replaced with a fixed, bounded reservation —
  see `01-current-state.md` §7.3 for the full writeup.
- **2d. Admission control** (`admission.py`) — LIVE. Rejects at arrival per class
  (interactive fails fast over its wait target; deadline rejects when unmeetable;
  throughput only at the global ceiling). Self-healing: throughput is MEASURED
  live from real completions (dispatcher feeds it), seeded from persisted
  last-known-good across restarts, so estimates track a failing/recovering fleet
  or a model change with no config edit. 30 + 11 tests + live. Two knobs
  (`interactive_max_wait_s`, `global_queue_ceiling`) currently at safe code
  defaults — to be surfaced in the Settings UI (§5), per the "no hidden settings"
  rule; deliberately NOT hand-written into config.json.
- **2c foundation. Hashed key store** (`keystore.py`) — BUILT + tested (42),
  verified on server, NOT yet activated (activation = cutover; see §2c below).
- **2c wiring. `check_api_key` rewired onto the key store** (`gwauth.py`) —
  LIVE 2026-07-29. Restart clean on all 4 nodes; curl smoke tests confirmed 401
  (no key), 403 (invalid key), and the CRITICAL empty-keystore log line. Real
  traffic still authenticates via the legacy fallback since `keys.db` is empty —
  wiring is live but the cutover (§2f) has not happened yet. See §2c below.

The synchronous `/api/chat` path was not modified; production traffic was
undisturbed.

---

## 0. Housekeeping (still to do — small, some are hygiene risks)

- **Revoke stale keys** `cloud-reference`, `IT Eval`, `unplanned-intake`,
  `Markdown Hub`, `webapp-01`, and any leftover load-tester/sweep keys.
  **Note:** for env-var keys this means editing `.env` and `docker-compose.yml`,
  not just the dashboard — see `06-operational-notes.md`, Finding 1.
- **Resolve the `HireDesk` / `hiredesk` duplicate** (two clients, two tiers).
  Handled naturally by the key migration.
- **Restore ai-node-GB10 to its multi-model role** (`MAX_LOADED_MODELS=3`, no
  `KEEP_ALIVE`, keep `OLLAMA_MODELS=/var/lib/ollama/models`).
- **Investigate / remove the hard routing rules.** `routing_rules` is consumed
  only in `pick_node`, which `/api/chat` no longer uses — so the rules never
  affected chat traffic. Rebuild routing in the scheduler/dispatch path where
  traffic actually flows, or remove the dead config. A feature that silently does
  nothing is worse than none.
- **Add a regression test for the `low`-weight starvation guard** in
  `dispatcher.py` (2026-08-28 fix). The existing 24-case suite never exercised
  sustained overload, which is exactly how the previous aging bug shipped
  undetected. A test should assert `low` jobs still get dispatched (on the
  `low_reserve_every_seconds` cadence) under continuous higher-weight arrivals,
  and that they never out-rank a currently-queued higher-weight job.
- **Separate test traffic from usage stats** — tag test clients or log them to a
  separate file.

---

## 1. Finish v1 conversions (optional, low cost)

Only `/api/chat` uses the v1 scheduler. If continuing to lean on the sync path
before full v2 cutover:
- Convert `POST /api/generate` and `POST /v1/chat/completions` to `acquire_slot`.
- Leave `/api/embed` / `/api/embeddings` unscheduled.

(Lower priority now that the async path exists — most batch traffic should move
to `/api/jobs` rather than the sync endpoints.)

---

## 2. Gateway v2 — remaining pieces

### 2c. Hashed key store with allowed-class sets + weight — CLOSED

**Foundation BUILT and tested (`keystore.py`, 42 tests). `check_api_key` is now
rewired onto it (`gwauth.py`) and verified LIVE 2026-07-29** — restart clean on
all 4 nodes, curl confirmed 401/403/CRITICAL-empty-keystore. The empty-keystore
fallback means this wiring did NOT double as the cutover: real traffic is still
authenticating via the legacy env/config path since `keys.db` is empty. The
cutover — populating `keys.db` and reissuing keys — is §2f, a separate, still
human-run event (see below).

Key model (revised from the original single-class design):

- **Keys are stored as SHA-256 hashes** in a dedicated `keys.db`, never as
  plaintext in config/env. Secret shown once at creation, never recoverable.
  Revoke deletes the row — final, no resurrection. This is the single source of
  truth that eliminates the three key-management findings in `06`.
  (SHA-256 not bcrypt: keys are 256-bit random tokens, uncrackable regardless of
  hash speed, and checked on every request — a slow hash would tax the hot path
  for no benefit. Rationale in `keystore.py` header.)
- **A key carries an `allowed_classes` SET, not a single class.** Class is a
  property of the *work*, not the key: one app can do multiple kinds of work.
  HireDesk grades resumes (throughput, async) AND chats about them (interactive,
  sync), so its key allows `{interactive, throughput}`. Single-behavior apps get
  a one-element set. The request declares its class; the gateway checks it is in
  the key's allowed set.
- **Weight is single per key** — "who is asking" does not change with the kind of
  work. **Capability is single per key** — e.g. HireDesk = `high` (24b) for
  everything it does. (HireDesk's batch grading therefore also pulls the 24b — a
  data point for the topology decision in §7.)
- **Strict endpoint/class pairing** (`endpoint_class_ok`): the declared class
  must be in the key's allowed set AND match the endpoint's transport
  (interactive → sync `/api/chat`; deadline/throughput → async `/api/jobs`).
  Enforced from day one.
- **Integrity depends on correct client declaration.** Because the request states
  its own class, mixed apps must declare correctly — enforced by policy, not
  code: the app integration guide (§3) MUST include a class-declaration spec /
  prompt for mixed apps. Lying cannot gain cross-client priority (weight is fixed
  on the key), so a mis-declared class only reorders the app's own work.

**Done for 2c:**
- ~~Rewire `check_api_key` to look up the keystore~~ — LIVE (`gwauth.py`).
  **Option 3 chosen:** hard swap with an empty-keystore safety net (loud error /
  one-time fallback rather than silent total lockout) and easy rollback via the
  `main.py` backup. Confirmed on server.

**Still to build (before §2f can happen):**
- Enforce `endpoint_class_ok` in the `/api/chat` and `/api/jobs*` handlers — not
  yet called anywhere.
- Admin key-management endpoints: create (returns secret once), list (prefix +
  metadata, never the secret), revoke, update. These are what the future
  Settings UI and the cutover reissue will call. **This is the next build.**

### 2f. Key migration (cutover) — NEXT HUMAN-RUN EVENT

With `check_api_key` already rewired onto the key store (2c, live), this is now
the only remaining step to full v2 auth: revoke all, reissue with class + weight
per the mapping in `03-gateway-v2-design.md`. Remember env-var keys need
removing from `.env`/compose too. Run once the admin key endpoints (2c) exist
and `endpoint_class_ok` is enforced, on a planned outage window — the moment
real keys land in `keys.db`, the legacy fallback stops being exercised and old
env/config keys stop working.

---

## 3. Client-side work

- **App integration guide** — how to call the gateway per class, submit async
  jobs, poll, handle rejection, set client-side timeouts. Include a prompt app
  owners can hand to Claude to update their app. This is the "tell apps how to use
  the gateway" deliverable.
- **Update the Jira Triage Bot** — first async pilot (`deadline` class, ~90 s).
- **Update HireDesk** to async (`throughput`, batch-submit-of-singles).
- **Classify Asset Tracker.** By its current description (a chatbot a person
  waits on) it is `interactive` / `high`, but class is reversible so confirm by
  how the app actually calls the model.

---

## 4. Load tester

- Per-tier / per-class workload types (Chat 50/150, Summarize 2000/200, Analyze
  1500/400, Extract 800/40, RAG 4000/250) so one run simulates a realistic mix.
- Queue-depth visualization — chart queue depth and per-class waits over time.
- Update the tester for v2 classes and the async path.

---

## 5. Documentation and UI

- **Settings / About page** (read-only first) showing every scheduler/dispatcher
  parameter with a plain-English explanation. Standing rule: never hardcode a
  setting; every setting visible.
- **Capacity summary for management** — headline numbers only (what the cluster
  delivers now, what each added node adds). Not a before/after demo.
- **Surface admission-control rejections on the dashboard** (reason + count per
  class). Blocked on fixing `jobstore.py`'s currently-unreachable
  `mark_rejected`/`STATUS_REJECTED` path first — `jobs_api.py` rejects with a
  bare `HTTPException` before a job row is ever created, so there's nothing to
  query yet.

---

## 6. Open questions

- **Asset Tracker's class** — confirm interactive vs throughput by how it calls
  the model.
- **Job granularity for HireDesk** — batch-submit-of-singles is the chosen shape;
  confirm HireDesk does not need in-order or all-or-nothing semantics.
- **Should `interactive` ever queue**, or always fail fast when no slot is free?
  (Tied to admission control — decide with 2d.)
- **Retention window** for completed jobs (currently 24 h default).
- **Per-client fairness within a class** (vLLM-style request numbering) — add
  once several apps share a class.

---

## 7. Longer term

- **Model topology decision (24b vs nemo).** The fleet is currently nemo
  everywhere. Standardizing on 24b (better quality) means fewer slots per node —
  a quality-vs-capacity trade to settle by *measuring* (run the 24b capacity
  sweep) against *observed demand* (what fraction of traffic needs 24b). Capability
  aliases (planned in 2c) let apps request `standard`/`high` without hardcoding a
  model, so topology becomes a config change, not an app change. Any model change
  means re-running the capacity sweep — the numbers are model-specific.
- A fourth bulk class for very large, delay-tolerant work, if that appears.
- Unify the two dispatch loops into one scheduler once the async path is proven
  (the slot coordination is already shared).
- Revisit context length if long-document RAG becomes a requirement.
