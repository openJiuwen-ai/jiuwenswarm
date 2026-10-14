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
    """The in-code tree carries the built-in deferred inventory."""

    paths = [category.path for category in _TOOL_CATEGORIES]
    assert "scheduling > cron" in paths
    assert "scheduling > heartbeat" in paths
    assert "terminal" in paths
    by_path = {category.path: category for category in _TOOL_CATEGORIES}
    assert "powershell" in by_path["terminal"].tools
    assert "cron_create_job" in by_path["scheduling > cron"].tools
    assert "read_pdf" in by_path["documents"].tools
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
    # Two operations only: keyword search (name+short description+category,
    # schema-free) and get_schema for the tool the model has picked. The
    # retired list/category browse is gone from the card.
    assert set(tools[0].card.input_params["properties"].keys()) == {
        "operation",
        "query",
        "tool",
    }
    assert tools[0].card.input_params["required"] == ["operation"]
    assert tools[0].card.input_params["properties"]["operation"]["enum"] == [
        "search",
        "get_schema",
    ]
    assert "Skills" not in tools[0].card.input_params["properties"]["operation"]["description"]
    assert "SKILL.md" not in tools[0].card.input_params["properties"]["operation"]["description"]


def test_search_serves_matches_schema_free() -> None:
    """search returns name + short description + category, never schemas."""

    toolkit = _build_toolkit()
    tool = toolkit.get_tools()[0]

    result = asyncio.run(
        tool.invoke({"operation": "search", "query": "cron 定时任务"})
    )

    assert 'matching "cron 定时任务"' in result
    assert "- [tool] cron_create_job: 创建新的 cron 定时任务" in result
    assert "(category: scheduling > cron)" in result
    # Search output is tool lines only — no [category] navigation lines.
    assert "- [category]" not in result
    # Non-matching tools stay out; no schemas, no pagination.
    assert "heartbeat_run_now" not in result
    assert '"parameters"' not in result
    assert "[next]" not in result


def test_search_match_by_description_and_guessed_name() -> None:
    """Description keywords hit; a guessed near-name degrades to a match."""

    toolkit = _build_toolkit()
    tool = toolkit.get_tools()[0]

    # English description keyword.
    by_desc = asyncio.run(
        tool.invoke({"operation": "search", "query": "heartbeat job"})
    )
    assert "- [tool] heartbeat_run_now: Run a heartbeat job now" in by_desc

    # A guessed exact-ish name matches by name containment, both directions.
    guessed = asyncio.run(
        tool.invoke({"operation": "search", "query": "heartbeat_run"})
    )
    assert "heartbeat_run_now" in guessed


def test_search_miss_states_absence_instead_of_erroring() -> None:
    """No match renders an explicit absence answer — the enumeration guard.

    Regression shape (pinchbench task_image_gen): with image generation
    available only as a skill, the model must be able to conclude "the
    deferred tools do not provide this" in ONE round, not walk categories.
    """

    tool = _build_toolkit().get_tools()[0]

    result = asyncio.run(
        tool.invoke({"operation": "search", "query": "generate image png"})
    )

    assert "No deferred tool matches" in result
    assert "do not provide this capability" in result
    assert "another route" in result


def test_search_weak_matches_carry_absence_license() -> None:
    """Keyword-noise results must still state the absence license.

    Regression shape (pinchbench task_image_gen, search era): intent
    queries like "generate image file" pick up weak description matches
    (read_pdf mentions images/files), and the clean no-match answer never
    fires — the model rephrased the query 5 times hunting for it. Results
    with no name-level hit must carry the license to conclude absence.
    """

    tools = _deferred_tools() + [
        ToolInfo(
            name="read_pdf",
            description="Read a PDF file; pages without a text layer are "
            "reported as likely scanned images.",
        ),
    ]
    toolkit = _build_toolkit(tools)
    tool = toolkit.get_tools()[0]

    result = asyncio.run(
        tool.invoke({"operation": "search", "query": "generate image file"})
    )

    # The weak match is still served…
    assert "read_pdf" in result
    # …together with the license to conclude absence.
    assert "Only weak keyword matches" in result
    assert "do not provide this capability" in result

    # A name-level match is strong: no license note.
    strong = asyncio.run(
        tool.invoke({"operation": "search", "query": "cron image file"})
    )
    assert "cron_create_job" in strong
    assert "Only weak keyword matches" not in strong


