from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.workspace_provider import (
    SkillWorkspaceProvider,
    SkillWorkspaceUnavailable,
)


def test_managers_are_reused_only_within_initialized_user_workspace(
    tmp_path: Path,
) -> None:
    provider = SkillWorkspaceProvider()
    created: list[Path] = []

    def factory(ready):
        created.append(ready.workspace_dir)
        return _Manager()

    first, first_created = provider.get_or_create_manager(
        tmp_path / "user-a" / "agent",
        require_valid_state=True,
        factory=factory,
    )
    repeated, repeated_created = provider.get_or_create_manager(
        tmp_path / "user-a" / "agent",
        require_valid_state=True,
        factory=factory,
    )
    other, other_created = provider.get_or_create_manager(
        tmp_path / "user-b" / "agent",
        require_valid_state=True,
        factory=factory,
    )

    state_file = tmp_path / "user-a" / "agent" / "skills" / "skills_state.json"
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "marketplaces": [],
        "installed_plugins": [],
        "local_skills": [],
        "skill_configs": {},
    }
    assert first is repeated
    assert other is not first
    assert (first_created, repeated_created, other_created) == (True, False, True)
    assert created == [
        (tmp_path / "user-a" / "agent").resolve(),
        (tmp_path / "user-b" / "agent").resolve(),
    ]


def test_enterprise_validation_rejects_invalid_state(tmp_path: Path) -> None:
    workspace = tmp_path / "user-a" / "agent"
    state_file = workspace / "skills" / "skills_state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("[]", encoding="utf-8")

    with pytest.raises(SkillWorkspaceUnavailable, match="must contain an object"):
        SkillWorkspaceProvider().ensure(workspace, require_valid_state=True)


class _Manager:
    pass
