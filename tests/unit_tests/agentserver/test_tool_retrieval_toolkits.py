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
    # Code-mode rails/registrations: coding memory, LSP, worktree, user todos
    # also previously landed in the uncategorized fallback block.
    assert "coding-memory" in paths
    assert "lsp" in paths
    assert "worktree" in paths
    assert "user-todos" in paths
    by_path = {category.path: category for category in _TOOL_CATEGORIES}
    assert "powershell" in by_path["terminal"].tools
    assert "cron_create_job" in by_path["cron"].tools
    assert "read_pdf" in by_path["documents"].tools
    assert "video_understanding" in by_path["video-understanding"].tools
    assert "audio_metadata" in by_path["audio-processing"].tools
    assert "generate_visual" in by_path["image-generation"].tools
    assert "check_video_status" in by_path["video-generation"].tools
    assert "coding_memory_write" in by_path["coding-memory"].tools
    assert "enter_worktree" in by_path["worktree"].tools
    assert "user_todos" in by_path["user-todos"].tools
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
    # string ``tool`` drives the schema-read phase). ``keyword`` (BM25
    # filter within one category) is tool-side only.
    assert set(tools[0].card.input_params["properties"].keys()) == {
        "operation",
        "category",
        "keyword",
        "tool",
    }
    assert tools[0].card.input_params["required"] == ["operation"]
    assert tools[0].card.input_params["properties"]["operation"]["enum"] == [
        "list",
        "get_schema",
    ]
    assert "Skills" not in tools[0].card.input_params["properties"]["operation"]["description"]
    assert "SKILL.md" not in tools[0].card.input_params["properties"]["operation"]["description"]


