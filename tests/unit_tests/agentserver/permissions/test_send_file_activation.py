# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Activation contracts for exact send-file execution authorization."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
    SendFileToolkit,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as adapter_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _AbilityManager:
    def __init__(self, abilities: list[object] | None = None) -> None:
        self._abilities = list(abilities or [])

    def list(self) -> list[object]:
        return list(self._abilities)

    def add(self, ability: object) -> None:
        self._abilities.append(ability)

    def remove(self, name: str) -> None:
        self._abilities = [
            ability
            for ability in self._abilities
            if getattr(ability, "name", None) != name
        ]


def _adapter(*, registered_send_tool: bool = False) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    abilities = (
        [SimpleNamespace(name="send_file_to_user")] if registered_send_tool else []
    )
    adapter._instance = SimpleNamespace(ability_manager=_AbilityManager(abilities))
    adapter._build_cron_tools = MagicMock(return_value=[])
    adapter._resolve_prompt_channel = MagicMock(return_value="web")
    adapter._resolve_runtime_language = MagicMock(return_value="cn")
    adapter._cron_tools_registered_language = None
    adapter._project_dir = None
    adapter._tool_owner_id = MagicMock(return_value="test-owner")
    adapter._register_agent_owned_tool = MagicMock()
    adapter._enable_auto_permission = False
    adapter._last_mode = "agent.work.normal"
    adapter._session_messaging_toolkit = None
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("permission_mode", ["auto", "manual"])
async def test_new_ordinary_send_tool_does_not_require_auto_authorization(
    permission_mode: str,
) -> None:
    adapter = _adapter()
    toolkit = MagicMock()
    toolkit.get_tools.return_value = []
    with patch.object(
        adapter_module,
        "get_config",
        return_value={"permissions": {"mode": permission_mode}},
    ), patch.object(
        adapter_module,
        "SendFileToolkit",
        return_value=toolkit,
    ) as toolkit_type:
        await adapter._update_session_tools(
            session_id="session-a",
            request_id="request-a",
            channel_id="web",
        )

    toolkit_type.assert_called_once_with(
        request_id="request-a",
        session_id="session-a",
        channel_id="web",
        metadata=None,
        user_id=None,
        project_dir=None,
        require_execution_authorization=False,
    )


@pytest.mark.asyncio
async def test_registered_send_tool_stays_without_auto_authorization() -> None:
    adapter = _adapter(registered_send_tool=True)
    adapter._send_file_toolkit = MagicMock()
    configs = iter(
        [
            {"permissions": {"mode": "auto"}},
            {"permissions": {"mode": "manual"}},
        ]
    )

    with patch.object(adapter_module, "get_config", side_effect=configs):
        await adapter._update_session_tools("session-a", "request-a", "web")
        await adapter._update_session_tools("session-b", "request-b", "web")

    assert adapter._send_file_toolkit.update_runtime_context.call_args_list == [
        call(
            request_id="request-a",
            session_id="session-a",
            channel_id="web",
            metadata=None,
            user_id=None,
            project_dir=None,
            require_execution_authorization=False,
        ),
        call(
            request_id="request-b",
            session_id="session-b",
            channel_id="web",
            metadata=None,
            user_id=None,
            project_dir=None,
            require_execution_authorization=False,
        ),
    ]


@pytest.mark.asyncio
async def test_registered_send_tool_tracks_adapter_auto_activation() -> None:
    adapter = _adapter(registered_send_tool=True)
    adapter._send_file_toolkit = MagicMock()
    adapter._enable_auto_permission = True
    await adapter._update_session_tools("session-auto", "request-auto", "web")
    adapter._enable_auto_permission = False
    await adapter._update_session_tools("session-manual", "request-manual", "web")

    authorization_values = [
        item.kwargs["require_execution_authorization"]
        for item in adapter._send_file_toolkit.update_runtime_context.call_args_list
    ]
    assert authorization_values == [True, False]


@pytest.mark.asyncio
async def test_ordinary_auto_config_send_has_no_exact_grant_requirement(
    tmp_path,
) -> None:
    adapter = _adapter()
    source = tmp_path / "report.md"
    source.write_text("approved", encoding="utf-8")
    resource_manager = MagicMock()

    with patch.object(
        adapter_module,
        "get_config",
        return_value={"permissions": {"mode": "auto"}},
    ), patch.object(adapter_module.Runner, "resource_mgr", resource_manager):
        await adapter._update_session_tools(
            session_id="session-auto",
            request_id="request-auto",
            channel_id="web",
        )
        result = await adapter._send_file_toolkit.send_file(source.as_posix())

    assert adapter._send_file_toolkit._require_execution_authorization is False
    assert "send_file_execution_grant_missing" not in result


def test_runtime_context_authorization_update_is_explicit() -> None:
    toolkit = SendFileToolkit(
        request_id="request-a",
        session_id="session-a",
        channel_id="web",
        require_execution_authorization=True,
    )

    toolkit.update_runtime_context(
        request_id="request-b",
        session_id="session-b",
        channel_id="web",
    )
    assert toolkit._require_execution_authorization is True

    toolkit.update_runtime_context(
        request_id="request-c",
        session_id="session-c",
        channel_id="web",
        require_execution_authorization=False,
    )
    assert toolkit._require_execution_authorization is False
