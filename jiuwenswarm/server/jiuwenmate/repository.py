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
from datetime import datetime
from pathlib import Path

from jiuwenswarm.server.jiuwenmate.models import (
    ColleagueDefinition,
    ColleagueSettings,
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
"""

_TEMPLATE_DIR_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class PersistentAgentRepository(SQLiteStore):
    """Colleague definitions and their published configuration versions."""

    _schema = _SCHEMA

    @staticmethod
    def _definition(row: sqlite3.Row) -> ColleagueDefinition:
        return ColleagueDefinition(
            namespace_id=row["namespace_id"],
            agent_id=row["agent_id"],
            settings=ColleagueSettings.from_dict(json.loads(row["settings_json"])),
            source_template_id=row["source_template_id"],
            is_default=bool(row["is_default"]),
            revision=row["revision"],
            published_revision=row["published_revision"],
            current_version=row["current_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
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
            conn.execute(
                "INSERT INTO colleagues (namespace_id, agent_id, settings_json, source_template_id, is_default,"
                " revision, published_revision, current_version, next_version, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 1, ?, ?)",
                (
                    definition.namespace_id,
                    definition.agent_id,
                    json.dumps(definition.settings.to_dict(), ensure_ascii=False),
                    definition.source_template_id,
                    int(definition.is_default),
                    definition.revision,
                    definition.created_at.isoformat(),
                    definition.updated_at.isoformat(),
                ),
            )
        return True

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

    def next_version(self, namespace_id: str, agent_id: str) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT next_version FROM colleagues WHERE namespace_id = ? AND agent_id = ?",
                (namespace_id, agent_id),
            ).fetchone()
        if row is None:
            raise PersistentAgentError("colleague not found", code="NOT_FOUND")
        return f"v{row['next_version']}"

    def save_version(
        self,
        namespace_id: str,
        snapshot: ConfigSnapshot,
        *,
        revision: int,
        published_at: datetime,
    ) -> None:
        """Add a version and make it current, refusing if settings or numbering moved meanwhile."""
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE colleagues SET current_version = ?, published_revision = ?, next_version = next_version + 1"
                " WHERE namespace_id = ? AND agent_id = ? AND revision = ? AND 'v' || next_version = ?",
                (
                    snapshot.config_version,
                    revision,
                    namespace_id,
                    snapshot.agent_id,
                    revision,
                    snapshot.config_version,
                ),
            ).rowcount
            if not changed:
                raise PersistentAgentError("colleague was changed concurrently", code="CONFLICT")
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

    def mark_published(self, namespace_id: str, agent_id: str, *, revision: int) -> None:
        """Record that ``revision`` publishes to the current version unchanged."""
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE colleagues SET published_revision = ? WHERE namespace_id = ? AND agent_id = ? AND revision = ?",
                (revision, namespace_id, agent_id, revision),
            ).rowcount
            if not changed:
                raise PersistentAgentError("colleague was changed concurrently", code="CONFLICT")

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

    def delete(self, namespace_id: str, agent_id: str) -> bool:
        """Remove the colleague and all its versions; the namespace default claim stays."""
        with self._transaction() as conn:
            removed = conn.execute(
                "DELETE FROM colleagues WHERE namespace_id = ? AND agent_id = ?", (namespace_id, agent_id)
            ).rowcount
            conn.execute(
                "DELETE FROM config_versions WHERE namespace_id = ? AND agent_id = ?", (namespace_id, agent_id)
            )
        return bool(removed)


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
        persona = self.path(agent_id) / "persona" / f"{agent_id}.md"
        if not persona.is_file():
            raise PersistentAgentError("Template persona is missing", code="TEMPLATE_INVALID")
        return persona.read_text(encoding="utf-8")

    def remove(self, agent_id: str) -> None:
        shutil.rmtree(self.path(agent_id), ignore_errors=True)
