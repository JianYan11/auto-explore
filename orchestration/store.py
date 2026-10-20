"""SQLite task queue and transactional allocation for a single machine."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .models import Assignment, Outcome, TaskSpec


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    spec TEXT NOT NULL, resource_key TEXT NOT NULL,
    priority INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'queued'
        CHECK(state IN ('queued','retry_wait','active','succeeded','failed','cancelled')),
    available_at REAL NOT NULL, created_at REAL NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0, active_attempt_id TEXT
);
CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(state, available_at, priority, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS task_resource_exclusive ON tasks(resource_key)
    WHERE state='active' AND resource_key!='';
CREATE TABLE IF NOT EXISTS environments (
    serial TEXT PRIMARY KEY, reset_policy TEXT NOT NULL CHECK(reset_policy IN ('restart','clear-data')),
    state TEXT NOT NULL DEFAULT 'available' CHECK(state IN ('available','busy','quarantined')),
    generation INTEGER NOT NULL DEFAULT 0,
    active_attempt_id TEXT, last_package TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
    attempt_no INTEGER NOT NULL, serial TEXT NOT NULL REFERENCES environments(serial),
    owner TEXT NOT NULL, state TEXT NOT NULL
        CHECK(state IN ('preparing','running','finalizing','succeeded','failed','cancelled','lost')),
    output_dir TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL, heartbeat_at REAL NOT NULL, finished_at REAL,
    pid INTEGER, returncode INTEGER, reason TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    UNIQUE(task_id, attempt_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS attempt_device_exclusive ON attempts(serial)
    WHERE state IN ('preparing', 'running', 'finalizing');
CREATE UNIQUE INDEX IF NOT EXISTS attempt_task_exclusive ON attempts(task_id)
    WHERE state IN ('preparing', 'running', 'finalizing');
"""


