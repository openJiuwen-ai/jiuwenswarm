# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bilingual discovery coverage for bundled and optimized tool search."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.tool import (
    McpToolCard,
    ToolCard,
    ToolExposure,
    ToolInfo,
)
from openjiuwen.core.foundation.tool.schema import McpToolInfo
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
    ModelCallInputs,
)
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness import DeepAgent
from openjiuwen.harness.rails.progressive_tool_rail import ProgressiveToolRail
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.tools.cron import CronToolContext

from jiuwenswarm.agents.harness.code.rails.heartbeat_rail import HeartbeatRail
from jiuwenswarm.agents.harness.common.tools.tool_discovery_catalog import (
    BUILTIN_TOOL_DISCOVERY,
    PROGRESSIVE_DISCOVERY_PROPERTY,
    apply_discovery_metadata,
)
from jiuwenswarm.agents.harness.common.tools.tool_retrieval_toolkits import (
    _TOOL_CATEGORIES,
    ToolRetrievalToolkit,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


@pytest.fixture(params=[
    pytest.param((JiuwenSwarmCodeAdapter, "en", False), id="code-bundled"),
    pytest.param((JiuwenSwarmCodeAdapter, "en", True), id="code-catalogue"),
    pytest.param((JiuWenSwarmDeepAdapter, "en", False), id="work-en-bundled"),
    pytest.param((JiuWenSwarmDeepAdapter, "en", True), id="work-en-catalogue"),
    pytest.param((JiuWenSwarmDeepAdapter, "cn", False), id="work-cn-bundled"),
    pytest.param((JiuWenSwarmDeepAdapter, "cn", True), id="work-cn-catalogue"),
])
async def registered_discovery(request, monkeypatch, tmp_path):
    """Register real tools through the product adapter and startup rails."""
    adapter_type, language, optimized = request.param
    session_id = tmp_path.name
    config = {
        "preferred_language": "zh" if language == "cn" else "en",
        "progressive_tool_enabled": True,
        "progressive_tool_optimize": optimized,
    }
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.context.get_current_runtime",
        lambda: SimpleNamespace(session_message_service=SimpleNamespace()),
    )
    adapter = adapter_type()
    adapter.mark_as_session_scoped(session_id)
    adapter._last_mode = "code" if adapter_type is JiuwenSwarmCodeAdapter else "agent"
    adapter._config_base_cache = config
    context = CronToolContext(channel_id="web", session_id=session_id)
    adapter._runtime_cron_tool_context = context
    adapter._cron_runtime.set_backend(SimpleNamespace())
    heartbeat = HeartbeatRail(service=SimpleNamespace(), context=context)
    agent = DeepAgent(AgentCard(id=session_id, name="discovery-registration"))
    agent.configure(DeepAgentConfig(
        card=agent.card,
        tools=[],
        rails=[heartbeat],
        language=adapter._resolve_runtime_language(),
        auto_create_workspace=False,
        progressive_tool_enabled=True,
        tool_owner_id=adapter._tool_owner_id(),
    ))
    adapter._instance = agent
    try:
        adapter._ensure_cron_tools_registered(session_id)
        adapter._ensure_session_messaging_tools_registered(session_id, "web")
        for tool in adapter._create_tool_retrieval_tools():
            adapter._register_agent_owned_tool(tool, adapter._tool_owner_id())
            agent.ability_manager.add(tool.card)
        adapter._drop_pending_progressive_tool_rail()
        adapter._register_tool_retrieval_prompt_rail()
        await agent.ensure_initialized()
        yield adapter, agent, optimized
    finally:
        agent.ability_manager.teardown_tools()
        for rail in list(agent.configured_rails()):
            await agent.unregister_rail(rail)


async def _registered_search(agent: DeepAgent, optimized: bool, query: str) -> list[str]:
    card = agent.ability_manager.get("tool_search")
    tool = Runner.resource_mgr.get_tool(card.id)
    if optimized:
        listing = await tool.invoke({"operation": "list", "keyword": query})
        return [
            line.removeprefix("- [tool] ").split(":", 1)[0]
            for line in listing.splitlines()
            if line.startswith("- [tool] ")
        ]
    output = await tool.invoke({"query": query})
    assert output.success, output.error
    for item in output.data["results"]:
        registered = agent.ability_manager.get(item["name"])
        assert item["description"] == registered.description
        assert item["parameters"] == registered.input_params
    return [item["name"] for item in output.data["results"]]