def test_search_fixed_top_five_results() -> None:
    """No limit knob on the card: search always serves at most top 5."""
    tools = [
        ToolInfo(name=f"cron_tool_{index}", description=f"cron job {index}")
        for index in range(10)
    ]
    toolkit = _build_toolkit(tools)

    capped = asyncio.run(toolkit.tool_search(operation="search", query="cron"))
    assert capped.count("- [tool] cron_tool_") == 5

    # A legacy limit kwarg (retired from the card) is popped defensively,
    # never honored — the top-5 bound holds regardless.
    legacy = asyncio.run(
        toolkit.tool_search(operation="search", query="cron", limit=99)
    )
    assert legacy.count("- [tool] cron_tool_") == 5


def test_uncategorized_and_connector_tools_searchable() -> None:
    """Static-table misses and MCP connector tools both surface in search."""

    tools = _deferred_tools() + [
        ToolInfo(name="pdf_read", description="Read text from a PDF file"),
        ToolInfo(name="mcp_amap_maps_geo", description="将地址转换为经纬度坐标"),
        ToolInfo(name="mcp_amap_maps_text_search", description="关键词搜POI"),
    ]
    toolkit = _build_toolkit(
        tools,
        mcp_servers_provider=lambda: {"amap": {"zh": "高德地图位置服务。", "en": ""}},
    )

    # Uncatalogued tool matches by name/description, no category suffix.
    by_name = asyncio.run(toolkit.tool_search(operation="search", query="pdf"))
    assert "- [tool] pdf_read: Read text from a PDF file" in by_name

    # Connector tools, annotated with the virtual branch.
    by_intent = asyncio.run(
        toolkit.tool_search(operation="search", query="地址 经纬度 坐标")
    )
    assert "(category: mcp_amap)" in by_intent
    assert "mcp_amap_maps_geo:" in by_intent


def test_get_schema_roundtrip() -> None:
    """search returns names + short descriptions; get_schema returns the full
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

    A list-valued ``query`` degrades to its string form; the retired
    ``list`` operation degrades to a keyword search (its category becomes
    the query), and a bare root list falls back to the category overview.
    """

    toolkit = _build_toolkit()

    legacy = asyncio.run(
        toolkit.tool_search(operation="search", query=["cron"])
    )
    assert "cron_create_job" in legacy

    degraded = asyncio.run(
        toolkit.tool_search(
            operation="list", category="cron 定时", query="", cursor="x"
        )
    )
    assert "cron_create_job" in degraded

    root = asyncio.run(toolkit.tool_search(operation="list"))
    assert "[category] scheduling > cron:" in root


def test_unknown_operation_error_points_to_search() -> None:
    # Direct toolkit call: card-level enum validation would reject the
    # operation before the toolkit func runs.
    toolkit = _build_toolkit()

    with pytest.raises(ValueError, match="tool_search supports"):
        asyncio.run(toolkit.tool_search(operation="browse"))

    try:
        asyncio.run(toolkit.tool_search(operation="browse"))
    except ValueError as exc:
        # The error carries a search example so the model can retry directly.
        assert '"operation":"search"' in str(exc)


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
        "scheduling > cron",
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


def test_tool_search_finds_connector_tools() -> None:
    """A connector-name query surfaces the connector's tools."""

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

    result = asyncio.run(toolkit.tool_search(operation="search", query="mcp_amap"))

    assert "- [tool] mcp_amap_maps_geo:" in result
    assert "(category: mcp_amap)" in result


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
