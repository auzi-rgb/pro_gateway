# AI Gateway — Operational Notes and Known Issues

> Term definitions are canonical in `00-START-HERE.md` (glossary). This doc does not redefine them.

Real operational findings, confirmed live. These are the hard-won details good
internal docs exist to capture. Newest concerns first.

---

## Key management (as-is behavior — important for cutover)

The gateway currently loads API keys from **two sources**, and they behave
differently on revoke. Understanding this is essential before the v2 key
migration.

### How keys load today

In `main.py`, at startup, `API_KEYS` is built in this order:

1. Keys stored in `config.json` under `api_keys` (dashboard-created; the full
   secret is stored in the file).
2. Keys from environment variables `GATEWAY_API_KEY_*`, mapped to client names
   by `_ENV_KEY_MAP`. **These are applied second and win** on any name clash.

### Finding 1 — dashboard "revoke" is a placebo for env-var keys (confirmed live)

`/admin/config/keys/revoke` deletes the key from the in-memory dict and from
`config.json`. It **cannot** touch the environment. So on the next
`--force-recreate`, `startup()` re-reads the environment and any env-var key
comes back. Confirmed live on 2026-07-27: after revoking, stale clients
(`Markdown Hub`, `IT Eval`, `unplanned-intake`) were still present because they
are env-var keys.

Dashboard-created keys (config-only) **do** revoke permanently — verified live:
a dashboard key returned `Invalid API key` immediately after revoke.

**Consequence for cutover:** "revoke all keys" means editing `.env` and
`docker-compose.yml` to remove the `GATEWAY_API_KEY_*` entries too, *then*
restarting — not just clicking revoke in the dashboard. A dashboard-only revoke
will silently leave env keys live.

### Finding 2 — duplicate HireDesk clients (confirmed live)

`/admin/clients` shows both `HireDesk` (tier 1) and `hiredesk` (tier 2) as
separate clients — case-mismatched duplicates from the two key sources. They are
treated as two different clients at two different tiers, which muddies usage
stats and makes "which key is HireDesk using" ambiguous. Resolve at cutover by
collapsing to one clean key per client.

### Finding 3 — a dead key mapping (confirmed live)

`docker-compose.yml` passes `GATEWAY_API_KEY_T`, but `_ENV_KEY_MAP` has no `T`
entry, so that env var is **never loaded** into `API_KEYS`. It is present in the
environment (length 43) but the gateway is blind to it. Same class of bug as the
v1 mislabeled-keys problem: keys defined in three places (compose, `.env`, the
code map) that do not agree.

### Decision — hashed key store (`keystore.py` BUILT; activation at cutover)

The fix: stop storing API-key *secrets* anywhere; store only their **hashes** in
a dedicated `keys.db`. One source, final revoke. Built and tested (42 cases),
not yet activated — activation replaces `check_api_key` on the auth hot path, so
it is a clean hard-swap done at the cutover, not mid-week.

- Secrets are never in a plaintext file (only irreversible hashes stored). The
  secret is shown once at creation and is never recoverable.
- **Revoke is real and final** — deleting the hash row means the key can never
  authenticate again; no env layer to resurrect it. Verified in tests including
  "stays dead across store reopen."
- Full management from the gateway (create / list / revoke / update), which the
  Settings UI and the cutover reissue will call.

**SHA-256, not bcrypt** (note: user passwords use bcrypt — API keys should not).
Passwords are low-entropy and need a deliberately slow hash to resist brute
force. API keys are 256-bit random tokens — uncrackable regardless of hash speed
— and are checked on *every request* on the hot path, where bcrypt's ~100ms
would be a pure latency tax for zero benefit. A fast hash is both secure for
high-entropy input and correct for the hot path. Do not "upgrade" this to bcrypt.

**Keys carry an `allowed_classes` SET, not a single class.** Class is a property
of the work, not the key — one app can do multiple kinds of work. HireDesk grades
resumes (throughput) and chats about them (interactive), so its key allows
`{interactive, throughput}`. The request declares its class; the gateway checks
membership and transport (strict pairing). Weight and capability stay single per
key (HireDesk = `high`/24b for everything). Integrity relies on apps declaring
class correctly — enforced by the integration guide's class-declaration spec, not
by code; a mis-declared class can only reorder the app's own work because weight
is fixed on the key.

Infrastructure secrets (JWT signing secret, SMTP password) **stay** in the
environment — set once at deploy, not managed through the app. The line:
infrastructure secrets in env, application API keys as hashed rows in the DB.
This eliminates key-management Findings 1–3 in one move (dual-source drift is
their common root cause).

Trade-off: an existing key's secret can never be displayed again, only its prefix.
A lost key is reissued, not recovered — the point of an unrecoverable secret, but
a behavior change from today's system, which can display full keys.

---

## Node configuration reminders (from v1, still true)

- Model blob path differs per node: `ai-node-GB10` uses
  `/var/lib/ollama/models`; nodes 01/03/04 use
  `/usr/share/ollama/.ollama/models`. Setting the wrong path causes a
  permission-denied restart loop. Check per node before editing
  `override.conf`.
- Always use explicit version tags (`mistral-nemo:12b`), never `:latest` —
  mismatched tags fragment the pool because the gateway routes by exact name.
- `ai-node-GB10` should be restored to its multi-model role
  (`MAX_LOADED_MODELS=3`, no `KEEP_ALIVE`) once capacity testing is finished;
  keep `OLLAMA_MODELS=/var/lib/ollama/models` on that node.

## Deploy mechanics

- `main.py` and the v2 modules live in `~/ai-stack/gateway/` on the host, which
  is bind-mounted to `/app` in the container (`./gateway:/app`). So new `.py`
  files appear in the container without an image rebuild; a
  `--force-recreate` restart picks them up.
- Compile-check before restart (gateway dir is not writable as the normal user,
  so compile to /tmp):
  `python3 -c "import py_compile; py_compile.compile('/home/george/ai-stack/gateway/main.py', cfile='/tmp/gwcheck.pyc', doraise=True); print('OK')"`
- `jobs.db` lives at `/app/jobs.db` (host: `~/ai-stack/gateway/jobs.db`) with
  WAL sidecars `-wal` / `-shm`. Being on the bind-mounted host path is what makes
  jobs survive `--force-recreate`.