def test_bare_list_errors_with_call_shapes() -> None:
    """Bare list (no category, no keyword) errors — the category catalogue
    already sits in the system prompt, so re-dumping it wastes a call."""

    toolkit = _build_toolkit()
    tool = toolkit.get_tools()[0]

    with pytest.raises(
        ValueError, match='list needs "category" or "keyword"'
    ):
        asyncio.run(tool.invoke({"operation": "list"}))


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

    ``query`` is dropped (it is not the live keyword field), so a legacy
    bare search now hits the category/keyword requirement error — which
    teaches the retry — while legacy list calls that already carry a
    category degrade to the plain unfiltered listing. ``search`` aliases
    list.
    """

    toolkit = _build_toolkit()

    with pytest.raises(
        ValueError, match='list needs "category" or "keyword"'
    ):
        asyncio.run(toolkit.tool_search(operation="search", query=["cron"]))

    degraded = asyncio.run(
        toolkit.tool_search(
            operation="list", category="cron", query=["cron"], cursor="x"
        )
    )
    assert "cron_create_job" in degraded
    assert "cron_list_jobs" in degraded


def test_list_keyword_filters_category_by_bm25() -> None:
    """list + keyword BM25-filters one category; the header echoes both."""

    toolkit = _build_toolkit()
    tool = toolkit.get_tools()[0]

    listing = asyncio.run(
        tool.invoke({"operation": "list", "category": "cron", "keyword": "创建"})
    )

    assert '## Tools (category: cron, keyword: "创建")' in listing
    assert "- [tool] cron_create_job: 创建新的 cron 定时任务" in listing
    # "创建" only matches the create tool's description; the list tool drops.
    assert "cron_list_jobs" not in listing


def test_list_keyword_exact_tool_name_ranks_first() -> None:
    """A keyword naming one full tool ranks that tool first."""

    toolkit = _build_toolkit()

    listing = asyncio.run(
        toolkit.tool_search(
            operation="list", category="cron", keyword="cron_list_jobs"
        )
    )

    assert listing.index("cron_list_jobs") < listing.index("cron_create_job")


def test_list_keyword_without_category_searches_all_tools() -> None:
    """keyword with no category BM25-searches every live deferred tool."""

    toolkit = _build_toolkit()
    tool = toolkit.get_tools()[0]

    listing = asyncio.run(tool.invoke({"operation": "list", "keyword": "cron"}))

    assert '## Tools (keyword: "cron")' in listing
    assert "- [tool] cron_create_job:" in listing
    assert "- [tool] cron_list_jobs:" in listing
    # "cron" matches neither the heartbeat tool's name nor its description.
    assert "heartbeat_run_now" not in listing


def test_list_keyword_global_search_covers_uncategorized_and_connectors() -> None:
    """The root keyword sweep includes uncategorized and MCP tools."""

    tools = _deferred_tools() + [
        ToolInfo(name="pdf_read", description="Read text from a PDF file"),
        ToolInfo(name="mcp_amap_maps_geo", description="将地址转换为经纬度坐标"),
    ]
    toolkit = _build_toolkit(tools)

    listing = asyncio.run(toolkit.tool_search(operation="list", keyword="PDF"))

    assert '## Tools (keyword: "PDF")' in listing
    assert "- [tool] pdf_read:" in listing
    assert "cron_create_job" not in listing


def test_list_keyword_global_search_without_matches() -> None:
    """A global no-hit keyword returns a 0/N notice, not an error."""

    toolkit = _build_toolkit()

    # The no-hit keyword shares no token (whole sequence or bigram) with
    # any fixture doc.
    listing = asyncio.run(
        toolkit.tool_search(operation="list", keyword="视频生成")
    )

    assert 'keyword: "视频生成"' in listing
    assert 'No tools match keyword "视频生成" (0 of 3)' in listing


def test_list_keyword_junk_recall_regression() -> None:
    """Keyword recall stays relevance-bound: junk descriptions sharing no
    token with the query never surface (the user-visible failure for
    keyword "图片生成 image generation" was a junk tail of 完成/发生-style
    descriptions)."""

    tools = _deferred_tools() + [
        ToolInfo(
            name="generate_image",
            description="文生图：按文字提示词生成图片并保存到工作区。",
        ),
        ToolInfo(
            name="generate_visual",
            description="文生图：按文字提示词生成图片并保存到工作区。",
        ),
        ToolInfo(
            name="goal_report",
            description=(
                "提交目标执行的结构化报告；任务完成或受阻时调用，"
                "需求发生显著变化时也适用。"
            ),
        ),
    ]
    toolkit = _build_toolkit(tools)

    listing = asyncio.run(
        toolkit.tool_search(
            operation="list", keyword="图片生成 image generation"
        )
    )

    assert "- [tool] generate_image:" in listing
    assert "- [tool] generate_visual:" in listing
    # goal_report shares only the single characters 成/生 — dropped.
    assert "goal_report" not in listing
    assert "cron_create_job" not in listing
    assert "heartbeat_run_now" not in listing


def test_list_keyword_junk_only_corpus_reports_no_match() -> None:
    """With nothing relevant registered, the sweep says so honestly
    instead of returning the unigram junk tail."""

    tools = [
        ToolInfo(name="cron_create_job", description="创建新的 cron 定时任务"),
        ToolInfo(
            name="goal_report",
            description=(
                "提交目标执行的结构化报告；任务完成或受阻时调用，"
                "需求发生显著变化时也适用。"
            ),
        ),
    ]
    toolkit = _build_toolkit(tools)

    listing = asyncio.run(
        toolkit.tool_search(
            operation="list", keyword="图片生成 image generation"
        )
    )

    assert "No tools match" in listing
    assert "0 of 2" in listing


def test_list_keyword_single_character_query_matches_nothing() -> None:
    """Single-character queries match nothing — honest empty."""

    toolkit = _build_toolkit()

    listing = asyncio.run(toolkit.tool_search(operation="list", keyword="创"))

    assert "0 of 3" in listing


def test_list_keyword_without_matches_reports_counts() -> None:
    """A no-hit keyword returns a 0/N notice instead of raising."""

    toolkit = _build_toolkit()

    listing = asyncio.run(
        toolkit.tool_search(operation="list", category="cron", keyword="视频生成")
    )

    assert 'keyword: "视频生成"' in listing
    assert 'No tools match keyword "视频生成" (0 of 2)' in listing


def test_list_keyword_english_matches_description() -> None:
    """English keywords hit ASCII descriptions across token boundaries."""

    toolkit = _build_toolkit()

    listing = asyncio.run(
        toolkit.tool_search(operation="list", category="heartbeat", keyword="run")
    )

    assert "- [tool] heartbeat_run_now:" in listing


def test_list_keyword_filters_connector_category() -> None:
    """Connector virtual branches support the same keyword filter."""

    tools = _deferred_tools() + [
        ToolInfo(name="mcp_amap_maps_geo", description="将地址转换为经纬度坐标"),
        ToolInfo(name="mcp_amap_maps_text_search", description="关键词搜POI"),
    ]
    toolkit = _build_toolkit(
        tools,
        mcp_servers_provider=lambda: {"amap": {"zh": "高德地图位置服务。", "en": ""}},
    )

    listing = asyncio.run(
        toolkit.tool_search(operation="list", category="mcp_amap", keyword="坐标")
    )

    assert 'MCP connector: amap, keyword: "坐标"' in listing
    assert "- [tool] mcp_amap_maps_geo:" in listing
    assert "mcp_amap_maps_text_search" not in listing


def test_list_keyword_dirty_input_degrades() -> None:
    """Empty junk keyword spellings degrade to the unfiltered listing."""

    toolkit = _build_toolkit()

    # An empty array keyword counts as absent → the full category listing.
    degraded = asyncio.run(
        toolkit.tool_search(operation="list", category="cron", keyword=[])
    )
    assert "cron_create_job" in degraded
    assert "cron_list_jobs" in degraded

    # Array spellings join into one keyword string and filter normally.
    joined = asyncio.run(
        toolkit.tool_search(
            operation="list", category="cron", keyword=["创建", "cron"]
        )
    )
    assert "cron_create_job" in joined


def test_get_schema_ignores_keyword() -> None:
    """keyword is list-only; get_schema serves the schema record unchanged."""

    toolkit = _build_toolkit()

    record = json.loads(
        str(
            asyncio.run(
                toolkit.tool_search(
                    operation="get_schema", tool="cron_create_job", keyword="创建"
                )
            )
        )
    )

    assert record["name"] == "cron_create_job"
    assert record["schema"]["required"] == ["name"]


def test_frozen_catalogue_intersects_live_registry() -> None:
    """The snapshot carries only multi-member categories; singles collapse.

    Intersection with the live registry still drops absent tools, but a
    category whose live membership falls to one tool no longer renders a
    category line — the lone member falls through to the flat ``[tool]``
    block instead (a one-tool category costs a list round trip that reveals
    a single name the prompt could state outright).
    """

    tools = [
        ToolInfo(name="cron_create_job", description="创建 cron 任务"),
        ToolInfo(name="cron_list_jobs", description="列出 cron 任务"),
        ToolInfo(name="read_pdf", description="读 PDF"),
    ]
    toolkit = _build_toolkit(tools)
    snapshot = toolkit.frozen_catalogue

    # cron keeps two live members → stays a category; documents has only
    # read_pdf live → collapses out of the category list.
    assert [category.path for category in snapshot.categories] == ["cron"]
    assert snapshot.categories[0].tools == ("cron_create_job", "cron_list_jobs")
    assert {entry.name for entry in snapshot.flat_entries} == {
        "cron_create_job",
        "cron_list_jobs",
        "read_pdf",
    }

    # refresh_snapshot() re-freezes against the (possibly grown) registry:
    # worktree gains both members → becomes a category; a lone terminal
    # member stays collapsed into the flat block.
    tools.append(ToolInfo(name="powershell", description="执行 PowerShell 命令"))
    tools.append(ToolInfo(name="enter_worktree", description="进入 worktree"))
    tools.append(ToolInfo(name="exit_worktree", description="退出 worktree"))
    toolkit.refresh_snapshot()
    paths = [
        category.path for category in toolkit.frozen_catalogue.categories
    ]
    assert "worktree" in paths
    assert "terminal" not in paths


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
