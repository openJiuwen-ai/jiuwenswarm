# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Resolve and prepare the workspace before constructing ``SkillManager``."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from weakref import WeakValueDictionary


class SkillWorkspaceUnavailable(RuntimeError):
    """The requested workspace cannot safely host Skill state."""

    code = "workspace_unavailable"


@dataclass(frozen=True)
class SkillWorkspace:
    workspace_dir: Path
    skills_dir: Path
    state_file: Path
    existed: bool


class SkillWorkspaceProvider:
    """Idempotently prepare one explicit workspace without fallback routing."""

    _manager_lock = threading.RLock()
    _managers: "WeakValueDictionary[str, Any]" = WeakValueDictionary()

    _EMPTY_STATE = {
        "marketplaces": [],
        "installed_plugins": [],
        "local_skills": [],
        "skill_configs": {},
    }

    def ensure(
        self,
        workspace_dir: str | Path,
        *,
        require_valid_state: bool,
    ) -> SkillWorkspace:
        raw = str(workspace_dir or "").strip()
        if not raw:
            raise SkillWorkspaceUnavailable("workspace path is empty")
        workspace = Path(raw).expanduser().resolve()
        skills_dir = workspace / "skills"
        state_file = skills_dir / "skills_state.json"
        existed = workspace.is_dir() and skills_dir.is_dir()
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            skills_dir.mkdir(parents=True, exist_ok=True)
            if state_file.exists():
                if require_valid_state:
                    with state_file.open("r", encoding="utf-8") as stream:
                        state = json.load(stream)
                    if not isinstance(state, dict):
                        raise SkillWorkspaceUnavailable(
                            "skills_state.json must contain an object"
                        )
            else:
                fd, temp_name = tempfile.mkstemp(
                    prefix=".skills_state.", suffix=".tmp", dir=str(skills_dir)
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(self._EMPTY_STATE, stream, ensure_ascii=False, indent=2)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp_name, state_file)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
            # Opening r+ proves the authoritative ledger is both readable and
            # writable without changing its contents.
            with state_file.open("r+", encoding="utf-8"):
                pass
        except (OSError, ValueError) as exc:
            raise SkillWorkspaceUnavailable(
                f"workspace is not ready: {workspace}: {exc}"
            ) from exc
        return SkillWorkspace(
            workspace_dir=workspace,
            skills_dir=skills_dir,
            state_file=state_file,
            existed=existed,
        )

    def get_or_create_manager(
        self,
        workspace_dir: str | Path,
        *,
        require_valid_state: bool,
        factory: Callable[[SkillWorkspace], Any],
    ) -> tuple[Any, bool]:
        """Reuse a manager by verified workspace path or create it exactly once."""
        ready = self.ensure(
            workspace_dir,
            require_valid_state=require_valid_state,
        )
        key = os.path.normcase(str(ready.workspace_dir))
        with self._manager_lock:
            existing = self._managers.get(key)
            if existing is not None:
                return existing, False
            manager = factory(ready)
            self._managers[key] = manager
            return manager, True
