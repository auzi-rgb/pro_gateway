# AI Gateway — Architecture and Module Map

> Term definitions are canonical in `00-START-HERE.md` (glossary). This doc does not redefine them.

**Purpose:** explain how the code is organized and *why*, so the structure does
not have to be reverse-engineered. Read this before adding to the gateway.

---

## The one principle

**Build the best gateway we can, then tell apps how to use it — not the other
way around.** The gateway is the instrument everything else conforms to. This
decides many smaller calls: apps request a *capability*, not a hardcoded model
name; settings are visible, never silently hardcoded; and a feature that does
not work is removed rather than left to mislead.

---

## Why the code is split into modules

`main.py` is large (~1,400 lines). Adding the whole v2 async path inline would
push it past 2,000 and make it fragile: a syntax error anywhere takes down the
gateway, and no piece can be tested without loading all of it.

Instead the async path is split by *concern* — one module per thing that changes
for its own reason:

| Module | Owns | Knows nothing about |
|---|---|---|
| `jobstore.py` | How jobs are **persisted** (SQLite, WAL, lifecycle, prune) | HTTP, scheduling, nodes |
| `jobs_api.py` | How jobs are **submitted and polled** (HTTP routes, validation) | SQLite internals, dispatch |
| `dispatcher.py` | How queued jobs get **run** (ordering, slot reservation, inference) | HTTP routing contracts, the submit API |
| `main.py` | The gateway itself: nodes, health, chat path, admin, and wiring the above together | — |

This separation is what let each piece be tested in isolation, without starting
the gateway, before it was wired in. That is the concrete payoff, not ceremony:
`jobstore.py` was proven with 47 tests, `jobs_api.py` with 40, `dispatcher.py`
with 24 — all against fakes, none touching production.

## Dependency direction

```
main.py  ──imports──>  jobs_api.py  ──imports──>  jobstore.py
   │                                                  ▲
   └──imports──>  dispatcher.py  ──imports───────────┘
```

`main.py` imports everything; nothing imports `main.py`. Modules that need live
objects from `main.py` (the `nodes` list, `_free_node_for`, `DEFAULT_MODEL`, the
`check_api_key` function) receive them through an `init(...)` call at startup —
**dependency injection, not import** — which is what avoids a circular import.
This is why `jobs_api.init()` and `dispatcher.init()` exist and are called inside
`startup()` after the nodes are populated.

## Two schedulers, one slot counter

There are currently **two** dispatch loops:

1. The **v1 chat scheduler** (`acquire_slot` / `dispatcher_loop` in `main.py`),
   serving `/api/chat` synchronously. Proven, carries production traffic.
2. The **v2 async dispatcher** (`dispatcher.py`), serving `/api/jobs`.

They are deliberately separate (see decision log below). They cooperate through
exactly one shared thing: `node.active_requests`, the per-node in-flight slot
counter. They do **not** share a lock.

The race that this could create — both loops grabbing the same free slot — is
avoided because Python asyncio is single-threaded: the check ("is this node
free?") and the reserve (`active_requests += 1`) happen in one synchronous
stretch with no `await` between them, so neither loop can interleave with the
other mid-decision. `dispatcher._reserve_slot()` is written specifically to
preserve this; do not insert an `await` between the node selection and the
increment.

## The async job lifecycle

```
POST /api/jobs ──> stored 'queued'
                        │
              dispatcher picks it (class/weight order)
                        │
                   reserve a node slot
                        │
                   mark 'running'
                        │
              replay payload to node (Ollama)
                        │
        ┌───────────────┼────────────────┐
   'succeeded'      'failed'         (cancelled)
   result stored   error stored     result discarded
                        │
              slot released (always, in finally)
```

Persistence means a gateway restart does not strand work: on startup,
`requeue_orphans()` moves any job left `running` (from a crash mid-inference)
back to `queued` so the dispatcher runs it again. Safe because inference here is
idempotent — replaying a payload just regenerates the answer.

---

## Decision log (why, not just what)

- **Two dimensions, class and weight.** Class is an engineering property (what
  makes the result worthless: `interactive` fails fast, `deadline` is
  admission-checked, `throughput` waits). Weight is a policy property (who is
  asking), ordering requests *within* a class. Class is engineering, weight is
  policy — see `03-gateway-v2-design.md` §2 and `02-research-and-reasoning.md`.
- **Weight never crosses class.** A critical-weight throughput job never beats a
  routine interactive one. Crossing class would collapse the two dimensions back
  into the single priority score that v2 exists to escape. If leadership needs a
  workflow to beat chat, reclassify that workflow (give it a `deadline`), do not
  globalize weight.
- **Interactive-first is safe *because* interactive fails fast.** Serving chat
  ahead of batch only starves batch if the interactive queue stays non-empty;
  admission control rejects excess interactive rather than letting it pile up.
  The two are one decision, not two.
- **HireDesk: batch-submit-of-singles.** `POST /api/jobs/batch` enqueues N
  independent single-inference jobs under one `batch_id`. The app makes one call
  and polls one batch; the dispatcher still sees N independent units it can
  spread across the fleet. No multi-item execution path in the dispatcher.
- **Async dispatcher separate from the chat scheduler (for now).** Keeps the
  proven chat path untouched while the async engine is proven, and localizes any
  fault to new code. The unified single-scheduler end state is a later
  refinement; the slot coordination that makes it possible is already shared.
- **Capability aliases (planned, not yet built).** Apps should request
  `capability: standard | high` rather than a raw model name, so routing
  decouples from which model lives on which node. This is what will make the
  heterogeneous-model topology decision (24b vs nemo) a config change rather than
  an app change. The `capability` column already exists in the job store.
