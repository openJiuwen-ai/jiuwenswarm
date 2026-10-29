# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Sandbox endpoint addresses remain separate from the execution resource owner."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.management import sandbox
from jiuwenswarm.server.worker.execution_host import WorkerExecutionHost


@pytest.fixture
def owner(tmp_path, monkeypatch):
    policy = tmp_path / "policy.yaml"
    policy.write_text("landlock:\n  compatibility: strict\n", encoding="utf-8")
    monkeypatch.setattr(sandbox, "get_sandbox_endpoint", lambda: {
        "url": "http://127.0.0.1:8321", "startup_mode": "internal", "policy_file": str(policy),
    })
    monkeypatch.setattr(sandbox, "resolve_sandbox_policy_path", lambda _: policy)
    monkeypatch.setattr(sandbox, "resolve_sandbox_api_token", lambda **kwargs: "local-test-token")
    monkeypatch.setattr(sandbox, "sync_sandbox_api_token_environ", Mock())
    monkeypatch.setattr(sandbox, "resolve_preserve_file_sharing_mode_default", lambda: "shared")
    monkeypatch.setattr(sandbox, "update_sandbox_endpoint", Mock())
    monkeypatch.setattr(sandbox, "update_sandbox_runtime", lambda value: value)
    host = WorkerExecutionHost.__new__(WorkerExecutionHost)
    host._agent_manager = SimpleNamespace(recreate_agent=AsyncMock())
    host._jiuwenbox_runner = SimpleNamespace(
        is_owned_listener=Mock(return_value=False), ensure_running=AsyncMock(return_value=True),
        fetch_health=AsyncMock(return_value={"landlock_supported": True}),
        get_owned_endpoint=Mock(return_value=("127.0.0.1", 45678)), stop=AsyncMock(),
    )
    host._is_tcp_port_bindable = Mock(return_value=False)
    host._pick_free_tcp_port = Mock(return_value=45678)
    return host


async def test_enable_and_disable_keep_the_execution_owner_and_actual_address(owner):
    result = await owner._handle_sandbox_enable("web")
    call = owner._jiuwenbox_runner.ensure_running.call_args.kwargs
    assert call["host"] == "127.0.0.1" and call["port"] == 45678
    assert result["endpoint"]["url"] == "http://127.0.0.1:45678"
    owner._agent_manager.recreate_agent.assert_awaited_once_with("web", immediate=True)
    stopped = await owner._handle_sandbox_disable("web")
    owner._jiuwenbox_runner.stop.assert_awaited_once()
    assert stopped["jiuwenbox"] == {"host": "127.0.0.1", "port": 45678, "ready": False}


@pytest.mark.parametrize("explicit", [False, True])
async def test_landlock_health_uses_owner_runner_for_both_endpoint_forms(owner, explicit):
    payload = {"jiuwenbox": {"host": "127.0.0.1", "port": 8321}} if explicit else {}
    await owner._attach_landlock_status(payload)
    owner._jiuwenbox_runner.fetch_health.assert_awaited_once_with("127.0.0.1", 8321)
    assert payload["landlock"] == {"supported": True, "compatibility": "strict"}


@pytest.mark.parametrize("owned,bindable,expected", [(True, False, 8321), (False, True, 8321), (False, False, 45678)])
def test_port_allocation_passes_the_address_to_resource_checks(owner, owned, bindable, expected):
    owner._jiuwenbox_runner.is_owned_listener.return_value = owned
    owner._is_tcp_port_bindable.return_value = bindable
    assert owner._allocate_internal_jiuwenbox_port("127.0.0.1", 8321) == expected
    owner._jiuwenbox_runner.is_owned_listener.assert_called_once_with("127.0.0.1", 8321)
    if not owned:
        owner._is_tcp_port_bindable.assert_called_once_with("127.0.0.1", 8321)
    if not owned and not bindable:
        owner._pick_free_tcp_port.assert_called_once_with("127.0.0.1")
