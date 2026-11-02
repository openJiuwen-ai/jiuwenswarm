import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.tool_progress_context import (
    bind_tool_progress,
    reset_tool_progress,
)
from jiuwenswarm.agents.harness.common.tools.symphony_toolkits import (
    SymphonyToolkit,
)
from jiuwenswarm.symphony.service import SwarmSymphonyService


@pytest.fixture(autouse=True)
def enabled_symphony_config(monkeypatch):
    def fake_load_symphony_config(config=None):
        raw = config.get("symphony") if isinstance(config, dict) else None
        enabled = raw.get("enabled", True) if isinstance(raw, dict) else True
        return SimpleNamespace(enabled=bool(enabled))

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.symphony_toolkits.load_symphony_config",
        fake_load_symphony_config,
    )


@pytest.mark.asyncio
async def test_plan_calls_process_service_once_without_language():
    calls = []

    async def handler(query, **kwargs):
        calls.append({"query": query, **kwargs})
        return {"success": True, "content": "## Plan", "direct_display": True}

    result = await SymphonyToolkit(SimpleNamespace(plan=handler)).plan(
        "use installed skills"
    )

    assert calls == [
        {
            "query": "use installed skills",
            "mode": None,
            "candidate_skill_ids": None,
            "progress": None,
        }
    ]
    assert result == {
        "success": True,
        "content": "## Plan",
        "direct_display": True,
        "continue_after_display": True,
        "followup_action": "external_skill_discovery",
    }


@pytest.mark.asyncio
async def test_plan_passes_mode_and_deduplicated_candidates():
    seen = {}

    async def handler(query, **kwargs):
        seen.update({"query": query, **kwargs})
        return {"success": True}

    await SymphonyToolkit(SimpleNamespace(plan=handler)).plan(
        "compose",
        mode="beam",
        candidate_skill_ids=["skill-a", "skill-a", "Skill B"],
    )

    assert seen == {
        "query": "compose",
        "mode": "beam",
        "candidate_skill_ids": ["skill-a", "Skill B"],
        "progress": None,
    }


@pytest.mark.asyncio
async def test_plan_passes_current_progress_callback_directly_to_service():
    events = []

    async def handler(query, *, progress=None, **kwargs):
        del query, kwargs
        callback = progress
        await callback({"event": "started", "graph": {"nodes": [], "edges": []}})
        return {"success": True}

    async def progress(event):
        events.append(event)

    token = bind_tool_progress(progress)
    try:
        await SymphonyToolkit(SimpleNamespace(plan=handler)).plan(
            "compose", mode="beam"
        )
    finally:
        reset_tool_progress(token)

    assert events == [{"event": "started", "graph": {"nodes": [], "edges": []}}]


@pytest.mark.asyncio
async def test_plan_returns_compact_plan_and_beam_graph():
    async def handler(*args, **kwargs):
        del args, kwargs
        return {
            "success": True,
            "content": "## Beam Plan",
            "result": {
                "beam_search": {
                    "language": "cn",
                    "round_index": 2,
                    "graph": {
                        "nodes": [
                            {
                                "id": "skill-a",
                                "label": "Skill A",
                                "status": "final",
                                "seed": True,
                                "unused": "drop",
                            }
                        ],
                        "edges": [],
                    },
                },
                "recommended_plans": [
                    {
                        "title": "Plan",
                        "status": "ready",
                        "steps": [{"skill_id": "skill-a", "name": "Skill A"}],
                        "can_feed_edges": [],
                        "missing_inputs": [],
                    }
                ],
            },
        }

    result = await SymphonyToolkit(SimpleNamespace(plan=handler)).plan(
        "compose", mode="beam"
    )

    assert result["beam_search"] == {
        "language": "cn",
        "round_index": 2,
        "graph": {
            "nodes": [
                {
                    "id": "skill-a",
                    "label": "Skill A",
                    "status": "final",
                    "seed": True,
                }
            ],
            "edges": [],
        },
    }
    assert result["plan"]["steps"] == [
        {"step": 1, "skill_id": "skill-a", "name": "Skill A"}
    ]
    assert "result" not in result


