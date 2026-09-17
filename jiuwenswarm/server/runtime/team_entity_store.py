# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent per-team entity metadata stored in the team workspace."""

from __future__ import annotations

import shutil
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from openjiuwen.agent_teams.paths import configure_openjiuwen_home, get_agent_teams_home, team_home
from jiuwenswarm.agents.harness.team.config_loader import (
    TeamTemplateNotFoundError,
    get_team_template_snapshot,
)
from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.server.runtime.team_binding_store import validate_team_name

configure_openjiuwen_home(get_user_workspace_dir())

TEAM_ENTITY_META_DIR = ".team-meta"
TEAM_ENTITY_FILE = "team.yaml"


class TeamEntityStoreError(ValueError):
    """Validation or persistence error for team entity metadata."""

    def __init__(self, message: str, *, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TeamEntity:
    team_name: str
    template_id: str
    created_at: float
    updated_at: float
    template_snapshot: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TeamEntity":
        snapshot = data.get("template_snapshot")
        if not isinstance(snapshot, dict) or not snapshot:
            raise TeamEntityStoreError("team entity template_snapshot is missing", code="INVALID_ENTITY")
        return cls(
            team_name=str(data.get("team_name") or "").strip(),
            template_id=str(data.get("template_id") or "").strip(),
            created_at=float(data.get("created_at") or 0),
            updated_at=float(data.get("updated_at") or 0),
            template_snapshot=deepcopy(snapshot),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "team_name": self.team_name,
            "template_id": self.template_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "template_snapshot": deepcopy(self.template_snapshot),
        }


def _yaml() -> YAML:
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    return yaml


class TeamEntityStore:
    """File-backed team entity metadata catalog."""

    def __init__(self, teams_home: Path | None = None) -> None:
        self._teams_home = teams_home
        self._lock = threading.RLock()

    def _team_path(self, team_name: str) -> Path:
        normalized = validate_team_name(team_name)
        return self._teams_home / normalized if self._teams_home is not None else team_home(normalized)

    def entity_path(self, team_name: str) -> Path:
        return self._team_path(team_name) / "team-workspace" / TEAM_ENTITY_META_DIR / TEAM_ENTITY_FILE

    def exists(self, team_name: str) -> bool:
        return self.entity_path(team_name).is_file()

    def get(self, team_name: str) -> TeamEntity | None:
        path = self.entity_path(team_name)
        if not path.is_file():
            return None
        with self._lock:
            return self._read_unlocked(path)

    def write(
        self,
        *,
        team_name: str,
        template_id: str,
        template_snapshot: dict[str, Any],
        created_at: float | None = None,
    ) -> TeamEntity:
        normalized_name = validate_team_name(team_name)
        normalized_template = str(template_id or "").strip()
        if not normalized_template:
            raise TeamEntityStoreError("template_id is required", code="BAD_REQUEST")
        if not isinstance(template_snapshot, dict) or not template_snapshot:
            raise TeamEntityStoreError("template_snapshot is required", code="BAD_REQUEST")

        path = self.entity_path(normalized_name)
        now = time.time()
        with self._lock:
            existing = self._read_unlocked(path) if path.is_file() else None
            entity = TeamEntity(
                team_name=normalized_name,
                template_id=normalized_template,
                created_at=existing.created_at if existing is not None else float(created_at or now),
                updated_at=now,
                template_snapshot=deepcopy(template_snapshot),
            )
            self._write_unlocked(path, entity)
            return entity

    def ensure(
        self,
        *,
        team_name: str,
        template_id: str,
        template_snapshot: dict[str, Any],
        created_at: float | None = None,
    ) -> TeamEntity:
        existing = self.get(team_name)
        if existing is not None:
            return existing
        return self.write(
            team_name=team_name,
            template_id=template_id,
            template_snapshot=template_snapshot,
            created_at=created_at,
        )

    def delete(self, team_name: str) -> bool:
        team_path = self._team_path(team_name)
        path = self.entity_path(team_name)
        with self._lock:
            if team_path.is_symlink() or not path.is_file():
                return False
            path.unlink()
            return True

    def delete_team_directory(self, team_name: str) -> bool:
        team_path = self._team_path(team_name)
        with self._lock:
            if team_path.is_symlink():
                team_path.unlink()
                return True
            if not team_path.exists():
                return False
            if not team_path.is_dir():
                raise TeamEntityStoreError("team entity path is not a directory", code="INVALID_ENTITY")
            try:
                shutil.rmtree(team_path)
                return True
            except OSError as exc:
                raise TeamEntityStoreError(
                    f"failed to delete team entity directory: {exc}",
                    code="INTERNAL_ERROR",
                ) from exc

    @staticmethod
    def default_teams_home() -> Path:
        return get_agent_teams_home()

    @staticmethod
    def _read_unlocked(path: Path) -> TeamEntity | None:
        try:
            data = _yaml().load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            raise TeamEntityStoreError(
                f"failed to read team entity metadata: {exc}",
                code="INTERNAL_ERROR",
            ) from exc
        if not isinstance(data, dict):
            raise TeamEntityStoreError("team entity metadata must be an object", code="INVALID_ENTITY")
        entity = TeamEntity.from_dict(data)
        if not entity.team_name:
            entity = TeamEntity(
                team_name=path.parent.parent.parent.name,
                template_id=entity.template_id,
                created_at=entity.created_at,
                updated_at=entity.updated_at,
                template_snapshot=entity.template_snapshot,
            )
        return entity

    @staticmethod
    def _write_unlocked(path: Path, entity: TeamEntity) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as stream:
                _yaml().dump(entity.to_dict(), stream)
            tmp_path.replace(path)
        except OSError as exc:
            raise TeamEntityStoreError(
                f"failed to write team entity metadata: {exc}",
                code="INTERNAL_ERROR",
            ) from exc


_DEFAULT_STORE: TeamEntityStore | None = None
_DEFAULT_STORE_LOCK = threading.Lock()


def get_team_entity_store() -> TeamEntityStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _DEFAULT_STORE_LOCK:
            if _DEFAULT_STORE is None:
                _DEFAULT_STORE = TeamEntityStore()
    return _DEFAULT_STORE


def ensure_team_entity(
    *,
    team_name: str,
    template_id: str,
    template_snapshot: dict[str, Any] | None = None,
    config_base: dict[str, Any] | None = None,
    created_at: float | None = None,
    store: TeamEntityStore | None = None,
) -> TeamEntity | None:
    entity_store = store or get_team_entity_store()
    existing = entity_store.get(team_name)
    if existing is not None:
        return existing

    snapshot = deepcopy(template_snapshot) if isinstance(template_snapshot, dict) and template_snapshot else None
    normalized_template = str(template_id or "").strip()
    if snapshot is None and normalized_template:
        try:
            snapshot = get_team_template_snapshot(config_base, template_id=normalized_template)
        except TeamTemplateNotFoundError:
            snapshot = None

    if not snapshot or not normalized_template:
        return None
    return entity_store.write(
        team_name=team_name,
        template_id=normalized_template,
        template_snapshot=snapshot,
        created_at=created_at,
    )


def ensure_team_entity_for_binding(
    binding: Any,
    *,
    config_base: dict[str, Any] | None = None,
    store: TeamEntityStore | None = None,
) -> TeamEntity | None:
    team_name = str(getattr(binding, "team_name", "") or "").strip()
    template_id = str(getattr(binding, "template_id", "") or "").strip()
    template_snapshot = getattr(binding, "template_snapshot", None)
    created_at = getattr(binding, "created_at", None)
    if not team_name:
        return None
    return ensure_team_entity(
        team_name=team_name,
        template_id=template_id,
        template_snapshot=template_snapshot if isinstance(template_snapshot, dict) else None,
        config_base=config_base,
        created_at=float(created_at) if isinstance(created_at, (int, float)) else None,
        store=store,
    )
