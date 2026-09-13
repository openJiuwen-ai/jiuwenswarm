# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Relay's legacy request-scoped ``office_claw_mcp`` field."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from openjiuwen.core.foundation.tool import ToolCard

from jiuwenswarm.common.mcp_config import (
    OfficeClawMcpRegistration,
    RequestScopedOfficeClawMcpTool,
    bind_active_office_claw_mcp_tools,
    bind_office_claw_from_agent,
    clear_agent_office_claw_tool_ids,
    ensure_request_scoped_office_claw_tool_allowed,
    extract_office_claw_mcp,
    set_agent_office_claw_tool_ids,
    validate_office_claw_mcp_config,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _request(
    config: object | None = None,
    *,
    request_id: str = "req-123",
    session_id: str = "session-456",
) -> AgentRequest:
    params = {"query": "一分钟后提醒我喝水"}
    if config is not None:
        params["office_claw_mcp"] = config
    return AgentRequest(
        request_id=request_id,
        channel_id="officeclaw",
        session_id=session_id,
        params=params,
    )


def _valid_config(*, invocation_id: str = "invoke-1") -> dict[str, object]:
    return {
        "command": r"E:\node\node.exe",
        "args": [r"E:\relay\packages\mcp-server\dist\index.js"],
        "cwd": r"E:\relay",
        "env": {
            "OFFICE_CLAW_API_URL": "http://127.0.0.1:3000",
            "OFFICE_CLAW_INVOCATION_ID": invocation_id,
            "OFFICE_CLAW_CALLBACK_TOKEN": "secret-token",
            "OFFICE_CLAW_MCP_EXCLUDED_TOOLS": "office_claw_list_tasks",
        },
    }


def _startup_env() -> dict[str, str]:
    return {
        "OFFICE_CLAW_MCP_COMMAND": r"E:\node\node.exe",
        "OFFICE_CLAW_MCP_ARGS_JSON": r'["E:\\relay\\packages\\mcp-server\\dist\\index.js"]',
        "OFFICE_CLAW_MCP_CWD": r"E:\relay",
    }


class _AbilityManager:
    def __init__(self) -> None:
        self.cards: dict[str, object] = {}
        self.removed: list[str] = []

    def get(self, name: str) -> object | None:
        return self.cards.get(name)

    def add(self, card: object) -> SimpleNamespace:
        name = str(getattr(card, "name"))
        existing = self.cards.get(name)
        if existing is not None and getattr(existing, "id", None) != getattr(card, "id", None):
            return SimpleNamespace(added=False, reason="duplicate_tool")
        self.cards[name] = card
        return SimpleNamespace(added=True, reason="added_tool")

    def remove(self, name: str) -> None:
        self.removed.append(name)
        self.cards.pop(name, None)


class _ResourceManager:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.removed: list[str] = []

    def add_tool(self, tool: object, *, tag: str) -> None:
        assert tag == "office-claw"
        card = getattr(tool, "card")
        self.tools[str(card.id)] = tool

    def remove_tool(self, tool_id: str) -> None:
        self.removed.append(tool_id)
        self.tools.pop(tool_id, None)


def _bare_session_adapter() -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(ability_manager=_AbilityManager())
    adapter._active_office_claw_mcp = None
    adapter._active_office_claw_tool_instances = ()
    return adapter


def _tool_list() -> list[dict[str, object]]:
    return [
        {
            "name": "office_claw_preview_scheduled_task",
            "description": "preview",
            "input_params": {"type": "object"},
        },
        {
            "name": "office_claw_multi_mention",
            "description": "multi",
            "input_params": {"type": "object"},
        },
    ]


def test_extract_supports_only_office_claw_mcp() -> None:
    config = _valid_config()

    extracted = extract_office_claw_mcp(
        {
            "office_claw_mcp": config,
            "request_mcp_servers": {"mcpServers": {"other": {"command": "bad"}}},
        }
    )

    assert extracted == config
    assert extracted is not config
    assert extract_office_claw_mcp({"request_mcp_servers": {"mcpServers": {}}}) is None
    assert extract_office_claw_mcp({"office_claw_mcp": "invalid"}) is None


def test_validation_pins_process_identity_to_relay_startup() -> None:
    config = _valid_config()

    validated = validate_office_claw_mcp_config(config, environ=_startup_env())

    assert validated == config
    changed = _valid_config()
    changed["args"] = [r"E:\attacker\script.js"]
    with pytest.raises(ValueError, match="args do not match"):
        validate_office_claw_mcp_config(changed, environ=_startup_env())


def test_request_scoped_tool_allowlist_rejects_cross_request_id() -> None:
    with bind_active_office_claw_mcp_tools(["office-claw-request-aaa.office-claw.multi"]):
        ensure_request_scoped_office_claw_tool_allowed(
            "office-claw-request-aaa.office-claw.multi"
        )
        with pytest.raises(RuntimeError, match="bound to another request"):
            ensure_request_scoped_office_claw_tool_allowed(
                "office-claw-request-bbb.office-claw.multi"
            )


def test_request_scoped_tool_allowlist_rejects_unbound_invoke() -> None:
    with pytest.raises(RuntimeError, match="without an active request binding"):
        ensure_request_scoped_office_claw_tool_allowed(
            "office-claw-request-aaa.office-claw.multi"
        )


@pytest.mark.asyncio
async def test_request_scoped_tool_invoke_rebinds_from_live_allowlist() -> None:
    from jiuwenswarm.common.mcp_config import (
        _clear_live_office_claw_allowlists_for_tests,
        get_active_office_claw_mcp_tool_ids,
        publish_live_office_claw_allowlist,
        register_live_office_claw_tool_instance,
        revoke_live_office_claw_allowlist,
        unregister_live_office_claw_tool_instance,
    )

    _clear_live_office_claw_allowlists_for_tests()
    tool_id = "office-claw-request-aaa.office-claw.office_claw_multi_mention"
    card = ToolCard(
        id=tool_id,
        name="office_claw_multi_mention",
        description="multi",
        input_params={},
    )
    tool = RequestScopedOfficeClawMcpTool(
        card,
        {
            "command": "node",
            "args": ["index.js"],
            "cwd": ".",
            "env": {"OFFICE_CLAW_INVOCATION_ID": "aaa"},
        },
    )
    publish_live_office_claw_allowlist([tool_id])
    register_live_office_claw_tool_instance(tool, [tool_id])
    seen: dict[str, object] = {}

    async def _capture(inputs, **kwargs):
        seen["allowed"] = get_active_office_claw_mcp_tool_ids()
        return {"result": "ok"}

    try:
        with patch.object(
            RequestScopedOfficeClawMcpTool,
            "_invoke_with_active_binding",
            new=AsyncMock(side_effect=_capture),
        ):
            result = await tool.invoke({})
    finally:
        unregister_live_office_claw_tool_instance(tool)
        revoke_live_office_claw_allowlist([tool_id])
        _clear_live_office_claw_allowlists_for_tests()

    assert result == {"result": "ok"}
    assert seen["allowed"] == frozenset({tool_id})
    assert get_active_office_claw_mcp_tool_ids() is None


@pytest.mark.asyncio
async def test_request_scoped_tool_invoke_rebinds_from_expected_allowlist_kwarg() -> None:
    from jiuwenswarm.common.mcp_config import (
        OFFICE_CLAW_EXPECTED_TOOL_IDS_KWARG,
        get_active_office_claw_mcp_tool_ids,
    )

    tool_id = "office-claw-request-mine.office-claw.office_claw_multi_mention"
    foreign_id = "office-claw-request-other.office-claw.office_claw_multi_mention"
    card = ToolCard(
        id=tool_id,
        name="office_claw_multi_mention",
        description="multi",
        input_params={},
    )
    tool = RequestScopedOfficeClawMcpTool(
        card,
        {"command": "node", "args": ["index.js"], "cwd": ".", "env": {}},
    )
    seen: dict[str, object] = {}

    async def _capture(inputs, **kwargs):
        seen["allowed"] = get_active_office_claw_mcp_tool_ids()
        return {"result": "ok"}

    with patch.object(
        RequestScopedOfficeClawMcpTool,
        "_invoke_with_active_binding",
        new=AsyncMock(side_effect=_capture),
    ):
        result = await tool.invoke(
            {},
            **{OFFICE_CLAW_EXPECTED_TOOL_IDS_KWARG: [tool_id]},
        )

    assert result == {"result": "ok"}
    assert seen["allowed"] == frozenset({tool_id})
    assert get_active_office_claw_mcp_tool_ids() is None

    with pytest.raises(Exception, match="without an active request binding"):
        await tool.invoke(
            {},
            **{OFFICE_CLAW_EXPECTED_TOOL_IDS_KWARG: [foreign_id]},
        )


@pytest.mark.asyncio
async def test_request_scoped_tool_invoke_refuses_foreign_live_rebind_when_concurrent() -> None:
    from jiuwenswarm.common.mcp_config import (
        _clear_live_office_claw_allowlists_for_tests,
        publish_live_office_claw_allowlist,
        revoke_live_office_claw_allowlist,
    )

    _clear_live_office_claw_allowlists_for_tests()
    owned_id = "office-claw-request-mine.office-claw.office_claw_multi_mention"
    foreign_id = "office-claw-request-other.office-claw.office_claw_multi_mention"
    foreign_card = ToolCard(
        id=foreign_id,
        name="office_claw_multi_mention",
        description="foreign",
        input_params={},
    )
    foreign_tool = RequestScopedOfficeClawMcpTool(
        foreign_card,
        {"command": "node", "args": ["foreign.js"], "cwd": ".", "env": {}},
    )
    publish_live_office_claw_allowlist([owned_id])
    publish_live_office_claw_allowlist([foreign_id])

    try:
        with pytest.raises(Exception, match="without an active request binding"):
            await foreign_tool.invoke({})
    finally:
        revoke_live_office_claw_allowlist([owned_id])
        revoke_live_office_claw_allowlist([foreign_id])
        _clear_live_office_claw_allowlists_for_tests()


@pytest.mark.asyncio
async def test_request_scoped_tool_invoke_refuses_cross_request_binding() -> None:
    card = ToolCard(
        id="office-claw-request-bbb.office-claw.office_claw_multi_mention",
        name="office_claw_multi_mention",
        description="multi",
        input_params={},
    )
    tool = RequestScopedOfficeClawMcpTool(
        card,
        {
            "command": "node",
            "args": ["index.js"],
            "cwd": ".",
            "env": {"OFFICE_CLAW_INVOCATION_ID": "other"},
        },
    )

    with bind_active_office_claw_mcp_tools(
        ["office-claw-request-aaa.office-claw.office_claw_multi_mention"]
    ):
        with pytest.raises(Exception, match="bound to another request"):
            await tool.invoke({})


@pytest.mark.asyncio
async def test_registers_exact_tool_names_and_cleans_them_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _startup_env().items():
        monkeypatch.setenv(key, value)
    resource_manager = _ResourceManager()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", resource_manager)
    monkeypatch.setattr(
        interface_deep,
        "list_office_claw_mcp_tools",
        AsyncMock(return_value=_tool_list()),
    )
    adapter = _bare_session_adapter()

    registration = await adapter.register_request_scoped_office_claw_mcp(
        _request(_valid_config())
    )

    assert registration is not None
    assert registration.tool_names == (
        "office_claw_preview_scheduled_task",
        "office_claw_multi_mention",
    )
    assert registration.invocation_id == "invoke-1"
    assert len(registration.tool_instances) == 2
    assert set(adapter._instance.ability_manager.cards) == set(registration.tool_names)
    assert len(resource_manager.tools) == 2
    assert adapter._active_office_claw_mcp == registration
    from jiuwenswarm.common.mcp_config import (
        get_live_office_claw_allowlist_for_tool_id,
        get_live_office_claw_allowlist_for_tool_instance,
    )

    assert get_live_office_claw_allowlist_for_tool_id(registration.tool_ids[0]) == frozenset(
        registration.tool_ids
    )
    assert get_live_office_claw_allowlist_for_tool_instance(
        registration.tool_instances[0]
    ) == frozenset(registration.tool_ids)

    await adapter.cleanup_request_scoped_office_claw_mcp(registration)

    assert resource_manager.tools == {}
    assert adapter._instance.ability_manager.cards == {}
    assert resource_manager.removed == list(registration.tool_ids)
    assert adapter._active_office_claw_mcp is None
    assert get_live_office_claw_allowlist_for_tool_id(registration.tool_ids[0]) is None


@pytest.mark.asyncio
async def test_cleanup_does_not_remove_rebound_short_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _startup_env().items():
        monkeypatch.setenv(key, value)
    resource_manager = _ResourceManager()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", resource_manager)
    monkeypatch.setattr(
        interface_deep,
        "list_office_claw_mcp_tools",
        AsyncMock(return_value=_tool_list()),
    )
    shared_ability = _AbilityManager()
    adapter_a = _bare_session_adapter()
    adapter_b = _bare_session_adapter()
    adapter_a._instance = SimpleNamespace(ability_manager=shared_ability)
    adapter_b._instance = SimpleNamespace(ability_manager=shared_ability)

    registration_a = await adapter_a.register_request_scoped_office_claw_mcp(
        _request(_valid_config(invocation_id="inv-a"), request_id="req-a", session_id="sess-a")
    )
    assert registration_a is not None

    # Simulate a polluted shared AbilityManager: B overwrites short names after
    # owning the previous mapping (sequential replace on same AM).
    adapter_b._active_office_claw_mcp = registration_a
    registration_b = await adapter_b.register_request_scoped_office_claw_mcp(
        _request(_valid_config(invocation_id="inv-b"), request_id="req-b", session_id="sess-b")
    )
    assert registration_b is not None
    multi_card = shared_ability.get("office_claw_multi_mention")
    assert multi_card is not None
    assert str(multi_card.id) in registration_b.tool_ids

    await adapter_a.cleanup_request_scoped_office_claw_mcp(registration_a)

    # A's cleanup must not delete B's rebound short-name mapping.
    assert "office_claw_multi_mention" in shared_ability.cards
    assert str(shared_ability.get("office_claw_multi_mention").id) in registration_b.tool_ids


@pytest.mark.asyncio
async def test_foreign_short_name_conflict_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _startup_env().items():
        monkeypatch.setenv(key, value)
    resource_manager = _ResourceManager()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", resource_manager)
    monkeypatch.setattr(
        interface_deep,
        "list_office_claw_mcp_tools",
        AsyncMock(return_value=_tool_list()),
    )
    adapter = _bare_session_adapter()
    foreign = ToolCard(
        id="office-claw-request-foreign.office-claw.office_claw_multi_mention",
        name="office_claw_multi_mention",
        description="foreign",
        input_params={},
    )
    adapter._instance.ability_manager.cards["office_claw_multi_mention"] = foreign

    registration = await adapter.register_request_scoped_office_claw_mcp(
        _request(_valid_config())
    )

    assert registration is None
    assert adapter._instance.ability_manager.get("office_claw_multi_mention") is foreign


@pytest.mark.asyncio
async def test_legacy_ability_manager_post_add_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy AbilityManager.add() returns None. If the short-name mapping
    does not land on our card id afterwards, registration must fail closed
    instead of treating ``None`` as success."""

    class _StickyForeignAbilityManager(_AbilityManager):
        def add(self, card: object) -> None:
            # Pretend to accept the card (legacy return) but keep the foreign bind.
            return None

    for key, value in _startup_env().items():
        monkeypatch.setenv(key, value)
    resource_manager = _ResourceManager()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", resource_manager)
    monkeypatch.setattr(
        interface_deep,
        "list_office_claw_mcp_tools",
        AsyncMock(
            return_value=[
                {
                    "name": "office_claw_preview_scheduled_task",
                    "description": "preview",
                    "input_params": {"type": "object"},
                }
            ]
        ),
    )
    adapter = _bare_session_adapter()
    sticky = _StickyForeignAbilityManager()
    sticky.cards["office_claw_preview_scheduled_task"] = ToolCard(
        id="office-claw-request-foreign.office-claw.office_claw_preview_scheduled_task",
        name="office_claw_preview_scheduled_task",
        description="foreign",
        input_params={},
    )
    # Pre-check sees foreign id and fails before add — exercise post-add path
    # by clearing the foreign card only for the pre-check window is hard;
    # instead start empty and make add() a no-op so post-verify sees missing id.
    sticky.cards.clear()
    adapter._instance = SimpleNamespace(ability_manager=sticky)

    registration = await adapter.register_request_scoped_office_claw_mcp(
        _request(_valid_config())
    )

    assert registration is None
    assert sticky.cards == {}


@pytest.mark.asyncio
async def test_registration_failure_is_non_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _startup_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        interface_deep,
        "list_office_claw_mcp_tools",
        AsyncMock(side_effect=RuntimeError("MCP unavailable")),
    )
    adapter = _bare_session_adapter()

    registration = await adapter.register_request_scoped_office_claw_mcp(
        _request(_valid_config())
    )

    assert registration is None
    assert adapter._instance.ability_manager.cards == {}


@pytest.mark.asyncio
async def test_unary_request_always_cleans_up_request_mcp() -> None:
    parent = object.__new__(JiuWenSwarmDeepAdapter)
    parent._is_session_scoped_adapter = False
    registration = OfficeClawMcpRegistration("req-123", ("tool-id",), ("tool",))
    response = AgentResponse(request_id="req-123", channel_id="officeclaw")
    child = SimpleNamespace(
        register_request_scoped_office_claw_mcp=AsyncMock(return_value=registration),
        process_message_impl=AsyncMock(return_value=response),
        cleanup_request_scoped_office_claw_mcp=AsyncMock(),
    )
    parent._get_or_create_session_adapter = AsyncMock(return_value=child)
    parent._evict_idle_session_adapters = AsyncMock()

    result = await parent.process_message_impl(_request(_valid_config()), {})

    assert result is response
    child.cleanup_request_scoped_office_claw_mcp.assert_awaited_once_with(registration)


@pytest.mark.asyncio
async def test_stream_request_cleans_up_when_consumer_finishes() -> None:
    parent = object.__new__(JiuWenSwarmDeepAdapter)
    parent._is_session_scoped_adapter = False
    registration = OfficeClawMcpRegistration("req-123", ("tool-id",), ("tool",))
    chunk = AgentResponseChunk(request_id="req-123", channel_id="officeclaw")

    class _Child:
        def __init__(self) -> None:
            self.register_request_scoped_office_claw_mcp = AsyncMock(
                return_value=registration
            )
            self.cleanup_request_scoped_office_claw_mcp = AsyncMock()

        async def process_message_stream_impl(self, request, inputs):
            yield chunk

    child = _Child()
    parent._get_or_create_session_adapter = AsyncMock(return_value=child)
    parent._evict_idle_session_adapters = AsyncMock()

    chunks = [
        item
        async for item in parent.process_message_stream_impl(
            _request(_valid_config()),
            {},
        )
    ]

    assert chunks == [chunk]
    child.cleanup_request_scoped_office_claw_mcp.assert_awaited_once_with(registration)


@pytest.mark.asyncio
async def test_stream_request_binds_active_tool_allowlist() -> None:
    parent = object.__new__(JiuWenSwarmDeepAdapter)
    parent._is_session_scoped_adapter = False
    registration = OfficeClawMcpRegistration(
        "req-123",
        ("office-claw-request-aaa.office-claw.office_claw_multi_mention",),
        ("office_claw_multi_mention",),
    )
    seen: dict[str, bool] = {}

    class _Child:
        def __init__(self) -> None:
            self.register_request_scoped_office_claw_mcp = AsyncMock(
                return_value=registration
            )
            self.cleanup_request_scoped_office_claw_mcp = AsyncMock()

        async def process_message_stream_impl(self, request, inputs):
            ensure_request_scoped_office_claw_tool_allowed(registration.tool_ids[0])
            seen["allowed"] = True
            with pytest.raises(RuntimeError, match="bound to another request"):
                ensure_request_scoped_office_claw_tool_allowed(
                    "office-claw-request-bbb.office-claw.office_claw_multi_mention"
                )
            seen["rejected_foreign"] = True
            yield AgentResponseChunk(request_id="req-123", channel_id="officeclaw")

    child = _Child()
    parent._get_or_create_session_adapter = AsyncMock(return_value=child)
    parent._evict_idle_session_adapters = AsyncMock()

    _ = [
        item
        async for item in parent.process_message_stream_impl(
            _request(_valid_config()),
            {},
        )
    ]

    assert seen == {"allowed": True, "rejected_foreign": True}


def test_carrier_stores_on_ability_manager_and_rebinds() -> None:
    """The allowlist lives on the shared ability_manager, visible to either agent."""
    tool_id = "office-claw-request-aaa.office-claw.office_claw_multi_mention"
    ability = SimpleNamespace()
    deep_agent = SimpleNamespace(ability_manager=ability)
    # DeepAgent and its inner ReActAgent share the same ability_manager.
    react_agent = SimpleNamespace(ability_manager=ability)

    set_agent_office_claw_tool_ids(deep_agent, [tool_id])

    # Stored on the shared carrier, not on the agent object itself.
    assert not hasattr(deep_agent, "_active_office_claw_tool_ids")
    assert hasattr(ability, "_active_office_claw_tool_ids")

    # The rail may resolve to either agent object; both re-bind the allowlist.
    for agent in (deep_agent, react_agent):
        with bind_office_claw_from_agent(agent):
            ensure_request_scoped_office_claw_tool_allowed(tool_id)
            with pytest.raises(RuntimeError, match="bound to another request"):
                ensure_request_scoped_office_claw_tool_allowed(
                    "office-claw-request-bbb.office-claw.office_claw_multi_mention"
                )


def _request_with_connectors(
        config: object | None = None,
        *,
        connectors: dict[str, dict[str, object]] | None = None,
        request_id: str = "req-123",
        session_id: str = "session-456",
) -> AgentRequest:
    params: dict[str, object] = {"query": "读文件"}
    if config is not None:
        params["office_claw_mcp"] = config
    if connectors is not None:
        params["request_mcp_servers"] = {"mcpServers": connectors}
    return AgentRequest(
        request_id=request_id,
        channel_id="officeclaw",
        session_id=session_id,
        params=params,
    )


@pytest.mark.asyncio
async def test_connector_builtin_name_conflict_downgrades_per_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connector tool whose short name collides with a *built-in* agent tool
    (e.g. ``filesystem.read_file`` vs the agent's ``read_file``) must be skipped
    in place — the rest of the connector's tools and the overall registration
    must survive. Previously this raised ``RuntimeError`` which propagated to the
    outer ``except`` and rolled back the entire registration, leaving
    ``tools_search`` with 0 matches and the LLM falling back to web_search."""

    for key, value in _startup_env().items():
        monkeypatch.setenv(key, value)
    resource_manager = _ResourceManager()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", resource_manager)
    monkeypatch.setattr(
        interface_deep,
        "list_office_claw_mcp_tools",
        AsyncMock(return_value=[]),
    )

    async def _fake_list_request_mcp_server_tools(server_name, config):
        # filesystem connector exposes read_file (shadows built-in) + a unique
        # list_dir tool that should register fine.
        return (
            [
                {
                    "name": "read_file",
                    "description": "fs read_file",
                    "input_params": {"type": "object"},
                },
                {
                    "name": "list_dir",
                    "description": "fs list_dir",
                    "input_params": {"type": "object"},
                },
            ],
            {"command": "node", "args": ["fs.js"], "cwd": "."},
        )

    monkeypatch.setattr(
        interface_deep,
        "list_request_mcp_server_tools",
        AsyncMock(side_effect=_fake_list_request_mcp_server_tools),
    )

    adapter = _bare_session_adapter()
    # Pre-seed the ability manager with a *built-in* read_file — its id does NOT
    # carry the request-scoped ``office-claw-request-`` prefix.
    builtin_read_file = ToolCard(
        id="read_file_jiuwenswarm_s_officeclaw_sid",
        name="read_file",
        description="built-in read_file",
        input_params={},
    )
    adapter._instance.ability_manager.cards["read_file"] = builtin_read_file

    registration = await adapter.register_request_scoped_office_claw_mcp(
        _request_with_connectors(
            connectors={"filesystem": {"command": "node", "args": ["fs.js"]}}
        )
    )

    # Registration succeeds (not rolled back to None).
    assert registration is not None
    # Only list_dir made it into the registration — read_file was skipped.
    assert registration.tool_names == ("list_dir",)
    # The built-in read_file mapping is untouched.
    assert (
            adapter._instance.ability_manager.get("read_file") is builtin_read_file
    )
    # No orphaned resource entry for the skipped read_file tool.
    skipped_id = next(
        (tid for tid in registration.tool_ids if tid.endswith(".read_file")),
        None,
    )
    assert skipped_id is None


@pytest.mark.asyncio
async def test_connector_foreign_short_name_conflict_still_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connector tool whose short name collides with a *foreign* request-scoped
    tool (different request scope, id carries the ``office-claw-request-``
    prefix) must still fail closed — the downgrade is only for collisions with
    genuine built-in tools, not for cross-request races."""

    for key, value in _startup_env().items():
        monkeypatch.setenv(key, value)
    resource_manager = _ResourceManager()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", resource_manager)
    monkeypatch.setattr(
        interface_deep,
        "list_office_claw_mcp_tools",
        AsyncMock(return_value=[]),
    )

    async def _fake_list_request_mcp_server_tools(server_name, config):
        return (
            [
                {
                    "name": "office_claw_multi_mention",
                    "description": "foreign-shadow",
                    "input_params": {"type": "object"},
                }
            ],
            {"command": "node", "args": ["x.js"], "cwd": "."},
        )

    monkeypatch.setattr(
        interface_deep,
        "list_request_mcp_server_tools",
        AsyncMock(side_effect=_fake_list_request_mcp_server_tools),
    )

    adapter = _bare_session_adapter()
    foreign = ToolCard(
        id="office-claw-request-foreign.office-claw.office_claw_multi_mention",
        name="office_claw_multi_mention",
        description="foreign",
        input_params={},
    )
    adapter._instance.ability_manager.cards["office_claw_multi_mention"] = foreign

    registration = await adapter.register_request_scoped_office_claw_mcp(
        _request_with_connectors(
            connectors={"amap": {"command": "node", "args": ["amap.js"]}}
        )
    )

    # Fail-closed: registration rolled back, foreign card preserved.
    assert registration is None
    assert (adapter._instance.ability_manager.get("office_claw_multi_mention") is foreign)


def test_clear_agent_office_claw_tool_ids_unbinds() -> None:
    """Clearing the allowlist makes subsequent invokes fail closed as unbound."""
    tool_id = "office-claw-request-aaa.office-claw.office_claw_multi_mention"
    ability = SimpleNamespace()
    agent = SimpleNamespace(ability_manager=ability)

    set_agent_office_claw_tool_ids(agent, [tool_id])
    with bind_office_claw_from_agent(agent):
        ensure_request_scoped_office_claw_tool_allowed(tool_id)

    clear_agent_office_claw_tool_ids(agent)
    assert not hasattr(ability, "_active_office_claw_tool_ids")

    # After clear, bind is a no-op and the tool is refused as unbound.
    with bind_office_claw_from_agent(agent):
        with pytest.raises(RuntimeError, match="without an active request binding"):
            ensure_request_scoped_office_claw_tool_allowed(tool_id)


@pytest.mark.asyncio
async def test_cleanup_superseded_request_preserves_active_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old request cleanup must not wipe the newer request's AM allowlist."""
    from jiuwenswarm.common.mcp_config import (
        _OFFICE_CLAW_TOOL_IDS_ATTR,
        _clear_live_office_claw_allowlists_for_tests,
    )

    for key, value in _startup_env().items():
        monkeypatch.setenv(key, value)
    resource_manager = _ResourceManager()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", resource_manager)
    monkeypatch.setattr(
        interface_deep,
        "list_office_claw_mcp_tools",
        AsyncMock(return_value=_tool_list()),
    )
    _clear_live_office_claw_allowlists_for_tests()
    adapter = _bare_session_adapter()

    old_reg = await adapter.register_request_scoped_office_claw_mcp(
        _request(
            _valid_config(invocation_id="inv-old"),
            request_id="req-old",
            session_id="sess-1",
        )
    )
    assert old_reg is not None

    new_reg = await adapter.register_request_scoped_office_claw_mcp(
        _request(
            _valid_config(invocation_id="inv-new"),
            request_id="req-new",
            session_id="sess-1",
        )
    )
    assert new_reg is not None
    assert adapter._active_office_claw_mcp == new_reg
    ability = adapter._instance.ability_manager
    assert getattr(ability, _OFFICE_CLAW_TOOL_IDS_ATTR) == frozenset(new_reg.tool_ids)

    await adapter.cleanup_request_scoped_office_claw_mcp(old_reg)

    assert adapter._active_office_claw_mcp == new_reg
    assert getattr(ability, _OFFICE_CLAW_TOOL_IDS_ATTR) == frozenset(new_reg.tool_ids)
    with bind_office_claw_from_agent(adapter._instance):
        ensure_request_scoped_office_claw_tool_allowed(new_reg.tool_ids[0])

    await adapter.cleanup_request_scoped_office_claw_mcp(new_reg)
    _clear_live_office_claw_allowlists_for_tests()


@pytest.mark.asyncio
async def test_invoke_refuses_env_invocation_mismatch_against_active_allowlist() -> None:
    from jiuwenswarm.common.mcp_config import (
        _clear_live_office_claw_allowlists_for_tests,
        publish_live_office_claw_allowlist,
        register_live_office_claw_tool_instance,
        revoke_live_office_claw_allowlist,
        unregister_live_office_claw_tool_instance,
    )

    _clear_live_office_claw_allowlists_for_tests()
    tool_id = "office-claw-request-aaa.office-claw.office_claw_register_scheduled_task"
    card = ToolCard(
        id=tool_id,
        name="office_claw_register_scheduled_task",
        description="register",
        input_params={},
    )
    live_tool = RequestScopedOfficeClawMcpTool(
        card,
        {
            "command": "node",
            "args": ["index.js"],
            "cwd": ".",
            "env": {"OFFICE_CLAW_INVOCATION_ID": "inv-a"},
        },
        request_id="req-a",
        server_name="office-claw",
    )
    wrong_env_tool = RequestScopedOfficeClawMcpTool(
        card,
        {
            "command": "node",
            "args": ["index.js"],
            "cwd": ".",
            "env": {"OFFICE_CLAW_INVOCATION_ID": "inv-c"},
        },
        request_id="req-c",
        server_name="office-claw",
    )
    publish_live_office_claw_allowlist([tool_id])
    register_live_office_claw_tool_instance(live_tool, [tool_id])

    try:
        with bind_active_office_claw_mcp_tools([tool_id]):
            with pytest.raises(Exception, match="env invocation mismatches"):
                await wrong_env_tool.invoke({})
    finally:
        unregister_live_office_claw_tool_instance(live_tool)
        revoke_live_office_claw_allowlist([tool_id])
        _clear_live_office_claw_allowlists_for_tests()


@pytest.mark.asyncio
async def test_concurrent_unbound_preferred_id_is_skipped_by_rail() -> None:
    """When allowlist is unbound and the short name is live-concurrent, refuse preferred_id."""
    from jiuwenswarm.common.mcp_config import (
        _clear_live_office_claw_allowlists_for_tests,
        publish_live_office_claw_allowlist,
        revoke_live_office_claw_allowlist,
    )
    from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
        ProgressiveToolRail,
    )

    _clear_live_office_claw_allowlists_for_tests()
    owned_id = "office-claw-request-aaa.office-claw.office_claw_register_scheduled_task"
    foreign_id = "office-claw-request-ccc.office-claw.office_claw_register_scheduled_task"
    publish_live_office_claw_allowlist([owned_id])
    publish_live_office_claw_allowlist([foreign_id])

    rail = object.__new__(ProgressiveToolRail)
    rail._office_claw_active_tool_ids = None
    rail._office_claw_invocation_id = None
    rail._office_claw_delivery_thread_id = None
    rail._cached_deferred_tool_infos = []
    foreign_tool = SimpleNamespace(card=SimpleNamespace(id=foreign_id))
    rail._lookup_tool_instance = lambda tool_id: (  # type: ignore[method-assign]
        foreign_tool if tool_id == foreign_id else None
    )
    rail._refresh_deferred_tool_cache = AsyncMock()

    try:
        # ContextVar unbound + concurrent live → must not return foreign preferred.
        tool, resolved_id, _ = await rail._resolve_tool_instance_with_recovery(
            "office_claw_register_scheduled_task",
            foreign_id,
        )
        assert tool is None
        assert resolved_id == foreign_id
    finally:
        revoke_live_office_claw_allowlist([owned_id])
        revoke_live_office_claw_allowlist([foreign_id])
        _clear_live_office_claw_allowlists_for_tests()


