# AI Gateway — Research and Reasoning

**Why the design changed.** This document records the reasoning behind v2 so
that decisions do not get re-litigated from scratch, and so the parts that came
from evidence can be told apart from the parts that were judgment calls.

---

## 1. The original problem

Four GB10 nodes had just replaced an unequal mixed setup. A live test showed
traffic split 65% / 32% / ~0% / ~0% across nodes that were now identical
hardware. The stated goals were: make routing spec-aware, redefine tiers, and
produce a capacity number that could support a purchasing argument.

Two of those three goals survived contact with the data. The third — "spec-aware
routing where nodes self-report capabilities" — turned out to be unnecessary,
because once the nodes were configured identically the routing problem was a
tiebreak bug, not a lack of information about the nodes.

---

## 2. What the measurements found

Each of these was found by instrumenting and looking, usually after a confident
theory turned out to be wrong. That pattern is the most important thing to carry
forward.

**The load balancer was cascading, not balancing.** `min()` on load ratio
returns the first item when everything ties, and idle nodes always tie at zero.
Every request went to the first node in the preferred list until it filled, then
spilled to the next. The distribution 3125 / 826 / 481 / 287 is the signature of
a waterfall, not a balancer.

**Node configuration had drifted.** ai-node-GB10 was missing
`OLLAMA_NUM_PARALLEL` entirely, so it served requests strictly one at a time —
a second concurrent request waited ~15 s for the first to finish. The nodes were
"identical hardware" but not identical software.

**Context length, not parallelism, was the memory constraint.** Raising
`NUM_PARALLEL` to 6 caused Ollama to refuse the model: it estimated 141.8 GiB
required against 132.5 GiB available. The cause was nemo's default context
window (262,144 tokens) multiplied across parallel slots. Capping context to
32K dropped the footprint from 113 GB to 27.5 GB with no loss of usable
capability — 32K is roughly 24,000 words, far beyond what these apps send.

**Model naming fragmented the pool.** Three nodes had `mistral-nemo:latest`
while one had `mistral-nemo:12b`. Ollama treats these as different models, and
the gateway routes by exact name, so no single model name could reach all four
nodes. Explicit version tags everywhere; never `:latest`.

**The API keys were mislabeled, invalidating three rounds of tuning.** The load
tester's "T1" key did not exist in the gateway config, its "T2" field held a
tier-3 key, and its "T3" key was also missing. Every scheduler conclusion drawn
before this was discovered had been measured on traffic whose labels did not
match its actual tiers. The dispatch instrumentation caught it: lifetime
dispatch counts showed `{'2': 167, '3': 22}` — tier 1 had never appeared at all.

**Latency alone can lie.** One sweep produced a beautiful, smooth, linear TTFT
curve. It was measuring how fast the node returned an out-of-memory error.
Tokens/sec was zero throughout, which is what exposed it. Always cross-check a
latency metric against a throughput metric.

---

## 3. Capacity: how the number was defined

Request-per-minute is not a capacity metric — it depends entirely on prompt and
response length. The honest unit is **sustained tokens/sec at a fixed
concurrency, while p95 time-to-first-token stays under an SLO.**

- TTFT over total response time, because for streaming output it is what a
  waiting human actually feels, and total time is dominated by response length,
  which the gateway does not control.
- p95 over mean, because it catches real degradation while ignoring single
  outliers. This is how SaaS platforms state latency objectives.
- A fixed benchmark prompt with `temperature: 0`, a fixed seed, and a capped
  `num_predict`, so runs are comparable.
- A warmup request, discarded, so cold-load time does not pollute steady-state
  numbers.
- **Aggregate** tokens/sec (total tokens ÷ wall-clock), not the average of
  per-request speeds. Per-request speed falls as concurrency rises because
  streams share the GPU; total throughput still climbs. Measuring the wrong one
  makes scaling look negative.

Result: ~80 tok/s per node, ~312 tok/s across four, 97% linear scaling, knee at
12 concurrent. Linear scaling is the actual argument for buying more nodes.

---

## 4. Why the three-tier model was wrong

The v1 scheduler worked. Under identical load it took failures from 31.1% to
zero and produced a clean priority staircase. It is not being replaced because
it failed; it is being replaced because it answers a question that does not
match the workload.

**Three tiers assumed every request was chat-like** — differing only in how
important it was and how long it could wait. The real traffic is:

- One chat client (Open WebUI, small IT group)
- A bot that must comment on a new ticket before a human picks it up
- Batch apps that upload 40 resumes and wait minutes
- An inventory system whose AI usage pattern is still unclear

These fail in different ways, not at different speeds. A slow chat response is
annoying. A late ticket comment is *worthless* — worse than none, because it
arrives after someone already took the ticket. A slow resume batch is fine.

**Tuning could not fix this, and three rounds proved it.** Every attempt to make
priority ordering strict starved the middle tier; every attempt to protect the
middle tier let the bottom tier starve. The problem was that one dimension was
being asked to express two independent things: how urgent the work is, and who
is asking for it.