async def test_registered_search_distinguishes_reminders_and_session_work(registered_discovery):
    _adapter, agent, optimized = registered_discovery
    cases = [
        ("创建一次性定时提醒，2分钟后提醒用户喝水", "cron_create_job", 1),
        ("明天上午九点通知我开会", "cron_create_job", 1),
        # Cross-language lexical retrieval must fit the default schema budget;
        # BM25 alone does not guarantee first place for every paraphrase.
        ("Schedule a daily notification", "cron_create_job", 5),
        ("查看所有定时任务", "cron_list_jobs", 1),
        ("取消明天的提醒", "cron_delete_job", 1),
        ("创建心跳跟进任务，稍后继续", "heartbeat_create_job", 1),
        ("读取其他会话的结果", "session_read", 1),
    ]
    for query, expected, rank_limit in cases:
        names = await _registered_search(agent, optimized, query)
        assert expected in names[:rank_limit], (query, names)


async def test_registered_disclosure_preserves_scheduling_boundary(registered_discovery):
    adapter, agent, optimized = registered_discovery
    for name in ("cron_create_job", "heartbeat_create_job", "session_read"):
        card = agent.ability_manager.get(name)
        assert card.exposure is ToolExposure.DEFERRED
        assert card.properties[PROGRESSIVE_DISCOVERY_PROPERTY]["aliases"]
    assert bool(agent.find_rails_by_type((ProgressiveToolRail,))) is not optimized

    ctx = AgentCallbackContext(
        agent=agent.react_agent,
        inputs=ModelCallInputs(tools=await agent.ability_manager.list_tool_info()),
    )
    await ctx.fire(AgentCallbackEvent.BEFORE_MODEL_CALL)
    assert {tool.name for tool in ctx.inputs.tools} == {"tool_search", "tool_call"}
    if optimized:
        card = agent.ability_manager.get("tool_search")
        search = Runner.resource_mgr.get_tool(card.id)
        directory = await search.invoke({"operation": "list", "category": "heartbeat"})
        schema = json.loads(await search.invoke({
            "operation": "get_schema", "tool": "cron_create_job",
        }))
        cron = agent.ability_manager.get("cron_create_job")
        assert schema["description"] == cron.description
        assert schema["schema"] == cron.input_params
        marker = "- [tool] heartbeat_create_job:"
    else:
        directory = agent.system_prompt_builder.build()
        marker = "- **heartbeat_create_job**:"
    if adapter._resolve_runtime_language() != "en":
        return
    heartbeat_line = next(line for line in directory.splitlines() if line.startswith(marker))
    assert "existing task" in heartbeat_line
    assert "cron_create_job" in heartbeat_line
    assert "independent schedules" in heartbeat_line


async def test_registered_search_tracks_cron_reregistration_and_removal(registered_discovery):
    adapter, agent, optimized = registered_discovery
    old_card = agent.ability_manager.get("cron_create_job")
    await _registered_search(agent, optimized, "明天上午九点通知我开会")
    adapter._cron_tools_registered_language = None
    adapter._ensure_cron_tools_registered(adapter._parent_session_id)
    current_card = agent.ability_manager.get("cron_create_job")
    assert current_card is not old_card
    assert current_card.properties[PROGRESSIVE_DISCOVERY_PROPERTY]["aliases"]
    names = await _registered_search(agent, optimized, "明天上午九点通知我开会")
    assert names[0] == "cron_create_job", names
    agent.ability_manager.remove_ability("cron_create_job")
    names = await _registered_search(agent, optimized, "明天上午九点通知我开会")
    assert "cron_create_job" not in names


def _info(name: str, description: str = "Authoritative tool instructions") -> ToolInfo:
    return ToolInfo(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
    )


def test_every_categorized_builtin_has_english_summary_and_chinese_aliases() -> None:
    names = {name for category in _TOOL_CATEGORIES for name in category.tools}

    assert names <= BUILTIN_TOOL_DISCOVERY.keys()
    for name in names:
        metadata = BUILTIN_TOOL_DISCOVERY[name]
        assert metadata.display_en.strip()
        assert metadata.aliases_zh