@pytest.mark.asyncio
async def test_rail_owned_allowlist_beats_foreign_preferred_id() -> None:
    """Rail pin must resolve the owned instance even if AM preferred is foreign."""
    from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
        ProgressiveToolRail,
    )

    owned_id = "office-claw-request-aaa.office-claw.office_claw_register_scheduled_task"
    foreign_id = "office-claw-request-ccc.office-claw.office_claw_register_scheduled_task"
    owned_tool = SimpleNamespace(_params={"env": {"OFFICE_CLAW_INVOCATION_ID": "inv-mine"}})
    foreign_tool = SimpleNamespace(_params={"env": {"OFFICE_CLAW_INVOCATION_ID": "inv-other"}})

    rail = object.__new__(ProgressiveToolRail)
    rail.set_office_claw_active_tool_ids(
        [owned_id],
        delivery_thread_id="thread_mine",
        invocation_id="inv-mine",
    )
    rail._cached_deferred_tool_infos = []
    rail._lookup_tool_instance = lambda tool_id: (  # type: ignore[method-assign]
        owned_tool if tool_id == owned_id else foreign_tool if tool_id == foreign_id else None
    )
    rail._refresh_deferred_tool_cache = AsyncMock()

    tool, resolved_id, _ = await rail._resolve_tool_instance_with_recovery(
        "office_claw_register_scheduled_task",
        foreign_id,
    )
    assert tool is owned_tool
    assert resolved_id == owned_id


