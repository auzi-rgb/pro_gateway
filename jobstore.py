"""
jobstore.py — persistent job store for the AI Gateway async path (v2).

Standalone module. Owns its own SQLite database (/app/jobs.db by default),
separate from the users DB. Safe to import and exercise in isolation before
it is wired into main.py.

Job lifecycle (from 03-gateway-v2-design.md):

    queued -> running -> succeeded
                      \\-> failed
                      \\-> rejected   (admission control refused it)
                      \\-> cancelled  (DELETE, or caller disconnected/gave up)

The store is deliberately dumb: it records state and returns rows. It makes
no scheduling decisions and knows nothing about nodes, classes, or weights
beyond storing them. The dispatcher and admission control (built later) are
the only things that interpret those fields.

Concurrency: WAL mode is enabled so poll reads (GET /api/jobs/{id}) do not
block behind status writes from the dispatcher. All three future callers
(submit endpoints, dispatcher loop, poll endpoints) share one process and
will hit this DB concurrently.
"""

import json
import time
import uuid
import sqlite3
from contextlib import contextmanager

# --- Status constants -------------------------------------------------------
# Use these everywhere rather than bare strings so a typo is an ImportError,
# not a silently-never-matching status.

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"

# States from which a job can still be cancelled by the caller.
CANCELLABLE = (STATUS_QUEUED, STATUS_RUNNING)

# States that mean the job is finished and eligible for pruning.
TERMINAL = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_REJECTED, STATUS_CANCELLED)


