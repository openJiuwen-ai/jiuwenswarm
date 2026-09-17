"""Persistent team entity bindings.

The store is intentionally small and local-file based: it records the stable
user-facing team entity name and the template used to construct runtime specs.
Runtime state is derived elsewhere and is not persisted here.
"""

from __future__ import annotations

import json
import re
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.paths import configure_openjiuwen_home, get_agent_teams_home

from jiuwenswarm.common.utils import get_agent_root_dir
from jiuwenswarm.common.utils import get_user_workspace_dir

TEAM_NAME_RE = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,64}$")


class TeamBindingStoreError(ValueError):
    """Validation or persistence error for team bindings."""

    def __init__(self, message: str, *, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TeamBinding:
    team_name: str
    template_id: str
    created_at: float
    updated_at: float
    session_ids: tuple[str, ...] = ()
    last_session_id: str = ""
    legacy: bool = False
    template_snapshot: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TeamBinding":
        session_ids_raw = data.get("session_ids", [])
        session_ids = tuple(
            str(item).strip()
            for item in session_ids_raw
            if str(item).strip()
        ) if isinstance(session_ids_raw, list) else ()
        return cls(
            team_name=str(data.get("team_name") or "").strip(),
            template_id=str(data.get("template_id") or "").strip(),
            created_at=float(data.get("created_at") or 0),
            updated_at=float(data.get("updated_at") or 0),
            session_ids=session_ids,
            last_session_id=str(data.get("last_session_id") or "").strip(),
            legacy=bool(data.get("legacy", False)),
            template_snapshot=(
                deepcopy(data.get("template_snapshot"))
                if isinstance(data.get("template_snapshot"), dict)
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_name": self.team_name,
            "template_id": self.template_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "session_ids": list(self.session_ids),
            "last_session_id": self.last_session_id,
            "legacy": self.legacy,
        }


def _replace_bound_sessions(
    binding: TeamBinding,
    *,
    session_ids: tuple[str, ...],
    last_session_id: str,
    updated_at: float,
) -> TeamBinding:
    return TeamBinding(
        team_name=binding.team_name,
        template_id=binding.template_id,
        created_at=binding.created_at,
        updated_at=updated_at,
        session_ids=session_ids,
        last_session_id=last_session_id,
        legacy=binding.legacy,
        template_snapshot=deepcopy(binding.template_snapshot) if binding.template_snapshot else None,
    )


def validate_team_name(team_name: str) -> str:
    normalized = str(team_name or "").strip()
    if not normalized:
        raise TeamBindingStoreError("team_name is required", code="BAD_REQUEST")
    if normalized in {".", ".."} or not TEAM_NAME_RE.fullmatch(normalized):
        raise TeamBindingStoreError(
            "team_name must be 1-64 characters without path separators or control characters",
            code="BAD_REQUEST",
        )
    return normalized


class TeamBindingStore:
    """File-backed team binding catalog."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            configure_openjiuwen_home(get_user_workspace_dir())
            self._path = get_agent_teams_home() / "bindings.json"
            self._migrate_legacy_file(get_agent_root_dir() / "teams" / "bindings.json")
        else:
            self._path = path
        self._lock = threading.RLock()

    def _migrate_legacy_file(self, legacy_path: Path) -> None:
        """Move the old agent/teams catalog when the new catalog is absent."""
        if self._path.exists() or not legacy_path.is_file():
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            legacy_path.replace(self._path)
        except FileNotFoundError:
            # Another process may have completed the migration first.
            if not self._path.exists():
                raise

    @property
    def path(self) -> Path:
        return self._path

    def list(self) -> list[TeamBinding]:
        with self._lock:
            data = self._read_unlocked()
            return sorted(data.values(), key=lambda item: item.updated_at, reverse=True)

    def get(self, team_name: str) -> TeamBinding | None:
        normalized = str(team_name or "").strip()
        if not normalized:
            return None
        with self._lock:
            return self._read_unlocked().get(normalized)

    def create(
        self,
        *,
        team_name: str,
        template_id: str,
        template_snapshot: dict[str, Any] | None = None,
        legacy: bool = False,
    ) -> TeamBinding:
        normalized_name = validate_team_name(team_name)
        normalized_template = str(template_id or "").strip()
        if not normalized_template:
            raise TeamBindingStoreError("template_id is required", code="BAD_REQUEST")

        now = time.time()
        with self._lock:
            data = self._read_unlocked()
            if normalized_name in data:
                raise TeamBindingStoreError("team_name already exists", code="CONFLICT")
            binding = TeamBinding(
                team_name=normalized_name,
                template_id=normalized_template,
                created_at=now,
                updated_at=now,
                legacy=legacy,
                template_snapshot=deepcopy(template_snapshot) if isinstance(template_snapshot, dict) else None,
            )
            data[normalized_name] = binding
            self._write_unlocked(data)
            return binding

    def bind_session(self, *, team_name: str, session_id: str) -> TeamBinding:
        normalized_name = str(team_name or "").strip()
        normalized_session = str(session_id or "").strip()
        if not normalized_name:
            raise TeamBindingStoreError("team_name is required", code="BAD_REQUEST")
        if not normalized_session:
            raise TeamBindingStoreError("session_id is required", code="BAD_REQUEST")

        with self._lock:
            data = self._read_unlocked()
            binding = data.get(normalized_name)
            if binding is None:
                raise TeamBindingStoreError("team binding not found", code="NOT_FOUND")

            now = time.time()
            changed = False
            for candidate_name, candidate in data.items():
                if candidate_name == normalized_name or normalized_session not in candidate.session_ids:
                    continue
                remaining = tuple(item for item in candidate.session_ids if item != normalized_session)
                last_session_id = candidate.last_session_id
                if last_session_id == normalized_session:
                    last_session_id = remaining[-1] if remaining else ""
                data[candidate_name] = _replace_bound_sessions(
                    candidate,
                    session_ids=remaining,
                    last_session_id=last_session_id,
                    updated_at=now,
                )
                changed = True

            session_ids = list(binding.session_ids)
            if normalized_session not in session_ids:
                session_ids.append(normalized_session)
                changed = True
            if binding.last_session_id != normalized_session:
                changed = True

            if changed:
                data[normalized_name] = _replace_bound_sessions(
                    binding,
                    session_ids=tuple(session_ids),
                    last_session_id=normalized_session,
                    updated_at=now,
                )
                self._write_unlocked(data)
            return data[normalized_name]

    def unbind_session(self, *, session_id: str, team_name: str | None = None) -> TeamBinding | None:
        normalized_session = str(session_id or "").strip()
        if not normalized_session:
            raise TeamBindingStoreError("session_id is required", code="BAD_REQUEST")
        normalized_name = str(team_name or "").strip()

        with self._lock:
            data = self._read_unlocked()
            candidates = [normalized_name] if normalized_name else list(data)
            updated_binding: TeamBinding | None = None
            now = time.time()
            for candidate in candidates:
                binding = data.get(candidate)
                if binding is None or normalized_session not in binding.session_ids:
                    continue
                session_ids = tuple(item for item in binding.session_ids if item != normalized_session)
                last_session_id = binding.last_session_id
                if last_session_id == normalized_session:
                    last_session_id = session_ids[-1] if session_ids else ""
                updated = _replace_bound_sessions(
                    binding,
                    session_ids=session_ids,
                    last_session_id=last_session_id,
                    updated_at=now,
                )
                data[binding.team_name] = updated
                if updated_binding is None:
                    updated_binding = updated

            if updated_binding is not None:
                self._write_unlocked(data)
            return updated_binding

    def delete(self, team_name: str) -> bool:
        normalized = str(team_name or "").strip()
        if not normalized:
            return False
        with self._lock:
            data = self._read_unlocked()
            existed = normalized in data
            if existed:
                data.pop(normalized, None)
                self._write_unlocked(data)
            return existed

    def _read_unlocked(self) -> dict[str, TeamBinding]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            raise TeamBindingStoreError(
                f"failed to read team bindings: {exc}",
                code="INTERNAL_ERROR",
            ) from exc
        items = raw.get("teams", raw) if isinstance(raw, dict) else {}
        if not isinstance(items, dict):
            return {}
        result: dict[str, TeamBinding] = {}
        for key, value in items.items():
            if not isinstance(value, dict):
                continue
            binding = TeamBinding.from_dict(value)
            if not binding.team_name:
                binding = TeamBinding.from_dict({**value, "team_name": key})
            if binding.team_name:
                result[binding.team_name] = binding
        return result

    def _write_unlocked(self, data: dict[str, TeamBinding]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "teams": {
                name: binding.to_dict()
                for name, binding in sorted(data.items())
            },
        }
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)


_DEFAULT_STORE: TeamBindingStore | None = None
_DEFAULT_STORE_LOCK = threading.Lock()


def get_team_binding_store() -> TeamBindingStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _DEFAULT_STORE_LOCK:
            if _DEFAULT_STORE is None:
                _DEFAULT_STORE = TeamBindingStore()
    return _DEFAULT_STORE