@pytest.mark.asyncio
async def test_unpinned_single_live_office_claw_preferred_is_refused() -> None:
    """Without rail pin, a sole remaining request-scoped card is still unsafe."""
    from jiuwenswarm.common.mcp_config import (
        _clear_live_office_claw_allowlists_for_tests,
        publish_live_office_claw_allowlist,
        revoke_live_office_claw_allowlist,
    )
    from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
        ProgressiveToolRail,
    )

    _clear_live_office_claw_allowlists_for_tests()
    foreign_id = "office-claw-request-ccc.office-claw.office_claw_register_scheduled_task"
    publish_live_office_claw_allowlist([foreign_id])

    rail = object.__new__(ProgressiveToolRail)
    rail._office_claw_active_tool_ids = None
    rail._office_claw_invocation_id = None
    rail._office_claw_delivery_thread_id = None
    rail._cached_deferred_tool_infos = []
    foreign_tool = SimpleNamespace(_params={"env": {"OFFICE_CLAW_INVOCATION_ID": "inv-other"}})
    rail._lookup_tool_instance = lambda tool_id: (  # type: ignore[method-assign]
        foreign_tool if tool_id == foreign_id else None
    )
    rail._refresh_deferred_tool_cache = AsyncMock()

    try:
        tool, resolved_id, _ = await rail._resolve_tool_instance_with_recovery(
            "office_claw_register_scheduled_task",
            foreign_id,
        )
        assert tool is None
        assert resolved_id == foreign_id
    finally:
        revoke_live_office_claw_allowlist([foreign_id])
        _clear_live_office_claw_allowlists_for_tests()


