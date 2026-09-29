"""
approvals/store.py
Application-owned SQLite storage for approvals and their audit log.

Every state change runs inside BEGIN IMMEDIATE, which takes SQLite's write lock
before reading, so two concurrent callers cannot both observe "approved" and
both consume the same approval.
"""

import json
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    approval_id   TEXT PRIMARY KEY,
    incident_id   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    action_hash   TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','expired','consumed')),
    requested_at  TEXT NOT NULL,
    requested_by  TEXT NOT NULL,
    decided_by    TEXT,
    decided_at    TEXT,
    reason        TEXT,
    expires_at    TEXT NOT NULL,
    consumed_at   TEXT,
    consumed_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_incident ON approvals(incident_id);

CREATE TABLE IF NOT EXISTS approval_audit (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id  TEXT NOT NULL,
    incident_id  TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    reason       TEXT,
    occurred_at  TEXT NOT NULL,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_approval ON approval_audit(approval_id);
"""

COLUMNS = ("approval_id", "incident_id", "kind", "action_hash", "payload_json", "status",
           "requested_at", "requested_by", "decided_by", "decided_at", "reason",
           "expires_at", "consumed_at", "consumed_by")


class ApprovalStore:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def transaction(self):
        """Exclusive-write transaction; commits on success, rolls back on error."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # -- rows -----------------------------------------------------------------
    @staticmethod
    def insert(conn, row: dict) -> None:
        conn.execute(f"INSERT INTO approvals ({', '.join(COLUMNS)}) VALUES ({', '.join('?' for _ in COLUMNS)})",
                     [row.get(c) for c in COLUMNS])

    @staticmethod
    def fetch(conn, approval_id: str):
        row = conn.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def transition(conn, approval_id: str, from_status: str, **fields) -> bool:
        """Compare-and-set: update only if the row is still in from_status."""
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = conn.execute(f"UPDATE approvals SET {sets} WHERE approval_id = ? AND status = ?",
                           [*fields.values(), approval_id, from_status])
        return cur.rowcount == 1

    @staticmethod
    def audit(conn, *, approval_id, incident_id, event_type, actor, occurred_at, reason=None, details=None):
        conn.execute(
            "INSERT INTO approval_audit (approval_id, incident_id, event_type, actor, reason, occurred_at, details_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (approval_id, incident_id, event_type, actor, reason, occurred_at,
             json.dumps(details, sort_keys=True) if details else None))

    # -- reads ----------------------------------------------------------------
    def get(self, approval_id: str):
        conn = self._connect()
        try:
            return self.fetch(conn, approval_id)
        finally:
            conn.close()

    def audit_events(self, approval_id: str) -> list:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM approval_audit WHERE approval_id = ? ORDER BY event_id",
                                (approval_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
