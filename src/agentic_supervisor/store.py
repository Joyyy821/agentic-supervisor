"""Durable run, control, and event storage."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


ACTIVE_STATUSES = ("starting", "active", "pausing", "paused", "recovering", "blocked")


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "state.sqlite3"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self._connect()
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    thread_id TEXT,
                    tmux_session TEXT NOT NULL,
                    status TEXT NOT NULL,
                    desired_status TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    codex_version TEXT NOT NULL,
                    git_head TEXT NOT NULL,
                    git_dirty INTEGER NOT NULL,
                    tokens_used INTEGER NOT NULL DEFAULT 0,
                    native_tokens_seen INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    token_budget INTEGER NOT NULL,
                    time_budget_seconds INTEGER NOT NULL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    native_id TEXT,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(run_id, native_id, kind)
                );
                CREATE TABLE IF NOT EXISTS controls (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    detail TEXT
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    name TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    exit_code INTEGER,
                    stdout_path TEXT,
                    stderr_path TEXT,
                    result_json TEXT
                );
                CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, id);
                CREATE INDEX IF NOT EXISTS controls_by_run ON controls(run_id, status, created_at);
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            if "tokens_used" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN tokens_used INTEGER NOT NULL DEFAULT 0")
            if "native_tokens_seen" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN native_tokens_seen INTEGER NOT NULL DEFAULT 0")
            index = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='one_live_run'").fetchone()
            if index and "'pausing'" not in index[0]:
                db.execute("DROP INDEX one_live_run")
                index = None
            if not index:
                db.execute("CREATE UNIQUE INDEX one_live_run ON runs((1)) "
                           "WHERE status IN ('starting','active','pausing','paused','recovering','blocked')")

    def _connect(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 10000")
        db.execute("PRAGMA journal_mode = WAL")
        return db

    def create_run(self, config: Any, codex_version: str, git_head: str, git_dirty: bool) -> str:
        run_id = str(uuid.uuid4())
        now = time.time()
        snapshot = json.dumps(config.safe_snapshot(), sort_keys=True)
        session = f"asup-{run_id}"
        with self.connect() as db:
            db.execute(
                """INSERT INTO runs
                (id,project,repository,tmux_session,status,desired_status,objective,config_json,
                 codex_version,git_head,git_dirty,created_at,token_budget,time_budget_seconds)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, config.project, str(config.repository), session, "starting", "active",
                 config.objective, snapshot, codex_version, git_head, int(git_dirty), now,
                 config.token_budget, config.time_budget_seconds),
            )
        run_dir = self.root / "runs" / run_id
        try:
            run_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            run_dir.parent.chmod(0o700)
            run_dir.mkdir(mode=0o700)
            run_dir.chmod(0o700)
            (run_dir / "artifacts").mkdir(mode=0o700)
            config_path = run_dir / "config.json"
            config_path.write_text(snapshot + "\n", encoding="utf-8")
            config_path.chmod(0o600)
        except Exception:
            with self.connect() as db:
                db.execute("DELETE FROM runs WHERE id=?", (run_id,))
            raise
        return run_id

    def get_run(self, run_id: str) -> sqlite3.Row:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown run: {run_id}")
        return row

    def list_runs(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute("SELECT * FROM runs ORDER BY created_at DESC"))

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"thread_id", "status", "desired_status", "started_at", "finished_at", "last_error"}
        if not fields or set(fields) - allowed:
            raise ValueError("invalid run fields")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.connect() as db:
            db.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*fields.values(), run_id))

    def observe_token_usage(self, run_id: str, native_tokens: int) -> tuple[int, int]:
        if isinstance(native_tokens, bool) or not isinstance(native_tokens, int) or native_tokens < 0:
            raise ValueError("native token usage must be a non-negative integer")
        with self.connect() as db:
            row = db.execute("SELECT tokens_used,native_tokens_seen FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown run: {run_id}")
            delta = native_tokens - row["native_tokens_seen"] if native_tokens >= row["native_tokens_seen"] else native_tokens
            total = row["tokens_used"] + delta
            db.execute("UPDATE runs SET tokens_used=?,native_tokens_seen=? WHERE id=?",
                       (total, native_tokens, run_id))
        return total, delta

    def queue_control(self, run_id: str, kind: str, payload: str) -> str:
        control_id = str(uuid.uuid4())
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO controls(id,run_id,kind,payload,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (control_id, run_id, kind, payload, "queued", now, now),
            )
        return control_id

    def pending_controls(self, run_id: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute(
                "SELECT * FROM controls WHERE run_id=? AND status='queued' ORDER BY created_at,id", (run_id,)
            ))

    def controls(self, run_id: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute(
                "SELECT * FROM controls WHERE run_id=? ORDER BY created_at DESC LIMIT 100", (run_id,)
            ))

    def release_waiting_controls(self, run_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE controls SET status='queued',updated_at=? WHERE run_id=? "
                       "AND status IN ('waiting_turn','waiting_resume')", (time.time(), run_id))

    def update_control(self, control_id: str, status: str, detail: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE controls SET status=?,updated_at=?,detail=? WHERE id=?",
                       (status, time.time(), detail, control_id))

    def add_event(self, run_id: str, kind: str, payload: dict[str, Any], native_id: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO events(run_id,native_id,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, native_id, kind, json.dumps(payload, sort_keys=True, default=str), time.time()),
            )

    def events(self, run_id: str, after: int = 0, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute(
                "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?", (run_id, after, limit)
            ))

    def latest_event(self, run_id: str, kind: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM events WHERE run_id=? AND kind=? ORDER BY id DESC LIMIT 1", (run_id, kind)
            ).fetchone()

    def create_attempt(self, run_id: str, name: str, command: list[str]) -> str:
        attempt_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO attempts(id,run_id,name,command_json,status,started_at) VALUES(?,?,?,?,?,?)",
                (attempt_id, run_id, name, json.dumps(command), "running", time.time()),
            )
        return attempt_id

    def resolve_attempt(self, run_id: str, attempt_id: str, status: str, detail: str) -> None:
        if status not in {"succeeded", "failed", "not_run", "outcome_unknown"}:
            raise ValueError("invalid attempt status")
        with self.connect() as db:
            current = db.execute("SELECT status FROM attempts WHERE id=? AND run_id=?",
                                 (attempt_id, run_id)).fetchone()
            if current is None:
                raise ValueError("attempt does not belong to this run")
            if current["status"] != "outcome_unknown" and not (
                current["status"] == "running" and status in {"outcome_unknown", "not_run"}
            ):
                raise ValueError("only an uncertain attempt can be resolved")
            updated = db.execute(
                "UPDATE attempts SET status=?,finished_at=?,result_json=? WHERE id=? AND run_id=?",
                (status, time.time(), json.dumps({"evidence": detail}), attempt_id, run_id),
            )
            if updated.rowcount != 1:
                raise ValueError("attempt does not belong to this run")

    def mark_interrupted_work_unknown(self, run_id: str) -> dict[str, int]:
        now = time.time()
        with self.connect() as db:
            attempts = db.execute(
                "UPDATE attempts SET status='outcome_unknown',finished_at=?,"
                "result_json=? WHERE run_id=? AND status='running'",
                (now, json.dumps({"evidence": "supervisor worker restarted during command"}), run_id),
            ).rowcount
            controls = db.execute(
                "UPDATE controls SET status='outcome_unknown',updated_at=?,"
                "detail='worker restarted after dispatch; delivery was not replayed' "
                "WHERE run_id=? AND status IN ('dispatched','awaiting_ack')",
                (now, run_id),
            ).rowcount
        return {"attempts": attempts, "controls": controls}

    def resolve_control(self, run_id: str, control_id: str, outcome: str, evidence: str) -> None:
        statuses = {"accepted": "confirmed", "rejected": "rejected", "not_applied": "not_applied"}
        if outcome not in statuses:
            raise ValueError("control outcome must be accepted, rejected, or not_applied")
        with self.connect() as db:
            updated = db.execute(
                "UPDATE controls SET status=?,updated_at=?,detail=? WHERE id=? AND run_id=? "
                "AND status='outcome_unknown'",
                (statuses[outcome], time.time(), f"operator resolved {outcome}: {evidence}", control_id, run_id),
            )
            if updated.rowcount != 1:
                raise ValueError("control is not uncertain or does not belong to this run")

    def attempt(self, run_id: str, attempt_id: str) -> sqlite3.Row:
        with self.connect() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=? AND run_id=?", (attempt_id, run_id)).fetchone()
        if row is None:
            raise ValueError("attempt does not belong to this run")
        return row

    def finish_attempt(self, attempt_id: str, status: str, exit_code: int | None,
                       stdout_path: str, stderr_path: str, result: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE attempts SET status=?,finished_at=?,exit_code=?,stdout_path=?,stderr_path=?,result_json=? WHERE id=?",
                (status, time.time(), exit_code, stdout_path, stderr_path,
                 json.dumps(result, sort_keys=True, default=str), attempt_id),
            )

    def attempts(self, run_id: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute("SELECT * FROM attempts WHERE run_id=? ORDER BY started_at", (run_id,)))