@pytest.mark.asyncio
async def test_invoke_refuses_office_claw_without_request_binding() -> None:
    """Unpinned request-scoped invoke must fail closed (no silent cross-session)."""
    from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
        ProgressiveToolRail,
    )
    from jiuwenswarm.agents.harness.common.tools.invoke_tool_tool import (
        InvokeToolInput,
    )

    tool_id = "office-claw-request-ccc.office-claw.office_claw_register_scheduled_task"
    live_tool = SimpleNamespace(
        _params={"env": {"OFFICE_CLAW_INVOCATION_ID": "inv-other"}},
        invoke=AsyncMock(return_value={"ok": True}),
    )
    card = SimpleNamespace(id=tool_id, name="office_claw_register_scheduled_task")

    rail = object.__new__(ProgressiveToolRail)
    rail._office_claw_active_tool_ids = None
    rail._office_claw_invocation_id = None
    rail._office_claw_delivery_thread_id = None
    rail.eager_tools = ["tools_search", "invoke_tool"]
    rail._cached_deferred_tool_infos = [card]
    rail._deepresearch_context_provider = None
    rail._lookup_tool_instance = lambda tid: live_tool if tid == tool_id else None  # type: ignore[method-assign]
    rail._refresh_deferred_tool_cache = AsyncMock()
    rail._resolve_runtime_agent = lambda: None  # type: ignore[method-assign]

    result = await rail._invoke_target_tool(
        session=None,
        params=InvokeToolInput(
            tool_name="office_claw_register_scheduled_task",
            arguments={"templateId": "reminder"},
        ),
    )
    assert result["success"] is False
    # Fail closed at resolve (foreign/unpinned) or refuse-without-binding.
    assert (
        "not bound" in result["error"]
        or "无法获取" in result["error"]
        or "不可用" in result["error"]
    )
    assert "未注册" not in result["error"]
    live_tool.invoke.assert_not_called()


def test_rail_set_office_claw_binding_stores_thread_and_invocation() -> None:
    from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
        ProgressiveToolRail,
    )

    rail = object.__new__(ProgressiveToolRail)
    rail.set_office_claw_active_tool_ids(
        ["office-claw-request-aaa.office-claw.office_claw_register_scheduled_task"],
        delivery_thread_id="thread_mine",
        invocation_id="inv-mine",
    )
    assert rail._office_claw_delivery_thread_id == "thread_mine"
    assert rail._office_claw_invocation_id == "inv-mine"
    rail.set_office_claw_active_tool_ids(None)
    assert rail._office_claw_delivery_thread_id is None
    assert rail._office_claw_invocation_id is None
