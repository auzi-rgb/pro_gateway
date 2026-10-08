# AI Gateway — knowledge base article

**Audience:** Georgetown IT staff and developers building applications that use
the city's language models.
**System owner:** Austin Madison
**Last reviewed:** 2026-10-08

---

## What it is

The AI Gateway is a single internal endpoint that city applications call to use
the city's language models. It sits in front of four GPU appliances and decides
which one handles each request.

Applications do not talk to the GPU appliances directly. They talk to the
gateway, and the gateway handles the rest.

**Why it exists:**

| Without a gateway | With the gateway |
|---|---|
| Every app hard-codes a server address | One address, forever |
| An appliance goes offline and apps break | Traffic moves to a healthy one automatically |
| One busy app slows everyone down | Work is prioritised by importance |
| No record of who used what | Every request is attributed to a key |

---

## Where it runs

| | |
|---|---|
| Host | `ai-app-server` (192.168.44.9) |
| Port | 8000 |
| Runs as | Docker Compose service `fastapi-gateway` |
| Behind it | Four NVIDIA GB10 appliances running Ollama |
| Dashboard | `/dashboard` (sign-in required) |
| Live view | `/live` (no sign-in) |

All of it is on the internal network. Nothing is exposed to the internet, and
no prompt or response leaves city infrastructure.

---

## Getting access

Request an **API key** from the system owner. Keys are issued per application,
not per person, so say which application it is for and roughly how it will be
used.

Each key carries two settings that decide how its traffic is handled. You do
not set these per request — they are fixed on the key when it is issued.

### Class — how the work is delivered

| Class | Use it for | How it behaves |
|---|---|---|
| `interactive` | A person is waiting on screen | Answered immediately, in arrival order |
| `deadline` | Work that must finish by a set time | Queued, soonest deadline served first |
| `throughput` | Bulk work nobody is waiting on | Queued, served by weight |

### Weight — who goes first when there is a queue

`critical` · `high` · `normal` · `low`

**Weight only affects `throughput` work.** Interactive requests are served in
the order they arrive and deadline requests by whichever is due soonest, so a
`critical` key gets no advantage in those classes. This surprises people, so it
is worth repeating: weight is a tiebreaker for bulk work, not a fast pass.

Low-weight work cannot be starved indefinitely. Roughly every two seconds the
longest-waiting low-weight job is given first claim on the next free slot,
regardless of what else is queued.

---

## Calling it

Authenticate with an `Authorization: Bearer <your-key>` header on every
request. No other header format is accepted.

### Interactive — a person is waiting

`POST /api/chat`

```bash
curl -X POST http://192.168.44.9:8000/api/chat \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "gemma4:12b",
        "messages": [{"role": "user", "content": "Summarise this permit application."}],
        "stream": true
      }'
```

Set `"stream": true` to receive the answer token by token as it is generated,
which is what makes a chat interface feel responsive. With `false` you wait for
the whole answer.

Two response headers are worth reading:

- `x-node` — which appliance served it
- `x-queue-wait-ms` — how long it waited for a free slot before any work began

If a request took 12 seconds and `x-queue-wait-ms` says 9000, the system was
busy, not slow. That distinction matters when troubleshooting.

### Background work — nobody is waiting

Submit the job, get an id back immediately, poll for the result.

```bash
# submit
curl -X POST http://192.168.44.9:8000/api/jobs \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemma4:12b",
       "messages":[{"role":"user","content":"Classify these 400 records."}],
       "class":"throughput"}'
# -> {"id": "abc123"}

# poll
curl http://192.168.44.9:8000/api/jobs/abc123 -H "Authorization: Bearer YOUR_KEY"
```

The job record includes `submitted_at`, `started_at` and `finished_at`, so you
can see how much of the elapsed time was queueing.

`POST /api/jobs/batch` submits several at once. `DELETE /api/jobs/{id}` cancels
one that has not started.

### OpenAI-compatible

`POST /v1/chat/completions` and `GET /v1/models` are available for tools that
expect the OpenAI format. Useful for off-the-shelf software that cannot be
changed.

### Other endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/tags` | Models available across the fleet |
| `GET /api/ps` | What is currently loaded, and memory in use |
| `POST /api/embed` | Vector embeddings |
| `GET /health` | Liveness check, no authentication |
| `GET /admin/status` | Fleet summary, no authentication |

---

## Reading the responses

### 200 — fine

### 401 / 403 — your key
401 means no key was sent, 403 means the key was not recognised or has been
revoked. Check the `Authorization` header is present and spelled `Bearer `.

### 503 — the gateway turned the request away

**This is not a fault.** Admission control refuses work it does not believe it
can complete in reasonable time, rather than accepting it and being slow for
everyone. The response body explains why, for example:

```json
{"detail": "estimated wait 3.2s exceeds interactive target 2.0s"}
```

**What to do:** retry after a short delay, with backoff. If it happens often,
the work probably belongs in the `throughput` class as a background job rather
than an interactive request. Talk to the system owner about re-issuing the key.

### 500 — something failed downstream
Report it with the time and the `x-node` header value if you have it.

---

## Common questions

**Which model should I use?**
The fleet runs several. Each one has a report card — accuracy by category,
speed, memory use — produced by Crucible, the companion load tester. Ask the
system owner for the current one rather than guessing from the model name.

**Why is my request slow?**
Check `x-queue-wait-ms`. If it is small, the model is simply generating a long
answer, and shortening your prompt or lowering the response length will help.
If it is large, the fleet is busy and your work may belong in a lower-priority
class.

**Why did I get an answer with no text in it?**
Some models reason before answering, and that reasoning counts against the
response length limit. Too small a limit and the model spends it all thinking
and returns nothing. Raise the limit, or ask the system owner whether the model
supports answering directly.

**Can I pick which appliance serves me?**
No, and you should not want to. The gateway picks the least loaded healthy one.

**Is my data sent anywhere?**
No. Prompts and responses stay on city hardware.

---

## For whoever supports this

**Restart** — use `--force-recreate`, not `restart`, or environment changes are
not picked up:

```bash
cd ~/ai-stack && docker compose up -d --force-recreate fastapi-gateway
```

**Check syntax before restarting.** The gateway directory is not writable by a
normal user, so compile to `/tmp`:

```bash
python3 -c "import py_compile; py_compile.compile('/home/george/ai-stack/gateway/main.py', cfile='/tmp/gwcheck.pyc', doraise=True); print('COMPILE OK')"
```

**Health checks** run every 30 seconds against each appliance's `/api/tags`. A
failing appliance is marked unhealthy and taken out of rotation automatically,
and an alert email is sent after a configured number of consecutive failures.
It is returned to rotation when it recovers.

**Keys** are managed from the dashboard, or through `/admin/config/keys`.

> ⚠️ **The key cutover is a one-way event.** While the key store is empty, the
> gateway falls back to the older environment-variable key and logs a CRITICAL
> warning. The moment the first key is created in the store, that fallback
> stops for **every client at once**. Plan it; do not discover it. The same
> thing happens in reverse if the last key is ever revoked.

**Logs:** `docker compose logs -f fastapi-gateway`

---

## Related

- **Crucible** — the load tester and model benchmark that measures this
  gateway. Separate article.
- Design documents live in the `pro_gateway` repository under `docs/`.
