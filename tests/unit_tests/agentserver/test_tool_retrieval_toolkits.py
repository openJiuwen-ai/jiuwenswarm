# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the static-category progressive tool_search layer."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.tools.tool_retrieval_toolkits import (
    _TOOL_CATEGORIES,
    ToolRetrievalToolkit,
)
from openjiuwen.core.foundation.tool import ToolExposure, ToolInfo


def _deferred_tools() -> list[ToolInfo]:
    # Real names from the static category table so list/category navigation
    # exercises the actual mappings.
    return [
        ToolInfo(
            name="cron_create_job",
            description="创建新的 cron 定时任务",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        ToolInfo(name="cron_list_jobs", description="列出所有 cron 定时任务"),
        ToolInfo(name="heartbeat_run_now", description="Run a heartbeat job now"),
    ]


def _build_toolkit(
    tools: list[ToolInfo] | None = None,
    mcp_servers_provider=None,
) -> ToolRetrievalToolkit:
    provider = (lambda: tools) if tools is not None else _deferred_tools
    return ToolRetrievalToolkit(
        deferred_tools_provider=provider,
        session_scope="test",
        mcp_servers_provider=mcp_servers_provider,
    )


def test_static_category_table_shape() -> None:
    """The in-code table carries the built-in deferred inventory."""

    paths = [category.path for category in _TOOL_CATEGORIES]
    # Flat names only — the two-level ``A > B`` paths are gone.
    assert all(">" not in path for path in paths)
    assert "cron" in paths
    assert "heartbeat" in paths
    assert "terminal" in paths
    # The settings-page toggles (图片/音频处理、视频理解、视频/图像生成) map to
    # these deferred tools; toggle-off just leaves them out of the live registry.
    assert "image-processing" in paths
    assert "audio-processing" in paths
    assert "video-understanding" in paths
    assert "image-generation" in paths
    assert "video-generation" in paths
    # agent_evolving family: six deferred tools that previously landed in the
    # uncategorized fallback block.
    assert "skill-evolution" in paths
    by_path = {category.path: category for category in _TOOL_CATEGORIES}
    assert "powershell" in by_path["terminal"].tools
    assert "cron_create_job" in by_path["cron"].tools
    assert "read_pdf" in by_path["documents"].tools
    assert "video_understanding" in by_path["video-understanding"].tools
    assert "audio_metadata" in by_path["audio-processing"].tools
    assert "generate_visual" in by_path["image-generation"].tools
    assert "check_video_status" in by_path["video-generation"].tools
    assert "prepare_skill_evolution" in by_path["skill-evolution"].tools
    assert "simplify_skill_experiences" in by_path["skill-evolution"].tools
    # Descriptions are description-only routing text, never tool-name dumps.
    # (Bare words like "cron" may occur naturally; full underscore names
    # must not.)
    for category in _TOOL_CATEGORIES:
        for name in category.tools:
            if "_" in name:
                assert name not in category.description


def test_tool_search_card_is_registered_with_tool_search_name() -> None:
    toolkit = _build_toolkit()

    tools = toolkit.get_tools()

    assert [tool.card.name for tool in tools] == ["tool_search"]
    # Mirrors the skill_index card contract: same parameter set and limits,
    # reworded for tools (call_goal is NOT in the card — stream_event_rail
    # injects it uniformly into the model-facing ToolInfo; the singular
    # string ``tool`` drives the schema-read phase).
    assert set(tools[0].card.input_params["properties"].keys()) == {
        "operation",
        "category",
        "tool",
    }
    assert tools[0].card.input_params["required"] == ["operation"]
    assert tools[0].card.input_params["properties"]["operation"]["enum"] == [
        "list",
        "get_schema",
    ]
    assert "Skills" not in tools[0].card.input_params["properties"]["operation"]["description"]
    assert "SKILL.md" not in tools[0].card.input_params["properties"]["operation"]["description"]


def test_root_list_serves_categories_description_only() -> None:
    """The root listing shows category lines only — no flat tool dump."""

    tool = _build_toolkit().get_tools()[0]

    listing = asyncio.run(tool.invoke({"operation": "list"}))

    assert "[category] cron:" in listing
    assert "[category] heartbeat:" in listing
    # Categorized tools are NOT listed at the root: their names are
    # discovered through the category listing, not a flat dump.
    assert "cron_create_job" not in listing
    # No pagination footer — listings are served whole.
    assert "[next]" not in listing


def test_category_list_serves_tools_schema_free() -> None:
    """list(category=...) returns member tools with short descriptions only."""

    toolkit = _build_toolkit()
    tool = toolkit.get_tools()[0]

    listing = asyncio.run(
        tool.invoke({"operation": "list", "category": "cron"})
    )

    assert "## Tools (category: cron)" in listing
    assert "- [tool] cron_create_job: 创建新的 cron 定时任务" in listing
    assert "- [tool] cron_list_jobs: 列出所有 cron 定时任务" in listing
    assert "heartbeat_run_now" not in listing
    assert '"parameters"' not in listing


def test_category_matching_tolerates_casing_only() -> None:
    """Casing/whitespace resolve; retired two-level spellings error with names."""

    toolkit = _build_toolkit()
    tool = toolkit.get_tools()[0]

    cased = asyncio.run(
        tool.invoke({"operation": "list", "category": " CRON "})
    )
    assert "cron_create_job" in cased

    # Two-level spellings from retired prompt snapshots no longer resolve;
    # the error lists the valid flat names so the model can retry directly.
    with pytest.raises(ValueError, match="is not a catalogue category") as caught:
        asyncio.run(
            tool.invoke({"operation": "list", "category": "scheduling > cron"})
        )
    assert "cron, heartbeat" in str(caught.value)


def test_unknown_category_error_lists_valid_names() -> None:
    tool = _build_toolkit().get_tools()[0]

    with pytest.raises(ValueError, match="is not a catalogue category"):
        asyncio.run(tool.invoke({"operation": "list", "category": "nope"}))

    try:
        asyncio.run(tool.invoke({"operation": "list", "category": "nope"}))
    except ValueError as exc:
        # The error carries the valid names so the model can retry directly.
        assert "cron, heartbeat" in str(exc)


def test_uncategorized_and_connector_tools_at_root() -> None:
    """Static-table misses surface at the root; MCP tools group by connector."""

    tools = _deferred_tools() + [
        ToolInfo(name="pdf_read", description="Read text from a PDF file"),
        ToolInfo(name="mcp_amap_maps_geo", description="将地址转换为经纬度坐标"),
        ToolInfo(name="mcp_amap_maps_text_search", description="关键词搜POI"),
    ]
    toolkit = _build_toolkit(
        tools,
        mcp_servers_provider=lambda: {"amap": {"zh": "高德地图位置服务。", "en": ""}},
    )
    listing = asyncio.run(toolkit.tool_search(operation="list"))

    assert "- [tool] pdf_read: Read text from a PDF file" in listing
    assert "[category] mcp_amap: 高德地图位置服务。" in listing
    # Connector tools ride their virtual branch, not [tool] lines.
    assert "mcp_amap_maps_geo: " not in listing


def test_list_get_schema_roundtrip() -> None:
    """list returns names + short descriptions; get_schema returns the full
    {name, description, schema} record for exactly the requested tool."""

    toolkit = _build_toolkit()
    tool = toolkit.get_tools()[0]

    gs = json.loads(
        str(asyncio.run(tool.invoke({"operation": "get_schema", "tool": "cron_create_job"})))
    )
    assert gs["name"] == "cron_create_job"
    assert gs["schema"]["required"] == ["name"]
    assert gs["description"]
    assert "tool_call" in gs["usage"]

    # get_schema without tool is rejected with an actionable message.
    with pytest.raises(ValueError, match="get_schema requires"):
        asyncio.run(tool.invoke({"operation": "get_schema"}))

    # The card schema takes a single string name (harness-level [189001]
    # validation fires before the toolkit func runs; the retired array
    # spelling and the extra names it carried both die here).
    from openjiuwen.core.common.exception.errors import (
        ValidationError as OpenJiuwenValidationError,
    )

    with pytest.raises(OpenJiuwenValidationError):
        asyncio.run(
            tool.invoke(
                {"operation": "get_schema", "tool": ["cron_create_job", "cron_list_jobs"]}
            )
        )

    # A direct call holding the retired array spelling degrades to its
    # first name instead of erroring (long sessions, lenient paths).
    legacy = json.loads(
        str(asyncio.run(toolkit.tool_search(operation="get_schema", tools=["cron_create_job"])))
    )
    assert legacy["name"] == "cron_create_job"

    # Unknown names are rejected with an actionable message.
    with pytest.raises(ValueError, match="not in the deferred catalogue"):
        asyncio.run(tool.invoke({"operation": "get_schema", "tool": "nope"}))


def test_retired_fields_degrade_instead_of_erroring() -> None:
    """Old prompt snapshots carrying retired fields keep working.

    Keyword search is no longer offered on the card; card validation
    rejects the field for model calls, and direct/lenient paths degrade to
    a plain unfiltered listing. ``search`` aliases list.
    """

    toolkit = _build_toolkit()

    legacy = asyncio.run(
        toolkit.tool_search(operation="search", query=["cron"])
    )
    assert "[category] cron:" in legacy

    degraded = asyncio.run(
        toolkit.tool_search(
            operation="list", category="cron", query=["cron"], cursor="x"
        )
    )
    assert "cron_create_job" in degraded


def test_root_listing_uncapped() -> None:
    """Root listings are served whole — every uncategorized tool appears."""

    tools = [
        ToolInfo(name=f"tool_{index}", description=f"Tool number {index}")
        for index in range(25)
    ]
    toolkit = _build_toolkit(tools)

    result = asyncio.run(toolkit.tool_search(operation="list"))

    for index in range(25):
        assert f"- [tool] tool_{index}: Tool number {index}" in result
    assert "[next]" not in result


def test_frozen_catalogue_intersects_live_registry() -> None:
    """The snapshot only carries categories with live tools."""

    tools = [
        ToolInfo(name="cron_create_job", description="创建 cron 任务"),
        ToolInfo(name="read_pdf", description="读 PDF"),
    ]
    toolkit = _build_toolkit(tools)
    snapshot = toolkit.frozen_catalogue

    assert [category.path for category in snapshot.categories] == [
        "documents",
        "cron",
    ]
    assert snapshot.categories[1].tools == ("cron_create_job",)
    assert {entry.name for entry in snapshot.flat_entries} == {
        "cron_create_job",
        "read_pdf",
    }

    # refresh_snapshot() re-freezes against the (possibly grown) registry.
    tools.append(ToolInfo(name="powershell", description="执行 PowerShell 命令"))
    toolkit.refresh_snapshot()
    assert "terminal" in [
        category.path for category in toolkit.frozen_catalogue.categories
    ]


def test_tool_call_envelope_registered_and_dispatches() -> None:
    """The advertised tool_call envelope owns deferred execution.

    Deferred tools stay out of the advertised tools list; the rail registers
    a fixed ``tool_call`` meta tool (exposure DIRECT) that authorizes against
    the live deferred catalogue and dispatches through AbilityManager — the
    execution contract the model demonstrably calls correctly.
    """

    from jiuwenswarm.agents.harness.common.rails.tool_retrieval_prompt_rail import (
        ToolRetrievalPromptRail,
    )

    toolkit = _build_toolkit()
    tool = toolkit.get_tools()[0]

    # The schema-read phase serves one flat record per call.
    gs = json.loads(
        str(
            asyncio.run(
                tool.invoke({"operation": "get_schema", "tool": "cron_create_job"})
            )
        )
    )
    assert gs["name"] == "cron_create_job"
    assert gs["schema"]["required"] == ["name"]

    cards = {
        "read_file": SimpleNamespace(name="read_file", exposure=ToolExposure.DIRECT),
        "cron_create_job": SimpleNamespace(
            name="cron_create_job", exposure=ToolExposure.DEFERRED
        ),
    }
    registered: dict = {}

    def _add_ability(card, tool_instance):
        registered[card.name] = tool_instance
        return SimpleNamespace(added=True)

    async def _execute(*, ctx, tool_call, session, parallel_tool_calls):
        assert tool_call.name == "cron_create_job"
        assert json.loads(tool_call.arguments) == {"name": "remind"}
        assert parallel_tool_calls is False
        return [("ok", SimpleNamespace(content=""))]

    agent = SimpleNamespace(
        system_prompt_builder=None,
        ability_manager=SimpleNamespace(
            add_ability=_add_ability,
            get=lambda n: cards.get(n)
            or (registered[n].card if n in registered else None),
            list=lambda: list(cards.values())
            + [t.card for t in registered.values()],
            remove_ability=lambda n: registered.pop(n, None),
            execute=_execute,
        ),
    )

    rail = ToolRetrievalPromptRail(toolkit=toolkit)
    rail.init(agent)
    rail._agent = agent

    # Envelope registered as a DIRECT meta tool.
    assert "tool_call" in registered
    assert registered["tool_call"].card.exposure == ToolExposure.DIRECT

    # Filter keeps DIRECT + meta tools; deferred stay hidden.
    inputs = SimpleNamespace(
        tools=[
            SimpleNamespace(name="read_file"),
            SimpleNamespace(name="cron_create_job"),
            SimpleNamespace(name="tool_search"),
            SimpleNamespace(name="tool_call"),
        ]
    )
    rail._filter_deferred_from_inputs(SimpleNamespace(inputs=inputs))
    assert {t.name for t in inputs.tools} == {"read_file", "tool_search", "tool_call"}

    # Dispatch: authorized catalogue name executes through AbilityManager.
    output = asyncio.run(
        rail._call_deferred_tool(
            "cron_create_job",
            {"name": "remind"},
            None,
            SimpleNamespace(agent=agent, inputs=None, extra={}),
        )
    )
    assert output.success is True
    assert output.data["name"] == "cron_create_job"

    # Non-catalogue names are rejected without touching AbilityManager.
    rejected = asyncio.run(
        rail._call_deferred_tool(
            "not_registered",
            {},
            None,
            SimpleNamespace(agent=agent, inputs=None, extra={}),
        )
    )
    assert rejected.success is False

    rail.uninit(agent)
    assert "tool_call" not in registered


def test_collect_deferred_tool_infos_is_explicit_only() -> None:
    """Only cards explicitly marked DEFERRED enter the catalogue."""

    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    # Bypass the heavyweight adapter constructor; the projection under test is
    # a pure function of the card list.
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    cards = [
        SimpleNamespace(
            name="cron_create_job",
            description="Create a cron schedule task",
            exposure=ToolExposure.DEFERRED,
            input_params={"type": "object", "properties": {}},
        ),
        SimpleNamespace(
            name="bash",
            description="direct shell tool",
            exposure=ToolExposure.DIRECT,
            input_params={"type": "object", "properties": {}},
        ),
        SimpleNamespace(
            name="no_exposure",
            description="legacy card without exposure",
            input_params={"type": "object", "properties": {}},
        ),
    ]

    infos = adapter._collect_deferred_tool_infos(cards)

    assert [info.name for info in infos] == ["cron_create_job"]
    assert infos[0].parameters == {"type": "object", "properties": {}}


def test_mcp_connector_groups_by_prefix() -> None:
    """MCP tools group by server prefix; known server names match precisely."""

    tools = _deferred_tools() + [
        ToolInfo(name="mcp_amap_maps_geo", description="d1"),
        ToolInfo(name="mcp_amap_maps_text_search", description="d2"),
        ToolInfo(name="mcp_my_server_tool", description="d3"),
    ]
    toolkit = _build_toolkit(
        tools,
        mcp_servers_provider=lambda: {
            "amap": {"zh": "接入高德地图各类位置服务。", "en": "AMap location services."},
            "my_server": {"zh": "", "en": ""},
        },
    )

    groups = toolkit.mcp_connector_groups()

    assert set(groups["amap"]["tools"]) == {
        "mcp_amap_maps_geo",
        "mcp_amap_maps_text_search",
    }
    assert groups["amap"]["description_zh"] == "接入高德地图各类位置服务。"
    assert groups["amap"]["description_en"] == "AMap location services."
    # Multi-underscore server names resolve via the known-name list, not the
    # first-segment fallback.
    assert groups["my_server"]["tools"] == ("mcp_my_server_tool",)
    # Non-MCP deferred tools never join a connector group.
    assert all(
        not name.startswith(("cron", "heartbeat"))
        for group in groups.values()
        for name in group["tools"]
    )


def test_mcp_connector_groups_fallback_without_provider() -> None:
    """Without a provider the first prefix segment still groups the tools."""

    tools = _deferred_tools() + [
        ToolInfo(name="mcp_amap_maps_geo", description="d"),
    ]
    toolkit = _build_toolkit(tools)

    groups = toolkit.mcp_connector_groups()

    assert groups["amap"]["tools"] == ("mcp_amap_maps_geo",)
    assert groups["amap"]["description_zh"] == ""
    assert groups["amap"]["description_en"] == ""


def test_tool_search_lists_connector_category() -> None:
    """list on a connector category serves the group, engine-free."""

    tools = _deferred_tools() + [
        ToolInfo(name="mcp_amap_maps_geo", description="将地址转换为经纬度坐标"),
        ToolInfo(name="mcp_amap_maps_text_search", description="关键词搜POI"),
    ]
    toolkit = _build_toolkit(
        tools,
        mcp_servers_provider=lambda: {
            "amap": {"zh": "接入高德地图各类位置服务。", "en": "AMap location services."},
        },
    )

    listing = asyncio.run(
        toolkit.tool_search(operation="list", category="mcp_amap")
    )

    assert "MCP connector: amap" in listing
    assert "接入高德地图各类位置服务" in listing
    assert "- [tool] mcp_amap_maps_geo:" in listing
    # Only the connector's tools are listed.
    assert "cron_create_job" not in listing


def test_installed_mcp_descriptions_reads_manifests(tmp_path, monkeypatch) -> None:
    """Manifest display_description feeds the connector descriptions."""

    from jiuwenswarm.server.runtime.mcp import marketplace

    (tmp_path / "amap").mkdir()
    (tmp_path / "amap" / "manifest.json").write_text("{}", encoding="utf-8")
    fake = SimpleNamespace(
        server_name="amap",
        package_id="amap",
        raw={
            "display_description": {
                "zh": "接入高德地图各类位置服务。",
                "en": "AMap location services.",
            }
        },
    )
    monkeypatch.setattr(marketplace, "load_mcp_package", lambda d, **k: fake)
    monkeypatch.setattr(marketplace, "_builtin_root", lambda: tmp_path)
    monkeypatch.setattr(marketplace, "_hub_root", lambda: tmp_path / "missing")

    out = marketplace.installed_mcp_descriptions()

    assert out["amap"] == {
        "zh": "接入高德地图各类位置服务。",
        "en": "AMap location services.",
    }