@pytest.mark.asyncio
async def test_plan_preserves_dynamic_graph_metadata():
    async def handler(*args, **kwargs):
        del args, kwargs
        return {
            "success": True,
            "plan_id": "plan-session-1",
            "dynamic_graph_enabled": True,
            "result": {"recommended_plans": []},
        }

    result = await SymphonyToolkit(SimpleNamespace(plan=handler)).plan("compose")

    assert result["plan_id"] == "plan-session-1"
    assert result["dynamic_graph_enabled"] is True


def test_toolkit_compacts_inferred_edge_provenance():
    edge = SymphonyToolkit._compact_can_feed_edge(
        {
            "source_id": "skill-a",
            "target_id": "skill-b",
            "confidence": None,
            "method": "fast_llm_inferred",
            "reason": "LLM connected retrieved candidates.",
            "port_mappings": [],
        }
    )

    assert edge == {
        "source_id": "skill-a",
        "target_id": "skill-b",
        "method": "fast_llm_inferred",
        "reason": "LLM connected retrieved candidates.",
    }


@pytest.mark.asyncio
async def test_plan_reports_service_failure():
    async def handler(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("service unavailable")

    result = await SymphonyToolkit(SimpleNamespace(plan=handler)).plan("compose")

    assert result["success"] is False
    assert "service unavailable" in result["detail"]


@pytest.mark.asyncio
async def test_status_and_refresh_remain_explicit_tools():
    async def graph_status():
        return {"success": True, "exists": True}

    async def refresh_graph(*, progress=None):
        assert progress is None
        return {"success": True, "rebuilt": True}

    toolkit = SymphonyToolkit(
        SimpleNamespace(graph_status=graph_status, refresh_graph=refresh_graph)
    )
    assert (await toolkit.graph_status())["exists"] is True
    assert (await toolkit.refresh_graph())["rebuilt"] is True


@pytest.mark.asyncio
async def test_refresh_timeout_aborts_blocked_progress_without_leaking_worker(
    monkeypatch,
    tmp_path,
):
    config = SimpleNamespace(
        paths=SimpleNamespace(
            skills_root=tmp_path / "skills",
            graph_dir=tmp_path / "graph",
        )
    )
    callback_entered = asyncio.Event()
    callback_cancelled = asyncio.Event()

    async def blocking_progress(_event):
        callback_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            callback_cancelled.set()

    async def fake_build_graph(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(to_dict=lambda: {"success": True, "version": "v1"})

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.service_build_graph",
        fake_build_graph,
    )
    monkeypatch.setattr(
        SymphonyToolkit,
        "_resolve_timeout_s",
        staticmethod(lambda default_s=1800.0: 0.05),
    )
    service = SwarmSymphonyService()
    toolkit = SymphonyToolkit(service)
    token = bind_tool_progress(blocking_progress)
    try:
        task = asyncio.create_task(toolkit.refresh_graph())
        await callback_entered.wait()
        result = await asyncio.wait_for(task, timeout=0.5)
    finally:
        reset_tool_progress(token)

    assert result["success"] is False
    assert "timeout" in result["detail"]
    assert callback_cancelled.is_set()
    assert service._active_build_task is None
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "symphony-build-progress"
    ]


def test_get_tools_exposes_only_graph_named_contracts():
    tools = SymphonyToolkit().get_tools()
    assert [tool.card.name for tool in tools] == [
        "symphony_read_graph",
        "symphony_refresh_graph",
        "symphony_compose_graph",
    ]
    assert all("score" not in tool.card.name for tool in tools)

    compose_tool = tools[-1]
    properties = compose_tool.card.input_params["properties"]

    assert properties["mode"]["enum"] == ["fast", "beam"]
    assert "language" not in properties
    assert "most relevant" in properties["candidate_skill_ids"]["description"]
    assert "fast is the default" in properties["mode"]["description"]


def test_disabled_config_hides_tools():
    assert SymphonyToolkit().get_tools({"symphony": {"enabled": False}}) == []
