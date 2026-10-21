"""Private per-group records; reads never create missing archives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import contextmanager, closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository, utc_time
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.sqlite_store import private_dir
from openjiuwen.harness.persistent_agent import GroupMessage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_info (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), namespace_id TEXT NOT NULL, group_id TEXT NOT NULL,
    platform TEXT NOT NULL, platform_chat_id TEXT NOT NULL, schema_version INTEGER NOT NULL,
    content_revision INTEGER NOT NULL DEFAULT 0, gap INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    ingest_seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL,
    payload TEXT NOT NULL, received_at_us INTEGER NOT NULL, changed_at TEXT, source_ref TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recalls (message_id TEXT PRIMARY KEY, source_ref TEXT NOT NULL, changed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_secrets (singleton INTEGER PRIMARY KEY CHECK(singleton=1), secret BLOB NOT NULL);
"""


@dataclass(frozen=True)
class ArchivePage:
    status: str
    messages: tuple[GroupMessage, ...] = ()
    next_cursor: str | None = None
    scanned_through: str | None = None
    reason: str | None = None


class ConversationArchive:
    def __init__(self, root: Path, groups: GroupChatRepository) -> None:
        self.root = Path(root).resolve()
        self.groups = groups

    def directory(self, namespace_id: str, group_id: str) -> Path:
        self.groups.get(namespace_id, group_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", group_id):
            raise PersistentAgentError("unsafe internal group ID")
        path = self.root / group_id
        if path.is_symlink() or path.resolve().parent != self.root:
            raise PersistentAgentError("group archive path escaped its root")
        return path

    @contextmanager
    def connection(self, namespace_id: str, group_id: str, *, create: bool = False):
        group = self.groups.get(namespace_id, group_id)
        directory = self.directory(namespace_id, group_id)
        db = directory / "messages.sqlite3"
        if db.is_symlink() or db.resolve().parent != directory.resolve():
            raise PersistentAgentError("group database path escaped its directory")
        if create:
            private_dir(directory)
            attachments = directory / "attachments"
            if attachments.is_symlink():
                raise PersistentAgentError("unsafe attachment directory")
            private_dir(attachments)
        elif not db.is_file():
            raise PersistentAgentError("group archive unavailable", code="NOT_FOUND")
        try:
            uri = db.as_uri() + ("?mode=rwc" if create else "?mode=rw")
            with closing(sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)) as conn:
                conn.row_factory = sqlite3.Row
                if create:
                    conn.executescript(_SCHEMA)
                conn.execute("BEGIN IMMEDIATE" if create else "BEGIN")
                try:
                    if create:
                        conn.execute("INSERT OR IGNORE INTO archive_secrets VALUES (1, randomblob(32))")
                        conn.execute("INSERT OR IGNORE INTO archive_info VALUES (1, ?, ?, ?, ?, 1, 0, 0)",
                                     (namespace_id, group_id, group.platform, group.platform_chat_id))
                    info = conn.execute("SELECT * FROM archive_info WHERE singleton=1").fetchone()
                    if info is None or (info["namespace_id"], info["group_id"], info["platform"],
                                        info["platform_chat_id"], info["schema_version"]) != (
                                            namespace_id, group_id, group.platform, group.platform_chat_id, 1):
                        raise PersistentAgentError("group archive identity or version mismatch")
                    yield conn
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
        except sqlite3.Error as exc:
            raise PersistentAgentError("group archive unavailable", code="ARCHIVE_UNAVAILABLE") from exc

    def append(self, namespace_id: str, group_id: str, message: GroupMessage, *, source_ref: str,
               changed_at: datetime | None = None, allow_resolved_mentions: bool = False) -> str:
        message = GroupMessage.model_validate(message).model_copy(update={"queued_request": False})
        require_identifier("source_ref", source_ref)
        stamp = utc_time(changed_at).isoformat() if changed_at is not None else None
        with self.connection(namespace_id, group_id, create=True) as conn:
            if message.status == "recalled":
                conn.execute("INSERT OR IGNORE INTO recalls VALUES (?,?,?)",
                             (message.message_id, source_ref, stamp or utc_time(message.received_at).isoformat()))
            recalled = conn.execute("SELECT 1 FROM recalls WHERE message_id=?", (message.message_id,)).fetchone()
            if recalled is not None or message.status == "recalled":
                message = message.model_copy(update={"status": "recalled", "text": None, "attachment_refs": ()})
            row = conn.execute("SELECT * FROM messages WHERE message_id=?", (message.message_id,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO messages(message_id,payload,received_at_us,changed_at,source_ref) VALUES (?,?,?,?,?)",
                             (message.message_id, message.model_dump_json(), time_micros(message.received_at), stamp, source_ref))
            elif recalled is not None:
                previous = GroupMessage.model_validate_json(row["payload"])
                current = previous.model_copy(update={"status": "recalled", "text": None, "attachment_refs": ()})
                if current != previous:
                    conn.execute("UPDATE messages SET payload=?,source_ref=? WHERE message_id=?",
                                 (current.model_dump_json(), source_ref, message.message_id))
                    conn.execute("UPDATE archive_info SET content_revision=content_revision+1")
            else:
                previous = GroupMessage.model_validate_json(row["payload"])
                newer = stamp is not None and (row["changed_at"] is None or stamp > row["changed_at"])
                # An older build stored Feishu @_user_N / @_all. Refresh now resolves those
                # tokens. The platform revision is unchanged, so this is not a conflicting edit.
                resolved_mentions = (
                    allow_resolved_mentions
                    and stamp is not None
                    and stamp == row["changed_at"]
                    and isinstance(previous.text, str)
                    and "@_" in previous.text
                    and isinstance(message.text, str)
                    and "@_" not in message.text
                    and previous.attachment_refs == message.attachment_refs
                )
                if newer or resolved_mentions:
                    message = message.model_copy(update={"received_at": previous.received_at,
                                                         "sender_id": previous.sender_id,
                                                         "sender_kind": previous.sender_kind})
                    conn.execute("UPDATE messages SET payload=?,changed_at=?,source_ref=? WHERE message_id=?",
                                 (message.model_dump_json(), stamp, source_ref, message.message_id))
                    if message != previous:
                        conn.execute("UPDATE archive_info SET content_revision=content_revision+1")
                elif previous != message:
                    # A redelivery can have different presentation metadata, but never overwrite known content.
                    if previous.status != "recalled" and (previous.text, previous.attachment_refs) != (
                            message.text, message.attachment_refs):
                        raise PersistentAgentError(
                            "message content conflicts without a newer revision", code="CONFLICT",
                        )
        return message_reference(group_id, message.message_id)

    def recall(self, namespace_id: str, group_id: str, message_id: str, *, source_ref: str,
               changed_at: datetime) -> None:
        require_identifier("message_id", message_id)
        require_identifier("source_ref", source_ref)
        with self.connection(namespace_id, group_id, create=True) as conn:
            added = conn.execute("INSERT OR IGNORE INTO recalls VALUES (?,?,?)",
                                 (message_id, source_ref, utc_time(changed_at).isoformat())).rowcount
            row = conn.execute("SELECT payload FROM messages WHERE message_id=?", (message_id,)).fetchone()
            changed = False
            if row:
                message = GroupMessage.model_validate_json(row["payload"])
                changed = message.status != "recalled" or message.text is not None or bool(message.attachment_refs)
                message = message.model_copy(update={"status": "recalled", "text": None, "attachment_refs": ()})
                conn.execute("UPDATE messages SET payload=? WHERE message_id=?", (message.model_dump_json(), message_id))
            if added or changed:
                conn.execute("UPDATE archive_info SET content_revision=content_revision+1")

    def mark_gap(self, namespace_id: str, group_id: str) -> None:
        with self.connection(namespace_id, group_id, create=True) as conn:
            conn.execute("UPDATE archive_info SET gap=1")

    def current_messages(self, namespace_id: str, group_id: str, message_ids: tuple[str, ...]) -> tuple[GroupMessage, ...]:
        with self.connection(namespace_id, group_id) as conn:
            messages = []
            for message_id in message_ids:
                row = conn.execute("SELECT payload FROM messages WHERE message_id=?", (message_id,)).fetchone()
                if row is None:
                    raise PersistentAgentError("selected group message disappeared")
                messages.append(GroupMessage.model_validate_json(row["payload"]))
        return tuple(messages)

    def recent(self, namespace_id: str, group_id: str, *, cursor: str | None, limit: int,
               visible_from: datetime, reader_id: str = "") -> ArchivePage:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise PersistentAgentError("invalid group page limit")
        with self.connection(namespace_id, group_id) as conn:
            info = conn.execute("SELECT * FROM archive_info").fetchone()
            secret = bytes(conn.execute("SELECT secret FROM archive_secrets WHERE singleton=1").fetchone()[0])
            scope = hashlib.sha256(json.dumps([namespace_id, group_id, reader_id,
                                              utc_time(visible_from).isoformat()]).encode()).hexdigest()
            upper = conn.execute("SELECT COALESCE(MAX(ingest_seq),0) FROM messages").fetchone()[0]
            start = 0
            if cursor is not None:
                row = conn.execute("SELECT ingest_seq,payload FROM messages WHERE message_id=?", (cursor,)).fetchone()
                if row:
                    if GroupMessage.model_validate_json(row["payload"]).received_at < utc_time(visible_from):
                        return ArchivePage("incomplete", reason="cursor outside the readable history")
                    start = row["ingest_seq"]
                else:
                    try:
                        state = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                        start, upper, revision, signature = state
                        signed = json.dumps([scope, start, upper, revision]).encode()
                        if not hmac.compare_digest(signature, hmac.new(secret, signed, hashlib.sha256).hexdigest()):
                            raise ValueError("cursor signature mismatch")
                        if revision != info["content_revision"]:
                            raise ValueError("cursor scope changed")
                        if not all(type(value) is int and value >= 0 for value in (start, upper, revision)):
                            raise ValueError("cursor position invalid")
                    except (ValueError, TypeError, UnicodeError, OverflowError):
                        return ArchivePage("incomplete", reason="unknown or expired group cursor")
            rows = conn.execute("SELECT * FROM messages WHERE ingest_seq>? AND ingest_seq<=? AND received_at_us>=? "
                                + ("ORDER BY ingest_seq DESC LIMIT ?" if cursor is None else "ORDER BY ingest_seq LIMIT ?"),
                                (start, upper, time_micros(visible_from), limit if cursor is None else limit + 1)).fetchall()
            if cursor is None:
                rows.reverse()
            page = rows[:limit]
            more = len(rows) > len(page)
            last = conn.execute("SELECT message_id FROM messages WHERE ingest_seq=?", (start,)).fetchone()
            scanned = page[-1]["message_id"] if page else last[0] if last is not None and start == upper else None
            next_cursor = None
            if more:
                position = page[-1]["ingest_seq"]
                signature = hmac.new(secret, json.dumps([scope, position, upper,
                                     info["content_revision"]]).encode(), hashlib.sha256).hexdigest()
                state = [position, upper, info["content_revision"], signature]
                next_cursor = base64.urlsafe_b64encode(json.dumps(state).encode()).decode()
            partial = bool(info["gap"])
            return ArchivePage("incomplete" if partial else "found" if page else "empty",
                               tuple(GroupMessage.model_validate_json(row["payload"]) for row in page),
                               next_cursor, scanned, "known collection gap" if partial else None)


def message_reference(group_id: str, message_id: str) -> str:
    """Stable, bounded source identity even for long platform message IDs."""
    return f"{group_id}:{hashlib.sha256(message_id.encode()).hexdigest()}"


def time_micros(value: datetime) -> int:
    delta = utc_time(value) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
