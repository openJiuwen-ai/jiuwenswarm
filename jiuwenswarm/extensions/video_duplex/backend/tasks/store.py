"""One SQLite record of task facts and idempotent user operations."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3


def task_database():
    from jiuwenswarm.common.utils import get_agent_root_dir

    return get_agent_root_dir() / "voice-agent-tasks.sqlite"


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TaskStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS managed_tasks (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, session TEXT NOT NULL,
                    body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS managed_tasks_scope ON managed_tasks(owner,session);
                CREATE TABLE IF NOT EXISTS task_commands (
                    owner TEXT NOT NULL, session TEXT NOT NULL, id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, task_id TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(owner,session,id));
            """)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def get(db, task_id, owner=None, session=None):
        row = db.execute(
            "SELECT * FROM managed_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row:
            raise ValueError("Task not found in this conversation")
        if owner is not None and row["owner"] != owner:
            raise ValueError("Task not found in this conversation")
        if session is not None and row["session"] != session:
            raise ValueError("Task not found in this conversation")
        return json.loads(row["body"])

    @staticmethod
    def put(db, task):
        db.execute(
            "INSERT INTO managed_tasks VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
            (task["id"], task["owner"], task["session"], encode(task)),
        )

    @staticmethod
    def rows(db, owner=None, session=None):
        if owner is None:
            rows = db.execute("SELECT body FROM managed_tasks ORDER BY rowid")
        else:
            rows = db.execute(
                "SELECT body FROM managed_tasks WHERE owner=? AND session=? ORDER BY rowid",
                (owner, session),
            )
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def replay(db, owner, session, command_id, request):
        if (
            not isinstance(command_id, str)
            or not command_id.strip()
            or len(command_id) > 256
        ):
            raise ValueError("A stable command_id is required")
        fingerprint = hashlib.sha256(encode(request).encode()).hexdigest()
        row = db.execute(
            "SELECT * FROM task_commands WHERE owner=? AND session=? AND id=?",
            (owner, session, command_id),
        ).fetchone()
        if row and row["fingerprint"] != fingerprint:
            raise ValueError("Command identity reused with different parameters")
        return (json.loads(row["body"]) if row else None), fingerprint

    @staticmethod
    def command(db, task, command_id, fingerprint, receipt):
        db.execute(
            "INSERT INTO task_commands VALUES (?,?,?,?,?,?)",
            (
                task["owner"],
                task["session"],
                command_id,
                fingerprint,
                task["id"],
                encode(receipt),
            ),
        )

    def read(self, task_id, owner=None, session=None):
        with self.transaction() as db:
            return self.get(db, task_id, owner, session)

    def update(self, task_id, change):
        with self.transaction() as db:
            task = self.get(db, task_id)
            previous = encode(task)
            change(task)
            if encode(task) != previous:
                task["sequence"] += 1
                self.put(db, task)
            return task