class JobStore:
    """
    Thin wrapper over a SQLite file. One instance per process.

    Every public method opens and closes its own connection via the get_conn()
    context manager. That is the same pattern main.py uses for the users DB and
    it keeps SQLite's one-writer model simple: no long-lived shared connection
    to leak or to serialize against.
    """

    def __init__(self, db_path="/app/jobs.db"):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # WAL: readers don't block the writer. Set per-connection; it is a
        # persistent property of the DB file once set, but setting it every
        # time is cheap and harmless.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self.get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id            TEXT PRIMARY KEY,
                    batch_id      TEXT,
                    client        TEXT NOT NULL,
                    job_class     TEXT NOT NULL,
                    weight        TEXT NOT NULL,
                    capability    TEXT,
                    model         TEXT,
                    payload       TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'queued',
                    deadline_ms   INTEGER,
                    node          TEXT,
                    result        TEXT,
                    error         TEXT,
                    submitted_at  REAL NOT NULL,
                    started_at    REAL,
                    finished_at   REAL
                )
            """)
            # Poll and prune are the hot read paths; index what they filter on.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_finished ON jobs(finished_at)")

    # --- Row helper ---------------------------------------------------------

    @staticmethod
    def _row_to_dict(row):
        if row is None:
            return None
        d = dict(row)
        # payload/result are stored as JSON text; hand callers real objects.
        for field in ("payload", "result"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except (ValueError, TypeError):
                    pass  # leave as-is if somehow not valid JSON
        return d

    # --- Create -------------------------------------------------------------

    def create(self, client, job_class, weight, payload,
               capability=None, model=None, deadline_ms=None, batch_id=None):
        """
        Insert one queued job. Returns the job id (a uuid4 hex string).

        payload is the request body that will be replayed to Ollama at dispatch
        time — stored verbatim as JSON.
        """
        job_id = uuid.uuid4().hex
        now = time.time()
        with self.get_conn() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (id, batch_id, client, job_class, weight, capability,
                    model, payload, status, deadline_ms, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, batch_id, client, job_class, weight, capability,
                 model, json.dumps(payload), STATUS_QUEUED, deadline_ms, now),
            )
        return job_id

    def create_batch(self, client, job_class, weight, payloads,
                     capability=None, model=None, deadline_ms=None):
        """
        Insert N independent single-inference jobs sharing one batch_id, in a
        single transaction (atomic ack — no "restarted after item 12" gap).

        This is the HireDesk shape: one submission, N schedulable rows, each of
        which the dispatcher spreads across the fleet independently. The store
        never grows a multi-item execution path.

        Returns (batch_id, [job_id, ...]) in payload order.
        """
        batch_id = uuid.uuid4().hex
        now = time.time()
        job_ids = []
        rows = []
        for payload in payloads:
            jid = uuid.uuid4().hex
            job_ids.append(jid)
            rows.append(
                (jid, batch_id, client, job_class, weight, capability,
                 model, json.dumps(payload), STATUS_QUEUED, deadline_ms, now))
        with self.get_conn() as conn:
            conn.executemany(
                """INSERT INTO jobs
                   (id, batch_id, client, job_class, weight, capability,
                    model, payload, status, deadline_ms, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return batch_id, job_ids

    # --- Read ---------------------------------------------------------------

    def get(self, job_id):
        with self.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_dict(row)

    def get_batch(self, batch_id):
        """All jobs in a batch, submission order. For batch polling."""
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE batch_id = ? ORDER BY submitted_at",
                (batch_id,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_queued(self):
        """
        All jobs still waiting, oldest first. This is what the dispatcher will
        load to rebuild its in-memory queue after a restart (persistence is the
        whole reason jobs live in SQLite).
        """
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY submitted_at",
                (STATUS_QUEUED,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def counts_by_status(self):
        """{status: count} — cheap health/dashboard number."""
        with self.get_conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # --- State transitions --------------------------------------------------
    # Each transition is guarded on the current status so a race (e.g. cancel
    # arriving as dispatch starts) can't move a job backwards or double-run it.
    # Methods return True if they changed a row, False if the guard blocked it.

    def mark_running(self, job_id, node):
        now = time.time()
        with self.get_conn() as conn:
            cur = conn.execute(
                """UPDATE jobs SET status = ?, node = ?, started_at = ?
                   WHERE id = ? AND status = ?""",
                (STATUS_RUNNING, node, now, job_id, STATUS_QUEUED),
            )
            return cur.rowcount > 0

    def mark_succeeded(self, job_id, result):
        now = time.time()
        with self.get_conn() as conn:
            cur = conn.execute(
                """UPDATE jobs SET status = ?, result = ?, finished_at = ?
                   WHERE id = ? AND status = ?""",
                (STATUS_SUCCEEDED, json.dumps(result), now,
                 job_id, STATUS_RUNNING),
            )
            return cur.rowcount > 0

    def mark_failed(self, job_id, error):
        now = time.time()
        with self.get_conn() as conn:
            # A job can fail from either queued (never dispatched) or running.
            cur = conn.execute(
                """UPDATE jobs SET status = ?, error = ?, finished_at = ?
                   WHERE id = ? AND status IN (?, ?)""",
                (STATUS_FAILED, str(error), now,
                 job_id, STATUS_QUEUED, STATUS_RUNNING),
            )
            return cur.rowcount > 0

    def mark_rejected(self, job_id, reason):
        """Admission control refused it at (or near) arrival."""
        now = time.time()
        with self.get_conn() as conn:
            cur = conn.execute(
                """UPDATE jobs SET status = ?, error = ?, finished_at = ?
                   WHERE id = ? AND status = ?""",
                (STATUS_REJECTED, str(reason), now, job_id, STATUS_QUEUED),
            )
            return cur.rowcount > 0

    def cancel(self, job_id):
        """
        Caller asked to cancel (DELETE) or gave up. Only meaningful while the
        job is still queued or running. Returns True if it was cancellable.
        """
        now = time.time()
        with self.get_conn() as conn:
            cur = conn.execute(
                """UPDATE jobs SET status = ?, finished_at = ?
                   WHERE id = ? AND status IN (?, ?)""",
                (STATUS_CANCELLED, now, job_id,
                 STATUS_QUEUED, STATUS_RUNNING),
            )
            return cur.rowcount > 0

    # --- Prune --------------------------------------------------------------

    def prune(self, older_than_seconds):
        """
        Delete terminal jobs finished more than older_than_seconds ago.
        Returns number of rows removed. Called on a timer by the gateway.
        """
        cutoff = time.time() - older_than_seconds
        placeholders = ",".join("?" for _ in TERMINAL)
        with self.get_conn() as conn:
            cur = conn.execute(
                f"""DELETE FROM jobs
                    WHERE status IN ({placeholders})
                      AND finished_at IS NOT NULL
                      AND finished_at < ?""",
                (*TERMINAL, cutoff),
            )
            return cur.rowcount

    # --- Recovery -----------------------------------------------------------

    def requeue_orphans(self):
        """
        On startup, any job left in 'running' is an orphan — the process died
        mid-inference, so no one will ever finish it. Move it back to queued so
        the dispatcher picks it up again. Returns number requeued.

        (Safe because inference is idempotent here: replaying the payload just
        regenerates the answer. If that ever stops being true, this becomes a
        policy decision rather than an automatic requeue.)
        """
        with self.get_conn() as conn:
            cur = conn.execute(
                """UPDATE jobs SET status = ?, node = NULL, started_at = NULL
                   WHERE status = ?""",
                (STATUS_QUEUED, STATUS_RUNNING),
            )
            return cur.rowcount
