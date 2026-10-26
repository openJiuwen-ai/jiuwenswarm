"""JiuwenMate-owned storage: colleague definitions, published versions and per-colleague Templates.

Everything lives under the JiuwenMate data directory. Personal-edition agents,
expert packages and the expert marketplace are never read or written here.
Published snapshots may contain model credentials, so the database is private
to the current user.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from jiuwenswarm.server.jiuwenmate.models import (
    ColleagueDefinition,
    ColleagueSettings,
    ConnectionRecord,
    PersistentAgentError,
)
from jiuwenswarm.server.jiuwenmate.spec_provider import ConfigSnapshot
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore, private_dir
from openjiuwen.harness.resources.extension_loader import find_agent_template_manifest, load_agent_template_package

_SCHEMA = """
CREATE TABLE IF NOT EXISTS colleagues (
    namespace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    source_template_id TEXT,
    is_default INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL,
    published_revision INTEGER,
    current_version TEXT,
    next_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, agent_id)
);
CREATE TABLE IF NOT EXISTS config_versions (
    namespace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    config_version TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, agent_id, config_version)
);
CREATE TABLE IF NOT EXISTS namespace_defaults (
    namespace_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    provisioned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS colleague_connections (
    namespace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    connection_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, agent_id),
    UNIQUE (namespace_id, config_id, channel, connection_key)
);
CREATE TABLE IF NOT EXISTS removed_colleagues (
    namespace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    removed_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, agent_id)
);
"""

_TEMPLATE_DIR_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Web and Feishu services share these files in the owner process. Do not read a
# replacement persona before its settings revision is committed or rolled back.
_TEMPLATE_SAVE_LOCK = RLock()


class PersistentAgentRepository(SQLiteStore):
    """Colleague configurations, their IM agents and the published configuration versions.

    A configuration's row is its Web agent. Each IM agent has its own row plus one
    ``colleague_connections`` row naming the configuration and the platform app it serves.
    """

    _schema = _SCHEMA

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        with closing(self._connect()) as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(colleagues)")}
            if "enabled" not in columns:
                conn.execute("ALTER TABLE colleagues ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
            if "hidden" not in columns:
                conn.execute("ALTER TABLE colleagues ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            if "published_settings_json" not in columns:
                conn.execute("ALTER TABLE colleagues ADD COLUMN published_settings_json TEXT")

    @staticmethod
    def _definition(row: sqlite3.Row) -> ColleagueDefinition:
        published = row["published_settings_json"]
        return ColleagueDefinition(
            namespace_id=row["namespace_id"],
            agent_id=row["agent_id"],
            settings=ColleagueSettings.from_dict(json.loads(row["settings_json"])),
            source_template_id=row["source_template_id"],
            is_default=bool(row["is_default"]),
            enabled=bool(row["enabled"]),
            revision=row["revision"],
            published_revision=row["published_revision"],
            current_version=row["current_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            hidden=bool(row["hidden"]),
            published_settings=None if published is None else ColleagueSettings.from_dict(json.loads(published)),
        )

    @staticmethod
    def _connection(row: sqlite3.Row) -> ConnectionRecord:
        return ConnectionRecord(
            namespace_id=row["namespace_id"],
            agent_id=row["agent_id"],
            config_id=row["config_id"],
            channel=row["channel"],
            connection_key=row["connection_key"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _insert_row(conn: sqlite3.Connection, definition: ColleagueDefinition) -> None:
        conn.execute(
            "INSERT INTO colleagues (namespace_id, agent_id, settings_json, source_template_id, is_default, enabled,"
            " revision, published_revision, current_version, next_version, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1, ?, ?)",
            (
                definition.namespace_id,
                definition.agent_id,
                json.dumps(definition.settings.to_dict(), ensure_ascii=False),
                definition.source_template_id,
                int(definition.is_default),
                int(definition.enabled),
                definition.revision,
                definition.created_at.isoformat(),
                definition.updated_at.isoformat(),
            ),
        )

    @staticmethod
    def _insert_version(
        conn: sqlite3.Connection, namespace_id: str, snapshot: ConfigSnapshot, published_at: datetime,
    ) -> None:
        conn.execute(
            "INSERT INTO config_versions (namespace_id, agent_id, config_version, snapshot_json, published_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                namespace_id,
                snapshot.agent_id,
                snapshot.config_version,
                json.dumps(snapshot.to_dict(), ensure_ascii=False),
                published_at.isoformat(),
            ),
        )

    def insert(self, definition: ColleagueDefinition, *, claim_default: bool = False) -> bool:
        """Insert a new colleague; with ``claim_default`` it fails softly if the namespace already has one."""
        with self._transaction() as conn:
            if claim_default:
                if conn.execute(
                    "SELECT 1 FROM namespace_defaults WHERE namespace_id = ?", (definition.namespace_id,)
                ).fetchone():
                    return False
                conn.execute(
                    "INSERT INTO namespace_defaults (namespace_id, agent_id, provisioned_at) VALUES (?, ?, ?)",
                    (definition.namespace_id, definition.agent_id, definition.created_at.isoformat()),
                )
            self._insert_row(conn, definition)
        return True

    def insert_connection_agent(
        self, definition: ColleagueDefinition, connection: ConnectionRecord,
    ) -> ConnectionRecord:
        """Add an IM agent for one app of a configuration, or return the agent already added for it.

        The configuration is checked in the same transaction, so an agent is never added to a
        configuration that was deleted or hidden after the caller looked at it.
        """
        with self._transaction() as conn:
            config = conn.execute(
                "SELECT hidden FROM colleagues WHERE namespace_id = ? AND agent_id = ?",
                (connection.namespace_id, connection.config_id),
            ).fetchone()
            if config is None or config["hidden"] or conn.execute(
                "SELECT 1 FROM colleague_connections WHERE namespace_id = ? AND agent_id = ?",
                (connection.namespace_id, connection.config_id),
            ).fetchone():
                raise PersistentAgentError("colleague not found", code="NOT_FOUND")
            row = conn.execute(
                "SELECT * FROM colleague_connections"
                " WHERE namespace_id = ? AND config_id = ? AND channel = ? AND connection_key = ?",
                (connection.namespace_id, connection.config_id, connection.channel, connection.connection_key),
            ).fetchone()
            if row is not None:
                return self._connection(row)
            self._insert_row(conn, definition)
            conn.execute(
                "INSERT INTO colleague_connections"
                " (namespace_id, agent_id, config_id, channel, connection_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    connection.namespace_id,
                    connection.agent_id,
                    connection.config_id,
                    connection.channel,
                    connection.connection_key,
                    connection.created_at.isoformat(),
                ),
            )
        return connection

    def connection_of(self, namespace_id: str, agent_id: str) -> ConnectionRecord | None:
        """The app an IM agent serves; a configuration's own row has none."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM colleague_connections WHERE namespace_id = ? AND agent_id = ?", (namespace_id, agent_id)
            ).fetchone()
        return None if row is None else self._connection(row)

    def connections(self, namespace_id: str, config_id: str | None = None) -> list[ConnectionRecord]:
        """IM connections of one configuration, or of the whole namespace, oldest first."""
        query = "SELECT * FROM colleague_connections WHERE namespace_id = ?"
        args: tuple[str, ...] = (namespace_id,)
        if config_id is not None:
            query += " AND config_id = ?"
            args += (config_id,)
        with closing(self._connect()) as conn:
            rows = conn.execute(query + " ORDER BY created_at, agent_id", args).fetchall()
        return [self._connection(row) for row in rows]

    def default_agent_id(self, namespace_id: str) -> str | None:
        """The colleague provisioned as default, kept even after that colleague is deleted."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT agent_id FROM namespace_defaults WHERE namespace_id = ?", (namespace_id,)
            ).fetchone()
        return None if row is None else row["agent_id"]

    def get(self, namespace_id: str, agent_id: str) -> ColleagueDefinition | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM colleagues WHERE namespace_id = ? AND agent_id = ?", (namespace_id, agent_id)
            ).fetchone()
        return None if row is None else self._definition(row)

    def list(self, namespace_id: str) -> list[ColleagueDefinition]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM colleagues WHERE namespace_id = ? ORDER BY created_at, agent_id", (namespace_id,)
            ).fetchall()
        return [self._definition(row) for row in rows]

    def update_settings(
        self,
        namespace_id: str,
        agent_id: str,
        settings: ColleagueSettings,
        *,
        expected_revision: int,
        updated_at: datetime,
    ) -> ColleagueDefinition:
        """Store new settings only if nobody changed the colleague since ``expected_revision``."""
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE colleagues SET settings_json = ?, revision = revision + 1, updated_at = ?"
                " WHERE namespace_id = ? AND agent_id = ? AND revision = ?",
                (
                    json.dumps(settings.to_dict(), ensure_ascii=False),
                    updated_at.isoformat(),
                    namespace_id,
                    agent_id,
                    expected_revision,
                ),
            ).rowcount
            if not changed:
                raise PersistentAgentError("colleague was changed concurrently", code="CONFLICT")
            row = conn.execute(
                "SELECT * FROM colleagues WHERE namespace_id = ? AND agent_id = ?", (namespace_id, agent_id)
            ).fetchone()
        return self._definition(row)

    def set_enabled(self, namespace_id: str, agent_id: str, *, enabled: bool, updated_at: datetime) -> ColleagueDefinition:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE colleagues SET enabled = ?, updated_at = ? WHERE namespace_id = ? AND agent_id = ?",
                (int(enabled), updated_at.isoformat(), namespace_id, agent_id),
            ).rowcount
            if not changed:
                raise PersistentAgentError("colleague not found", code="NOT_FOUND")
            row = conn.execute(
                "SELECT * FROM colleagues WHERE namespace_id = ? AND agent_id = ?", (namespace_id, agent_id)
            ).fetchone()
        return self._definition(row)

    def next_version(self, namespace_id: str, agent_id: str) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT next_version FROM colleagues WHERE namespace_id = ? AND agent_id = ?",
                (namespace_id, agent_id),
            ).fetchone()
        if row is None:
            raise PersistentAgentError("colleague not found", code="NOT_FOUND")
        return f"v{row['next_version']}"

    def save_capability_version(
        self, namespace_id: str, snapshot: ConfigSnapshot, *, previous_version: str, published_at: datetime,
    ) -> None:
        """Advance only the published capability snapshot, preserving all draft revisions."""
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE colleagues SET current_version = ?, next_version = next_version + 1"
                " WHERE namespace_id = ? AND agent_id = ? AND current_version = ? AND 'v' || next_version = ?",
                (snapshot.config_version, namespace_id, snapshot.agent_id, previous_version, snapshot.config_version),
            ).rowcount
            if not changed:
                raise PersistentAgentError("colleague was changed concurrently", code="CONFLICT")
            conn.execute(
                "INSERT INTO config_versions (namespace_id, agent_id, config_version, snapshot_json, published_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (namespace_id, snapshot.agent_id, snapshot.config_version,
                 json.dumps(snapshot.to_dict(), ensure_ascii=False), published_at.isoformat()),
            )

    def publish_configuration(
        self,
        namespace_id: str,
        config_id: str,
        *,
        revision: int,
        published_settings: ColleagueSettings,
        changes: list[tuple[str, str | None, ConfigSnapshot | None]],
        published_at: datetime,
    ) -> None:
        """Switch every agent of a configuration to one publication, or none of them.

        ``changes`` names each agent with the version it had when the publication was
        prepared and its new snapshot, or ``None`` if its version stays. The configuration
        must still be at ``revision`` and own exactly these agents.
        """
        settings_json = json.dumps(published_settings.with_persona("").to_dict(), ensure_ascii=False)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT revision FROM colleagues WHERE namespace_id = ? AND agent_id = ?", (namespace_id, config_id)
            ).fetchone()
            owned = {
                item["agent_id"] for item in conn.execute(
                    "SELECT agent_id FROM colleague_connections WHERE namespace_id = ? AND config_id = ?",
                    (namespace_id, config_id),
                )
            }
            if row is None or row["revision"] != revision or owned | {config_id} != {item[0] for item in changes}:
                raise PersistentAgentError("colleague was changed concurrently", code="CONFLICT")
            for agent_id, previous, snapshot in changes:
                # The configuration's own row keeps its draft; an IM agent's row records what it now runs.
                fields = "published_revision = ?" if agent_id == config_id else (
                    "published_revision = ?, revision = ?, settings_json = ?"
                )
                values: tuple = (revision,) if agent_id == config_id else (revision, revision, settings_json)
                condition = " WHERE namespace_id = ? AND agent_id = ? AND current_version IS ?"
                keys: tuple = (namespace_id, agent_id, previous)
                if snapshot is not None:
                    fields += ", current_version = ?, next_version = next_version + 1"
                    values += (snapshot.config_version,)
                    condition += " AND 'v' || next_version = ?"
                    keys += (snapshot.config_version,)
                if not conn.execute(f"UPDATE colleagues SET {fields}{condition}", values + keys).rowcount:
                    raise PersistentAgentError("colleague was changed concurrently", code="CONFLICT")
                if snapshot is not None:
                    self._insert_version(conn, namespace_id, snapshot, published_at)
            conn.execute(
                "UPDATE colleagues SET published_settings_json = ? WHERE namespace_id = ? AND agent_id = ?",
                (json.dumps(published_settings.to_dict(), ensure_ascii=False), namespace_id, config_id),
            )

    def publish_connection_agent(
        self,
        namespace_id: str,
        snapshot: ConfigSnapshot,
        *,
        config_id: str,
        config_revision: int,
        settings: ColleagueSettings,
        published_at: datetime,
    ) -> None:
        """Give a new IM agent its first version, refusing if the configuration was published meanwhile."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT published_revision FROM colleagues WHERE namespace_id = ? AND agent_id = ?",
                (namespace_id, config_id),
            ).fetchone()
            changed = row is not None and row["published_revision"] == config_revision and conn.execute(
                "UPDATE colleagues SET current_version = ?, next_version = next_version + 1, revision = ?,"
                " published_revision = ?, settings_json = ?"
                " WHERE namespace_id = ? AND agent_id = ? AND current_version IS NULL AND 'v' || next_version = ?",
                (
                    snapshot.config_version,
                    config_revision,
                    config_revision,
                    json.dumps(settings.with_persona("").to_dict(), ensure_ascii=False),
                    namespace_id,
                    snapshot.agent_id,
                    snapshot.config_version,
                ),
            ).rowcount
            if not changed:
                raise PersistentAgentError("colleague was changed concurrently", code="CONFLICT")
            self._insert_version(conn, namespace_id, snapshot, published_at)

    def hide(self, namespace_id: str, config_id: str, *, updated_at: datetime) -> None:
        """Take a configuration off the list and disable all its agents; its data stays."""
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE colleagues SET hidden = 1, enabled = 0, updated_at = ? WHERE namespace_id = ? AND agent_id = ?",
                (updated_at.isoformat(), namespace_id, config_id),
            ).rowcount
            if not changed:
                raise PersistentAgentError("colleague not found", code="NOT_FOUND")

    def get_version(
        self, namespace_id: str, agent_id: str, config_version: str
    ) -> tuple[ConfigSnapshot, datetime] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT snapshot_json, published_at FROM config_versions"
                " WHERE namespace_id = ? AND agent_id = ? AND config_version = ?",
                (namespace_id, agent_id, config_version),
            ).fetchone()
        if row is None:
            return None
        return ConfigSnapshot.from_dict(json.loads(row["snapshot_json"])), datetime.fromisoformat(row["published_at"])

    def delete(self, namespace_id: str, agent_id: str, *, record_removal: bool = False) -> bool:
        """Remove the colleague and all its versions; the namespace default claim stays.

        A configuration that still has IM connections is not removed, so none of them is left
        without its colleague. ``record_removal`` keeps the ID, so a repeated deletion can tell
        that it already happened.
        """
        with self._transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM colleague_connections WHERE namespace_id = ? AND config_id = ?", (namespace_id, agent_id)
            ).fetchone():
                raise PersistentAgentError("colleague still has connections", code="CONFLICT")
            removed = conn.execute(
                "DELETE FROM colleagues WHERE namespace_id = ? AND agent_id = ?", (namespace_id, agent_id)
            ).rowcount
            conn.execute(
                "DELETE FROM config_versions WHERE namespace_id = ? AND agent_id = ?", (namespace_id, agent_id)
            )
            conn.execute(
                "DELETE FROM colleague_connections WHERE namespace_id = ? AND agent_id = ?", (namespace_id, agent_id)
            )
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_group_access'").fetchone():
                conn.execute("DELETE FROM agent_group_access WHERE namespace_id=? AND agent_id=?", (namespace_id, agent_id))
            if removed and record_removal:
                conn.execute(
                    "INSERT OR IGNORE INTO removed_colleagues (namespace_id, agent_id, removed_at) VALUES (?, ?, ?)",
                    (namespace_id, agent_id, datetime.now(timezone.utc).isoformat()),
                )
        return bool(removed)

    def was_removed(self, namespace_id: str, agent_id: str) -> bool:
        """Whether a configuration with this ID was deleted with its removal recorded."""
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT 1 FROM removed_colleagues WHERE namespace_id = ? AND agent_id = ?", (namespace_id, agent_id)
            ).fetchone() is not None