**Timeouts are the wrong instrument.** A timeout asks "has this waited too
long?" but the useful question is "will this ever be served?" A request waiting
65 s in a draining queue is fine; the same request in a growing queue is doomed.
Timeouts treat them identically and kill the first one unnecessarily. Raising
the timeout just moves the cliff.

---

## 5. What the research said

Sources: queueing-theory treatments of LLM inference, vLLM and QLM scheduling
literature, Kubernetes priority-class guidance, commercial batch/queue APIs,
and the async request-reply pattern as documented by Microsoft and others.

**Three tiers is the industry norm, and the split matches.** Enterprise LLM
traffic is commonly divided into interactive work where a human waits,
non-interactive work that is important but can take minutes, and scheduled batch
that runs on idle capacity. Kubernetes guidance recommends three to five
priority tiers with large gaps between them — too many tiers create confusion.
So the instinct to use three was sound; the mistake was in what the three
represented.

**Aging is standard, and the "out-of-order scheduling limit" framing is better
than raw rates.** Hardware schedulers use age to prevent starvation from
out-of-order scheduling, with a configurable limit on how far a request can be
jumped — a tighter limit for latency-sensitive classes than for best-effort
ones. Capping displacement is cleaner than letting scores cross freely, which is
what caused the middle-tier starvation.

**Queue wait time is predictably estimable.** Dividing pending output tokens by
measured token throughput estimates wait accurately in aggregate; the
relationship between wait and queue position is linear with R² of 0.99. This is
what makes admission control feasible rather than guesswork — the estimate can
be computed from live state.

**Admission control should be based on token cost, not request count.** A single
very long prompt can consume more compute than hundreds of short ones. Counting
queue depth in requests is a poor proxy for load when request sizes vary as much
as they do here (a 40-resume batch versus a one-line chat message).

**Separate pools for interactive versus batch is a named, standard pattern.**
This is the direct precedent for the class split.

**The async job pattern is the accepted answer for long-running inference.**
Holding an HTTP connection ties up threads, risks gateway timeouts, and gives a
poor user experience. The convention is to accept, enqueue, return 202 with a
job ID quickly, and let the client poll. The commonly cited heuristic:
synchronous under ~10 seconds, async with polling under an hour. Several vendors
ship this as a parallel "queue API" mirroring their synchronous endpoints.

**This is the insight that dissolves the timeout problem.** The failures being
tuned against existed *only* because requests held open connections while
waiting. A batch job that returns a job ID immediately has no socket to time
out, so a queued job cannot fail merely for waiting. Hours of timeout tuning
were treating a symptom of the transport, not a scheduling problem.

**Per-client fairness within a priority level is a known refinement.** vLLM
numbers a client's simultaneous requests so one heavy user cannot monopolize a
priority level while a single request from another user retains its place. Not
implemented here yet; worth adding once multiple apps share a class.

---

## 6. The two-dimension model, and why

Two independent properties were being forced into one number.

**Class — what makes the result worthless.** Technical. Determines *behavior*,
not just position: whether the request fails fast, is admission-checked against
a deadline, or waits indefinitely.

**Weight — who is asking.** Organizational. Orders requests within a class.
A director's simple lookup and a clerk's simple lookup are technically
identical; only policy separates them. No amount of queueing theory decides
this, and pretending it is a technical property makes the config dishonest.

Keeping them separate gives a governance story that is easy to defend: **class
is engineering, weight is policy.** App developers choose class based on how
their app fails. IT assigns weight, and changes to it can require approval.

**Why `deadline` earns its own class rather than being a flag on `throughput`.**
Today the Jira bot is the only client with a deadline, and mechanically the
behavior is one admission check either way. It is a separate class for two
reasons: earliest-deadline-first ordering becomes available later if more such
clients appear, and — more immediately — three named classes with visibly
different behaviors is something a developer can be handed, whereas "throughput
class but set this one field" is a footnote people will miss. Apps that race a
human, fire on a webhook, or feed a time-sensitive workflow should land in the
right class by default.

---

## 7. Judgment calls that could reasonably go the other way

Recorded honestly, because they are policy rather than findings.

- **`interactive` always outranks everything.** Defensible because chat volume
  is small and its budget is tightest, but it means a critical-weight batch job
  never beats a routine chat message. If the organization disagrees, weight
  would need to cross class boundaries, which is considerably more complex.
- **32K context.** Generous for current apps, but a genuine cap. Long-document
  RAG would need a different node profile.
- **No preemption.** Aging changes order, never interrupts running inference.
  Preemption mid-generation is not practical here.
- **The GB300 comparison cannot be made from these numbers.** Vendor figures
  (20 PFLOPS FP4, trillion-parameter capacity) are capability claims at a
  quantization not in use here, not serving-throughput claims for a 12B model
  at concurrency. The defensible argument is not "8 GB10s beat 1 GB300"; it is
  "our workload is concurrent serving of models under ~30B, which shards
  perfectly — capacity scales linearly at 97% efficiency, measured, and here is
  what each added node buys." Cost figures decide the rest.