class TaskStore:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Unsupported queue schema version: {version}")
            db.executescript(SCHEMA)
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def enqueue(self, specs: list[TaskSpec]) -> list[str]:
        ids = []
        now = time.time()
        with self.connection(write=True) as db:
            for spec in specs:
                task_id = uuid4().hex
                db.execute("INSERT INTO tasks(id,spec,resource_key,priority,available_at,created_at) VALUES(?,?,?,?,?,?)",
                           (task_id, json.dumps(asdict(spec)), spec.resource_key, spec.priority, now, now))
                ids.append(task_id)
        return ids

    def register_environment(self, serial: str, reset_policy: str = "restart") -> None:
        if not serial.strip() or serial.startswith("-") or "\x00" in serial:
            raise ValueError("A nonempty ADB serial is required")
        if reset_policy not in {"restart", "clear-data"}:
            raise ValueError("reset_policy must be restart or clear-data")
        with self.connection(write=True) as db:
            current = db.execute("SELECT state FROM environments WHERE serial=?", (serial,)).fetchone()
            if current and current["state"] == "busy":
                raise ValueError("Cannot change an occupied environment")
            db.execute("INSERT INTO environments(serial,reset_policy) VALUES(?,?) "
                       "ON CONFLICT(serial) DO UPDATE SET reset_policy=excluded.reset_policy", (serial, reset_policy))

    def snapshot(self) -> dict:
        with self.connection() as db:
            db.execute("BEGIN")  # Keep all three table reads in one WAL snapshot.
            result = {table: [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY {order}")]
                      for table, order in (("tasks", "sequence"), ("attempts", "created_at,id"),
                                           ("environments", "serial"))}
        for task in result["tasks"]:
            task["spec"] = json.loads(task["spec"])
        return result

    def claim(self, serial: str, owner: str, output_root: Path) -> Assignment | None:
        now = time.time()
        with self.connection(write=True) as db:
            env = db.execute("SELECT * FROM environments WHERE serial=? AND state='available'", (serial,)).fetchone()
            if env is None:
                return None
            candidates = db.execute("""
                SELECT t.* FROM tasks t WHERE t.state IN ('queued','retry_wait')
                AND t.available_at<=? AND t.cancel_requested=0
                AND (t.resource_key='' OR NOT EXISTS (
                    SELECT 1 FROM tasks other WHERE other.state='active' AND other.resource_key=t.resource_key))
                ORDER BY t.priority DESC,t.sequence
                """, (now,)).fetchall()
            for task in candidates:
                spec = TaskSpec.from_dict(json.loads(task["spec"]))
                if not spec.devices or serial in spec.devices:
                    break
            else:
                return None
            attempt_no = db.execute("SELECT count(*)+1 FROM attempts WHERE task_id=?", (task["id"],)).fetchone()[0]
            attempt_id = uuid4().hex
            output = str(output_root.resolve() / task["id"] / attempt_id)
            db.execute("INSERT INTO attempts(id,task_id,attempt_no,serial,owner,state,output_dir,created_at,heartbeat_at) "
                       "VALUES(?,?,?,?,?,'preparing',?,?,?)",
                       (attempt_id, task["id"], attempt_no, serial, owner, output, now, now))
            db.execute("UPDATE tasks SET state='active',active_attempt_id=? WHERE id=?", (attempt_id, task["id"]))
            db.execute("UPDATE environments SET state='busy',active_attempt_id=?,generation=generation+1,last_package=?,error='' "
                       "WHERE serial=?", (attempt_id, spec.package, serial))
            return Assignment(task["id"], attempt_id, attempt_no, serial, env["reset_policy"], owner, output, spec)

    def touch(self, assignment: Assignment, *, state: str | None = None, pid: int | None = None) -> None:
        with self.connection(write=True) as db:
            changed = db.execute("UPDATE attempts SET heartbeat_at=?,state=coalesce(?,state),pid=coalesce(?,pid) "
                                 "WHERE id=? AND owner=? AND state IN ('preparing','running','finalizing')",
                                 (time.time(), state, pid, assignment.attempt_id, assignment.owner)).rowcount
            if changed != 1:
                raise RuntimeError("Attempt ownership was lost")

    def is_cancelled(self, task_id: str) -> bool:
        with self.connection() as db:
            row = db.execute("SELECT cancel_requested FROM tasks WHERE id=?", (task_id,)).fetchone()
            return row is None or bool(row[0])

    def cancel(self, task_id: str) -> None:
        with self.connection(write=True) as db:
            row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown task")
            if row[0] in {"queued", "retry_wait", "active"}:
                if row[0] == "active":
                    attempt = db.execute("SELECT a.state FROM attempts a JOIN tasks t ON t.active_attempt_id=a.id WHERE t.id=?",
                                         (task_id,)).fetchone()
                    if attempt and attempt[0] == "finalizing":
                        return  # Result commitment and environment cleanup have already started.
                db.execute("UPDATE tasks SET cancel_requested=1,state=CASE WHEN state='active' THEN state ELSE 'cancelled' END "
                           "WHERE id=?", (task_id,))

    def retry(self, task_id: str) -> None:
        with self.connection(write=True) as db:
            changed = db.execute("UPDATE tasks SET state='queued',cancel_requested=0,active_attempt_id=NULL,available_at=? "
                                 "WHERE id=? AND state IN ('failed','cancelled')", (time.time(), task_id)).rowcount
            if changed != 1:
                raise ValueError("Only a failed or cancelled task can be retried")

    def finish(self, assignment: Assignment, outcome: Outcome, environment_error: str = "") -> None:
        now = time.time()
        with self.connection(write=True) as db:
            attempt = db.execute("SELECT state,owner FROM attempts WHERE id=?", (assignment.attempt_id,)).fetchone()
            if attempt is None or attempt["owner"] != assignment.owner or attempt["state"] not in {"preparing", "running", "finalizing"}:
                raise RuntimeError("Cannot finish an unowned or completed attempt")
            task = db.execute("SELECT cancel_requested FROM tasks WHERE id=?", (assignment.task_id,)).fetchone()
            state = "cancelled" if task[0] else outcome.state
            task_state = state
            if state == "failed" and outcome.retryable and assignment.attempt_no < assignment.spec.max_attempts:
                task_state = "retry_wait"
            db.execute("UPDATE attempts SET state=?,finished_at=?,heartbeat_at=?,returncode=?,reason=?,error=? WHERE id=?",
                       (state, now, now, outcome.returncode, outcome.reason, outcome.error, assignment.attempt_id))
            db.execute("UPDATE tasks SET state=?,active_attempt_id=NULL,available_at=? WHERE id=?",
                       (task_state, now + assignment.spec.retry_delay_seconds, assignment.task_id))
            db.execute("UPDATE environments SET state=?,active_attempt_id=NULL,error=? WHERE serial=? AND active_attempt_id=?",
                       ("quarantined" if environment_error else "available", environment_error,
                        assignment.device, assignment.attempt_id))

    def quarantine(self, serial: str, error: str) -> None:
        with self.connection(write=True) as db:
            db.execute("UPDATE environments SET state='quarantined',error=? WHERE serial=? AND state!='busy'", (error, serial))

    def recover_environment(self, serial: str) -> None:
        with self.connection(write=True) as db:
            changed = db.execute("UPDATE environments SET state='available',error='',active_attempt_id=NULL "
                                 "WHERE serial=? AND state='quarantined'", (serial,)).rowcount
            if changed != 1:
                raise ValueError("Environment must be quarantined before recovery")

    def recover_interrupted(self) -> int:
        """Called only while holding the coordinator lock; never silently reuse devices."""
        with self.connection(write=True) as db:
            rows = db.execute("SELECT * FROM attempts WHERE state IN ('preparing','running','finalizing')").fetchall()
            for row in rows:
                db.execute("UPDATE attempts SET state='lost',reason='coordinator_lost',error=?,finished_at=? WHERE id=?",
                           ("Previous coordinator stopped; inspect device and process logs before recovery", time.time(), row["id"]))
                db.execute("UPDATE tasks SET state=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'failed' END,"
                           "active_attempt_id=NULL WHERE id=?", (row["task_id"],))
                db.execute("UPDATE environments SET state='quarantined',active_attempt_id=NULL,error='coordinator_lost' WHERE serial=?",
                           (row["serial"],))
            return len(rows)