class TemplateStore:
    """One AgentTemplate package per colleague, in the standard manifest / persona layout."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        private_dir(self._root)

    def path(self, agent_id: str) -> Path:
        if not _TEMPLATE_DIR_NAME.fullmatch(agent_id):
            raise PersistentAgentError("invalid agent_id for a Template directory")
        return self._root / agent_id

    @staticmethod
    def _write(pkg_dir: Path, agent_id: str, settings: ColleagueSettings, source_template_id: str | None) -> None:
        persona_dir = pkg_dir / "persona"
        persona_dir.mkdir(parents=True)
        (persona_dir / f"{agent_id}.md").write_text(settings.persona, encoding="utf-8")
        description = settings.description or settings.name
        manifest = {
            "package_type": "agent_template",
            "name": agent_id,
            "description": description,
            "persona": {"dir": "./persona"},
            "display_name": {"zh": settings.name, "en": settings.name},
            "display_description": {"zh": description, "en": description},
            "tags": [{"zh": tag, "en": tag} for tag in settings.tags],
            "quick_inputs": [{"zh": text, "en": text} for text in settings.capabilities.starter_prompts],
            "metadata": {"jiuwenmate": {"category": settings.category, "source_template_id": source_template_id}},
        }
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        load_agent_template_package(find_agent_template_manifest(pkg_dir))

    def _stage(self, agent_id: str, settings: ColleagueSettings, source_template_id: str | None) -> Path:
        staged = self._root / f".staging-{agent_id}-{uuid.uuid4().hex}"
        try:
            self._write(staged, agent_id, settings, source_template_id)
        except Exception as exc:
            shutil.rmtree(staged, ignore_errors=True)
            raise PersistentAgentError(f"Template could not be written: {exc}", code="TEMPLATE_INVALID") from exc
        return staged

    @contextmanager
    def create(self, agent_id: str, settings: ColleagueSettings, source_template_id: str | None) -> Iterator[Path]:
        """Write a new Template; it is removed again if the enclosed block fails."""
        target = self.path(agent_id)
        if target.exists():
            raise PersistentAgentError("Template directory already exists", code="CONFLICT")
        staged = self._stage(agent_id, settings, source_template_id)
        staged.rename(target)
        try:
            yield target
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise

    @contextmanager
    def replace(self, agent_id: str, settings: ColleagueSettings, source_template_id: str | None) -> Iterator[Path]:
        """Swap in a rewritten Template; the previous one is restored if the enclosed block fails."""
        target = self.path(agent_id)
        staged = self._stage(agent_id, settings, source_template_id)
        backup = self._root / f".backup-{agent_id}-{uuid.uuid4().hex}"
        with _TEMPLATE_SAVE_LOCK:
            had_previous = target.exists()
            if had_previous:
                target.rename(backup)
            staged.rename(target)
            try:
                yield target
            except BaseException:
                shutil.rmtree(target, ignore_errors=True)
                if had_previous:
                    backup.rename(target)
                raise
            if had_previous:
                shutil.rmtree(backup, ignore_errors=True)

    def read_persona(self, agent_id: str) -> str:
        with _TEMPLATE_SAVE_LOCK:
            persona = self.path(agent_id) / "persona" / f"{agent_id}.md"
            if not persona.is_file():
                raise PersistentAgentError("Template persona is missing", code="TEMPLATE_INVALID")
            return persona.read_text(encoding="utf-8")

    def remove(self, agent_id: str) -> None:
        shutil.rmtree(self.path(agent_id), ignore_errors=True)
