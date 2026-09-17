from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.system]

_BOOTSTRAP_PATH = (
        Path(__file__).resolve().parents[2]
        / "jiuwenswarm"
        / "agents"
        / "harness"
        / "team"
        / "remote_member_bootstrap.py"
)
_BOOTSTRAP_SPEC = importlib.util.spec_from_file_location(
    "test_remote_member_bootstrap_module",
    _BOOTSTRAP_PATH,
)
assert _BOOTSTRAP_SPEC is not None and _BOOTSTRAP_SPEC.loader is not None
bootstrap_module = importlib.util.module_from_spec(_BOOTSTRAP_SPEC)
_BOOTSTRAP_SPEC.loader.exec_module(bootstrap_module)


@pytest.mark.asyncio
async def test_shutdown_cleanup_scheduler_deletes_team_session_and_pushes_notice(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(_delay: float) -> None:
        return None

    manager = SimpleNamespace(delete_session_runtime=AsyncMock(return_value=True))
    notices: list[dict] = []

    async def fake_push_shutdown_cleanup_notice(**kwargs) -> None:
        notices.append(kwargs)

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"team": {"runtime": {"mode": "distributed", "role": "leader"}}},
    )
    monkeypatch.setattr(bootstrap_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda channel_id: manager,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "".join(["_push", "_shutdown_cleanup_notice"]),
        fake_push_shutdown_cleanup_notice,
    )

    from openjiuwen.agent_teams.schema.status import MemberStatus
    from openjiuwen.agent_teams.schema.team import TeamRole
    from openjiuwen.core.runner import Runner

    class _Result:
        success = True

    class _ShutdownMemberTool:
        async def invoke(self, inputs, **kwargs):
            return _Result()

    tool = _ShutdownMemberTool()
    monkeypatch.setattr(
        Runner,
        "resource_mgr",
        SimpleNamespace(get_tool=lambda *_args, **_kwargs: tool),
    )
    team_agent = SimpleNamespace(
        role=TeamRole.LEADER,
        spec=SimpleNamespace(lifecycle="persistent"),
        deep_agent=SimpleNamespace(
            ability_manager=SimpleNamespace(
                list=lambda: [SimpleNamespace(id="team.shutdown_member", name="shutdown_member")]
            ),
            card=SimpleNamespace(id="leader-card"),
        ),
        team_backend=SimpleNamespace(
            list_members=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        member_name="teammate-1",
                        status=MemberStatus.SHUTDOWN.value,
                    )
                ]
            )
        ),
    )

    bootstrap_module.attach_shutdown_member_remote_cleanup_wrapper(
        team_agent,
        session_id="sess-shutdown",
        channel_id="web",
    )
    await tool.invoke({"member_name": "teammate-1"})
    cleaned = await bootstrap_module.wait_for_pending_shutdown_cleanup_for_session(
        "sess-shutdown",
        timeout=1.0,
    )

    assert cleaned is True
    manager.delete_session_runtime.assert_awaited_once_with(
        "sess-shutdown",
        reason="team.shutdown_all_members: ",
    )
    assert notices == [
        {
            "session_id": "sess-shutdown",
            "channel_id": "web",
            "deleted": True,
        }
    ]