def test_code_defaults_to_english_and_work_can_select_english() -> None:
    code = object.__new__(JiuwenSwarmCodeAdapter)
    code._runtime_language_override = None
    work = object.__new__(JiuWenSwarmDeepAdapter)
    work._resolve_prompt_language = lambda: "en"

    assert code._resolve_runtime_language() == "en"
    assert work._resolve_runtime_language() == "en"


def test_builtin_card_gets_discovery_metadata_but_mcp_card_does_not() -> None:
    builtin = ToolCard(id="cron", name="cron_create_job", description="raw")
    mcp = McpToolCard(
        id="maps",
        name="mcp_maps_lookup",
        description="查询地图位置",
        server_name="maps",
    )

    apply_discovery_metadata(builtin)
    apply_discovery_metadata(mcp)

    assert builtin.properties[PROGRESSIVE_DISCOVERY_PROPERTY]["display"]["en"]
    assert "提醒我" in builtin.properties[PROGRESSIVE_DISCOVERY_PROPERTY]["aliases"]
    assert PROGRESSIVE_DISCOVERY_PROPERTY not in mcp.properties


@pytest.mark.asyncio
async def test_bundled_search_finds_chinese_query_in_english_mode() -> None:
    cards = [
        ToolCard(
            id=name,
            name=name,
            description="Authoritative tool instructions",
            exposure=ToolExposure.DEFERRED,
            input_params={"type": "object", "properties": {}},
        )
        for name in BUILTIN_TOOL_DISCOVERY
    ]
    for card in cards:
        apply_discovery_metadata(card)
    target = next(card for card in cards if card.name == "cron_create_job")
    target.description = "Create a scheduled job with the supplied cron expression."
    target.input_params = {
        "type": "object",
        "properties": {"cron": {"type": "string"}},
    }
    registry = SimpleNamespace(list=lambda: cards, registry_revision=0)
    agent = SimpleNamespace(
        card=SimpleNamespace(id="code-agent"),
        ability_manager=registry,
    )
    rail = ProgressiveToolRail(
        DeepAgentConfig(progressive_tool_enabled=True, language="en")
    )
    rail._tool_search_registry = registry
    rail._rebuild_tool_search_index(agent)

    matches = await rail._search_tools("请提醒我明天上午十点开会")

    assert matches[0]["name"] == "cron_create_job"
    assert matches[0]["description"] == target.description
    assert rail._collect_deferred_tool_descriptions(agent)["cron_create_job"] == (
        BUILTIN_TOOL_DISCOVERY["cron_create_job"].display_en
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "query"),
    [
        ("cron_create_job", "提醒我明天开会"),
        ("audio_transcription", "把这个录音转成文字"),
        ("search_skill", "找一个可以处理表格的技能"),
        ("enter_worktree", "创建隔离工作树"),
        ("session_read", "看看另一个会话的结果"),
    ],
)
async def test_optimized_search_finds_chinese_query_in_english_mode(
    name: str,
    query: str,
) -> None:
    infos = [_info(tool_name) for tool_name in BUILTIN_TOOL_DISCOVERY]
    info = next(tool for tool in infos if tool.name == name)
    toolkit = ToolRetrievalToolkit(
        deferred_tools_provider=lambda: infos,
        language="en",
    )

    search = toolkit.get_tools()[0]
    result = await search.invoke({"operation": "list", "keyword": query})
    schema = json.loads(
        await search.invoke({"operation": "get_schema", "tool": name})
    )

    assert result.splitlines()[2].startswith(f"- [tool] {name}:")
    assert schema["description"] == info.description


@pytest.mark.asyncio
async def test_optimized_search_keeps_mcp_metadata_verbatim() -> None:
    info = McpToolInfo(
        name="mcp_maps_lookup",
        description="查询地图位置",
        parameters={"type": "object"},
        server_name="maps",
    )
    toolkit = ToolRetrievalToolkit(
        deferred_tools_provider=lambda: [info],
        mcp_servers_provider=lambda: {"maps": {"zh": "地图服务", "en": "Map services"}},
        language="en",
    )
    search = toolkit.get_tools()[0]

    listing = await search.invoke({"operation": "list", "category": "mcp_maps"})
    schema = json.loads(
        await search.invoke({"operation": "get_schema", "tool": info.name})
    )

    assert "查询地图位置" in listing
    assert schema["description"] == info.description
