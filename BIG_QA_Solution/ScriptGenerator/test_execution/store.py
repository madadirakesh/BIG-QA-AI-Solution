"""
SQLite persistence for runs, results, publications, audit and the AI plan cache.

Runs execute on background threads that outlive the request, so every call
opens its own connection (same reason as db.app_db.open_own_connection).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional

from .models import utcnow_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS TestExecutionRuns (
    run_id TEXT PRIMARY KEY,
    created_by TEXT,
    source TEXT,
    status TEXT,
    config_json TEXT,
    source_meta_json TEXT,
    summary_json TEXT,
    usage_json TEXT,
    created_at TEXT,
    updated_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS TestExecutionCases (
    run_id TEXT NOT NULL,
    test_key TEXT NOT NULL,
    seq INTEGER,
    case_json TEXT,
    result_json TEXT,
    PRIMARY KEY (run_id, test_key)
);
CREATE TABLE IF NOT EXISTS TestExecutionPublications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    test_key TEXT NOT NULL,
    destination TEXT NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    status TEXT,
    external_ref TEXT,
    message TEXT,
    attempts INTEGER DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS TestExecutionAudit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    run_id TEXT,
    actor TEXT,
    action TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS TestExecutionPlanCache (
    plan_hash TEXT PRIMARY KEY,
    actions_json TEXT,
    created_at TEXT,
    hits INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_te_cases_run ON TestExecutionCases(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_te_pub_run ON TestExecutionPublications(run_id, test_key);
CREATE INDEX IF NOT EXISTS idx_te_audit_run ON TestExecutionAudit(run_id);
"""


class ExecutionStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _write(self, sql: str, params=()):
        with self._lock:
            conn = self._conn()
            try:
                with conn:
                    cur = conn.execute(sql, params)
                    return cur.rowcount
            finally:
                conn.close()

    def _read(self, sql: str, params=()) -> list[dict]:
        conn = self._conn()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    # ── runs ──────────────────────────────────────────────────────────────
    def create_run(self, run_id: str, created_by: str, source: str, status: str, config: dict, source_meta: dict):
        now = utcnow_iso()
        self._write(
            "INSERT INTO TestExecutionRuns (run_id, created_by, source, status, config_json, source_meta_json, "
            "summary_json, usage_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, created_by, source, status, json.dumps(config), json.dumps(source_meta), "{}", "{}", now, now),
        )

    def update_run(self, run_id: str, **fields):
        cols, vals = [], []
        for k, v in fields.items():
            if k in ("config", "source_meta", "summary", "usage"):
                k, v = f"{k}_json", json.dumps(v)
            cols.append(f"{k} = ?")
            vals.append(v)
        cols.append("updated_at = ?")
        vals.append(utcnow_iso())
        self._write(f"UPDATE TestExecutionRuns SET {', '.join(cols)} WHERE run_id = ?", (*vals, run_id))

    def get_run(self, run_id: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM TestExecutionRuns WHERE run_id = ?", (run_id,))
        if not rows:
            return None
        r = rows[0]
        for k in ("config", "source_meta", "summary", "usage"):
            r[k] = json.loads(r.pop(f"{k}_json") or "{}")
        return r

    def list_runs(self, limit: int = 30) -> list[dict]:
        rows = self._read(
            "SELECT run_id, created_by, source, status, summary_json, created_at, finished_at "
            "FROM TestExecutionRuns ORDER BY created_at DESC LIMIT ?", (limit,))
        for r in rows:
            r["summary"] = json.loads(r.pop("summary_json") or "{}")
        return rows

    # ── cases / results ──────────────────────────────────────────────────
    def save_cases(self, run_id: str, cases: list[dict]):
        with self._lock:
            conn = self._conn()
            try:
                with conn:
                    for i, c in enumerate(cases):
                        conn.execute(
                            "INSERT OR REPLACE INTO TestExecutionCases (run_id, test_key, seq, case_json, result_json) "
                            "VALUES (?,?,?,?,COALESCE((SELECT result_json FROM TestExecutionCases WHERE run_id=? AND test_key=?), NULL))",
                            (run_id, c["key"], i, json.dumps(c), run_id, c["key"]),
                        )
            finally:
                conn.close()

    def save_result(self, run_id: str, test_key: str, result: dict):
        self._write("UPDATE TestExecutionCases SET result_json = ? WHERE run_id = ? AND test_key = ?",
                    (json.dumps(result), run_id, test_key))

    def load_cases(self, run_id: str) -> list[tuple[dict, Optional[dict]]]:
        rows = self._read("SELECT case_json, result_json FROM TestExecutionCases WHERE run_id = ? ORDER BY seq", (run_id,))
        return [(json.loads(r["case_json"]), json.loads(r["result_json"]) if r["result_json"] else None) for r in rows]

    # ── publications (idempotency ledger) ───────────────────────────────
    def get_publication(self, idempotency_key: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM TestExecutionPublications WHERE idempotency_key = ?", (idempotency_key,))
        return rows[0] if rows else None

    def record_publication(self, run_id: str, test_key: str, destination: str, idempotency_key: str,
                           status: str, external_ref: str = "", message: str = ""):
        now = utcnow_iso()
        with self._lock:
            conn = self._conn()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO TestExecutionPublications (run_id, test_key, destination, idempotency_key, status, "
                        "external_ref, message, attempts, updated_at) VALUES (?,?,?,?,?,?,?,1,?) "
                        "ON CONFLICT(idempotency_key) DO UPDATE SET status=excluded.status, "
                        "external_ref=excluded.external_ref, message=excluded.message, "
                        "attempts=TestExecutionPublications.attempts+1, updated_at=excluded.updated_at",
                        (run_id, test_key, destination, idempotency_key, status, external_ref, message, now),
                    )
            finally:
                conn.close()

    def list_publications(self, run_id: str) -> list[dict]:
        return self._read("SELECT * FROM TestExecutionPublications WHERE run_id = ? ORDER BY id", (run_id,))

    # ── audit ────────────────────────────────────────────────────────────
    def audit(self, run_id: str, actor: str, action: str, detail: str = ""):
        self._write("INSERT INTO TestExecutionAudit (ts, run_id, actor, action, detail) VALUES (?,?,?,?,?)",
                    (utcnow_iso(), run_id, actor, action, detail[:2000]))

    def audit_log(self, run_id: str) -> list[dict]:
        return self._read("SELECT ts, actor, action, detail FROM TestExecutionAudit WHERE run_id = ? ORDER BY id", (run_id,))

    # ── AI plan cache ────────────────────────────────────────────────────
    def get_plan(self, plan_hash: str) -> Optional[list]:
        rows = self._read("SELECT actions_json FROM TestExecutionPlanCache WHERE plan_hash = ?", (plan_hash,))
        if not rows:
            return None
        self._write("UPDATE TestExecutionPlanCache SET hits = hits + 1 WHERE plan_hash = ?", (plan_hash,))
        return json.loads(rows[0]["actions_json"])

    def put_plan(self, plan_hash: str, actions: list):
        self._write("INSERT OR REPLACE INTO TestExecutionPlanCache (plan_hash, actions_json, created_at, hits) VALUES (?,?,?,0)",
                    (plan_hash, json.dumps(actions), utcnow_iso()))
