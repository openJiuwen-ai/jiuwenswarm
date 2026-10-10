# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-request Skill availability (``params.agent_skills_available``).

``process_message_impl`` / ``process_message_stream_impl`` call
``_ensure_chat_extensions`` before the turn, and that hook applies the
per-request equipment fields. These tests drive it with a real
``SkillUseRail`` over a real Skill directory, then read the roster back
through ``get_skills_for_session`` — the callable ``skill_tool`` and
``list_skill`` resolve a Skill name through, so a roster assertion here is a
dispatch assertion, not only a prompt assertion.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import openjiuwen.agent_evolving.trajectory as _traj_mod

if not hasattr(_traj_mod, "InMemoryTrajectoryRegistry"):
    _traj_mod.InMemoryTrajectoryRegistry = MagicMock

from openjiuwen.harness.rails import SkillUseRail  # noqa: E402

from jiuwenswarm.common.schema.agent import AgentRequest  # noqa: E402
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (  # noqa: E402
    JiuWenSwarmDeepAdapter,
)

_SKILL_MD = "---\nname: {name}\ndescription: {name} skill\n---\n\n# {name}\n"


def _install_skills(root: Path, *names: str) -> None:
    for name in names:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            _SKILL_MD.format(name=name), encoding="utf-8"
        )


@pytest.fixture(name="adapter")
def _adapter(tmp_path: Path) -> JiuWenSwarmDeepAdapter:
    """A deep adapter whose Skill rail sees two installed Skills."""
    skills_dir = tmp_path / "skills"
    _install_skills(skills_dir, "alpha", "beta")

    adapter = JiuWenSwarmDeepAdapter()
    adapter._skill_rail = SkillUseRail(
        skills_dir=[str(skills_dir)],
        skill_mode=SkillUseRail.SKILL_MODE_ALL,
        include_tools=False,
    )
    # _clear_skill_session_baseline reaches for the live loop session.
    adapter._instance = SimpleNamespace(
        _loop_session=SimpleNamespace(update_state=MagicMock())
    )
    return adapter


def _request(**params: object) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id="chan-1",
        session_id="sess-1",
        params={"mode": "agent", **params},
    )


async def _roster(adapter: JiuWenSwarmDeepAdapter) -> list[str]:
    rail = adapter._skill_rail
    await rail.reload_skills()
    return sorted(skill.name for skill in rail.get_skills_for_session(None))


async def test_absent_key_keeps_every_installed_skill(
    adapter: JiuWenSwarmDeepAdapter,
) -> None:
    """A request that says nothing about Skills is unrestricted."""
    assert await adapter._ensure_chat_extensions(_request()) is None

    assert await _roster(adapter) == ["alpha", "beta"]
    assert adapter._skill_rail.enabled_skills == set()


async def test_named_skills_are_the_only_ones_dispatchable(
    adapter: JiuWenSwarmDeepAdapter,
) -> None:
    """A named list is the ceiling for the prompt and for skill_tool."""
    request = _request(agent_skills_available=["alpha"])

    assert await adapter._ensure_chat_extensions(request) is None

    assert await _roster(adapter) == ["alpha"]
    # The session snapshot has to be dropped, or skill_tool keeps resolving
    # 'beta' out of the stale baseline.
    adapter._instance._loop_session.update_state.assert_called_with(
        {"skill_use": None}
    )


async def test_empty_list_lifts_a_previous_restriction(
    adapter: JiuWenSwarmDeepAdapter,
) -> None:
    """An empty list is the rail's 'no allow-list' state, not an empty roster."""
    assert (
        await adapter._ensure_chat_extensions(
            _request(agent_skills_available=["alpha"])
        )
        is None
    )
    assert await _roster(adapter) == ["alpha"]

    assert (
        await adapter._ensure_chat_extensions(_request(agent_skills_available=[]))
        is None
    )
    assert await _roster(adapter) == ["alpha", "beta"]


async def test_uninstalled_name_does_not_fail_the_request(
    adapter: JiuWenSwarmDeepAdapter,
) -> None:
    """An unknown name has no effect; the installed names still apply."""
    request = _request(agent_skills_available=["alpha", "nosuchskill"])

    assert await adapter._ensure_chat_extensions(request) is None

    assert await _roster(adapter) == ["alpha"]


async def test_non_list_value_is_rejected(
    adapter: JiuWenSwarmDeepAdapter,
) -> None:
    """A malformed value must not be served as an unrestricted turn."""
    for bad in ("alpha", {"alpha": True}, ["alpha", 7]):
        response = await adapter._ensure_chat_extensions(
            _request(agent_skills_available=bad)
        )

        assert response is not None
        assert response.ok is False
        assert response.payload["event_type"] == "chat.error"
        assert "agent_skills_available" in response.payload["error"]
        assert await _roster(adapter) == ["alpha", "beta"]
