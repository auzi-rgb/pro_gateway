# AI Gateway v2 — Design Specification

**Status:** Draft for review
**Author:** Georgetown IT
**Supersedes:** the 3-tier (T1/T2/T3) priority model

---

## 1. Why this exists

The gateway is the thing that makes a fleet of GB10 nodes useful. One node can
serve today's load. The argument for buying more nodes only holds if the gateway
distributes work well, protects the requests that matter, and degrades
predictably under pressure.

The first version used three priority tiers (T1/T2/T3). Load testing showed the
model was solving the wrong problem: it assumed every request was chat-like and
that priority was a single dimension. Neither is true here. Most traffic is
batch AI-assist from internal web apps, and "importance" turned out to have two
independent parts — what the request needs technically, and who is asking.

---

## 2. Two dimensions, not one

### 2.1 Class — what makes the result worthless

Class is an engineering property. It answers: *if this request is slow, what
breaks?* Each class has a different failure behavior, not just a different
position in line.

| Class | Result becomes worthless when | Behavior under load |
|---|---|---|
| `interactive` | The first token is slow — a human is watching | Fails fast. Sub-second queue target. Better to say "busy" than to make someone stare at a cursor. |
| `deadline` | A wall-clock deadline passes | Admission-checked. If the gateway estimates it cannot finish in time, it is rejected **immediately** so the caller can skip cleanly. |
| `throughput` | Never — it is only ever slower | Never rejected for waiting, no timeout. Waits as long as the queue requires. |

**Why `deadline` is its own class.** The Jira triage bot must comment on a new
ticket before a human picks it up. Nobody is watching a spinner, so it is not
interactive; but a comment that lands after assignment is useless, so it is not
throughput either. A late result is worse than no result, which is a distinct
failure mode and deserves a distinct class.

### 2.2 Weight — who is asking

Weight is a policy property, set per API key. It orders requests *within* a
class. A director's inventory lookup outranks a background enrichment job of the
same class; it never jumps an interactive chat request, because those are
different classes with different failure modes.

| Weight | Meaning |
|---|---|
| `critical` | Named leadership / city-critical workflow |
| `high` | Department-level business system |
| `normal` | Default for internal apps |
| `low` | Experiments, learning tools, non-essential |

**Governance line:** class is engineering, weight is policy. Class is chosen by
whoever builds the app based on how it fails. Weight is assigned by IT and
changes require approval.

---

## 3. Synchronous vs asynchronous

The timeout problems in v1 existed for one reason: every request held an open
HTTP connection while it waited. A batch job waiting 90 seconds looked like a
failure because the client socket gave up, not because the gateway failed.

v2 splits the transport:

| Path | Endpoint | Used by |
|---|---|---|
| Synchronous | `POST /api/chat` (existing) | `interactive` only |
| Asynchronous | `POST /api/jobs`, `GET /api/jobs/{id}` | `deadline`, `throughput` |

An async submission returns immediately with a job ID. The app polls for the
result. There is no connection to time out, so a queued job cannot fail merely
for waiting.

Keys carry their class, and the gateway enforces the pairing: an async-class key
calling `/api/chat` is refused, and vice versa. This prevents the failure mode
where a batch app accidentally uses the interactive path and gets rejected under
load.

### 3.1 Job lifecycle

```
submitted -> queued -> running -> succeeded
                                \-> failed
                                \-> rejected (admission)
```

Jobs persist in SQLite (alongside the existing users DB) so a gateway restart
does not strand in-flight work. Completed jobs are retained for a configurable
window, then pruned.

---

## 4. Admission control

Admission decides at arrival, when rejecting is cheap. Once admitted, a request
is guaranteed — the only thing that removes it is the caller disconnecting or
cancelling.

**Wait estimate.** Queue wait is predictable in aggregate: pending output tokens
divided by measured token throughput. The gateway tracks both continuously —
pending work from the queue, throughput from recent completions — so the
estimate adapts instead of relying on a hardcoded constant.

**Per class:**

- `interactive` — rejected if the estimated wait exceeds its short target
  (default 2s). Fails fast by design.
- `deadline` — rejected if the estimated wait plus expected generation time
  exceeds the request's deadline. The caller learns in milliseconds that it
  should skip this one.
- `throughput` — admitted unless the queue exceeds a global safety ceiling.
  Never rejected for expected slowness.

**Global ceiling.** A maximum total queue depth exists purely to stop unbounded
growth. Hitting it is an operational signal that capacity is short, and it
should be visible on the dashboard.

---

## 5. Dispatch

A single dispatcher assigns free node slots. Ordering:

1. `interactive` first, always. These have the tightest budget and the smallest
   volume.
2. `deadline` next, ordered by *closest deadline first*, so the most urgent
   goes next rather than the earliest-arrived.
3. `throughput` last, ordered by weight, then by arrival.

**Starvation guard.** A `throughput` request that has waited beyond a configured
threshold is promoted so it cannot be indefinitely blocked by a stream of
higher-class work. This is a backstop, not routine fairness: it only engages
after an unusually long wait, so normal ordering stays strict.

Node selection is unchanged from v1: least-loaded, round-robin among ties. That
already produces even distribution across the fleet.

---

## 6. What gets measured

Every dispatch records class, weight, client, wait time, queue composition, and
the node chosen. This instrumentation is what caught the bugs in v1 and stays.

Dashboard surfaces:

- Queue depth by class, live
- Wait time distribution per class
- Admission rejections, with reason
- Node distribution
- Estimated vs actual wait, so the estimator can be checked against reality

---

## 7. Migration

1. Revoke all existing keys.
2. Reissue with explicit class + weight.
3. Publish the integration guide so app owners can update their calls.

**Client mapping:**

| Client | Class | Weight | Notes |
|---|---|---|---|
| Open WebUI | `interactive` | normal | Only true chat client |
| Jira Triage Bot | `deadline` | high | ~90s deadline; must beat a human to the ticket |
| HireDesk | `throughput` | normal | Bursty — ~40 inferences per job |
| Asset Tracker | TBD | high | City inventory system; class depends on whether AI blocks a user action |
| cloud-reference | — | — | Revoke; study tool, no longer needed |
| IT Eval, unplanned-intake, Markdown Hub, webapp-01 | — | — | Revoke; unused since spring |
| Load tester keys | `throughput` | low | Test traffic, tagged so it can be filtered from usage reports |

---

## 8. Open questions

- Asset Tracker's class — does its AI call block something a user is doing?
- Retention window for completed jobs.
- Whether `interactive` should ever queue at all, or always fail fast when no
  slot is free.
- Whether HireDesk should submit 40 jobs or one job containing 40 items.
