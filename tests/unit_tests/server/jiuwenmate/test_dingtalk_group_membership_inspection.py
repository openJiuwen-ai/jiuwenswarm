"""Bot membership inspection never treats a user event or CLI failure as access."""

from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.jiuwenmate.dingtalk_cli_local_provider import LocalDingTalkCliRuntime
from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliRuntimeService, DingTalkCliScope


@pytest.mark.asyncio
@pytest.mark.parametrize("bots,expected", [([{"robotCode": "robot"}], "active"), ([], "left"),
    ([{"robotCode": "other"}], "left"), ([{}], "unknown"), (None, "unknown"),
    ([{"robotCode": "robot "}], "unknown")])
async def test_membership_uses_only_the_bound_bot_and_employee_oauth(tmp_path, monkeypatch, bots, expected):
    config = {"channels": {"dingtalk": {"apps": [{"enabled": True, "client_id": "app", "client_secret": "fake",
                                               "persistent_agent_id": "agent", "robot_code": "robot"}]}}}
    runtime = LocalDingTalkCliRuntime(root=tmp_path, config_loader=lambda: config, dws_path=tmp_path / "dws")
    monkeypatch.setattr(runtime, "dingtalk_auth_status", AsyncMock(return_value={"status": "connected", "_profile": "employee"}))
    run = AsyncMock(return_value={"result": {"bots": bots}})
    monkeypatch.setattr(runtime, "_execute_oauth_command", run)
    service = DingTalkCliRuntimeService(runtime=runtime)
    result = await service.inspect_group_membership(DingTalkCliScope("local", "agent", app_id="app"), conversation_id="cid-one")
    assert result == {"status": expected}
    assert run.await_args.args[1] == ["chat", "group", "bots", "--group", "cid-one"]
    assert run.await_args.args[2] == "employee"


@pytest.mark.asyncio
async def test_missing_login_does_not_inspect_with_a_shared_host_identity(tmp_path, monkeypatch):
    runtime = LocalDingTalkCliRuntime(root=tmp_path, config_loader=lambda: {})
    monkeypatch.setattr(runtime, "dingtalk_auth_status", AsyncMock(return_value={"status": "not_connected"}))
    run = AsyncMock()
    monkeypatch.setattr(runtime, "_execute_oauth_command", run)
    result = await DingTalkCliRuntimeService(runtime=runtime).inspect_group_membership(
        DingTalkCliScope("local", "agent", app_id="app"), conversation_id="cid-one")
    assert result == {"status": "unknown"}
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_inspection_never_installs_cli_via_npx(tmp_path, monkeypatch):
    config = {"channels": {"dingtalk": {"apps": [{"enabled": True, "client_id": "app", "client_secret": "fake",
                                               "persistent_agent_id": "agent", "robot_code": "robot"}]}}}
    runtime = LocalDingTalkCliRuntime(root=tmp_path, config_loader=lambda: config)
    auth = AsyncMock(return_value={"status": "connected", "_profile": "employee"})
    monkeypatch.setattr(runtime, "dingtalk_auth_status", auth)
    run = AsyncMock(return_value={"result": {"bots": [{"robotCode": "robot"}]}})
    monkeypatch.setattr(runtime, "_execute_oauth_command", run)
    result = await runtime.inspect_group_membership(DingTalkCliScope("local", "agent", app_id="app"),
                                                    conversation_id="cid-one")
    assert result == {"status": "unknown"}
    auth.assert_not_awaited()
    run.assert_not_awaited()
