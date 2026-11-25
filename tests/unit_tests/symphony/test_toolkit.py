import asyncio
import json
from types import SimpleNamespace

import pytest

from openjiuwen.core.common.exception.errors import ValidationError
from openjiuwen.core.foundation.tool import ToolExposure
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.sys_operation.cwd import init_cwd

from jiuwenswarm.agents.harness.common.tool_progress_context import (
    bind_tool_progress,
    reset_tool_progress,
)
from jiuwenswarm.agents.harness.common.tools.symphony_toolkits import (
    SymphonyToolkit,
)
from jiuwenswarm.runtime.symphony.service import SwarmSymphonyService
from jiuwenswarm.runtime.symphony.llm import LLMConfig
from jiuwenswarm.runtime.symphony.planning_inputs import supports_planning_inputs


def _minimal_planned_graph(status="ready"):
    nodes = (
        {}
        if status == "no_plan"
        else {
            "writer": {"label": "Writer", "metadata": {"type": "skill"}},
            "reviewer": {"label": "Reviewer", "metadata": {"type": "skill"}},
        }
    )
    return {
        "graph": {
            "id": "plan-1",
            "type": "planned_graph",
            "directed": True,
            "metadata": {"status": status},
            "nodes": nodes,
            "edges": (
                []
                if status == "no_plan"
                else [
                    {
                        "source": "writer",
                        "target": "reviewer",
                        "relation": "can_feed",
                    }
                ]
            ),
        }
    }


@pytest.fixture(autouse=True)
def enabled_symphony_config(monkeypatch):
    def fake_load_symphony_config(config=None):
        raw = config.get("symphony") if isinstance(config, dict) else None
        enabled = raw.get("orchestration", {}).get("enabled", True) if isinstance(raw, dict) else True
        return SimpleNamespace(orchestration=SimpleNamespace(enabled=bool(enabled)))

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.symphony_toolkits.load_symphony_config",
        fake_load_symphony_config,
    )


@pytest.mark.asyncio
async def test_plan_calls_process_service_once_without_language():
    calls = []

    async def handler(query, **kwargs):
        calls.append({"query": query, **kwargs})
        return {
            "success": True,
            "planned_graph": _minimal_planned_graph(),
        }

    result = await SymphonyToolkit(SimpleNamespace(plan=handler)).plan(
        "use installed skills"
    )

    assert calls == [
        {
            "query": "use installed skills",
            "mode": None,
            "capability_type": "auto",
            "candidate_ids": None,
            "candidate_skill_ids": None,
            "progress": None,
        }
    ]
    assert result == {
        "success": True,
        "planned_graph": _minimal_planned_graph(),
    }


@pytest.mark.asyncio
async def test_plan_passes_mode_and_deduplicated_candidates():
    seen = {}

    async def handler(query, **kwargs):
        seen.update({"query": query, **kwargs})
        return {
            "success": True,
            "planned_graph": _minimal_planned_graph(),
        }

    await SymphonyToolkit(SimpleNamespace(plan=handler)).plan(
        "compose",
        mode="beam",
        candidate_skill_ids=["skill-a", "skill-a", "Skill B"],
    )

    assert seen == {
        "query": "compose",
        "mode": "beam",
        "capability_type": "auto",
        "candidate_ids": None,
        "candidate_skill_ids": ["skill-a", "Skill B"],
        "progress": None,
    }


@pytest.mark.asyncio
async def test_plan_passes_request_selected_model_config_to_service():
    selected = LLMConfig(model="selected-model")
    seen = {}

    async def handler(query, **kwargs):
        seen.update({"query": query, **kwargs})
        return {"success": True, "planned_graph": _minimal_planned_graph()}

    result = await SymphonyToolkit(
        SimpleNamespace(plan=handler),
        llm_config_provider=lambda: selected,
    ).plan("compose")

    assert result["success"] is True
    assert seen["llm_config"] is selected


@pytest.mark.asyncio
async def test_refresh_uses_host_default_without_session_model_override():
    selected = LLMConfig(model="selected-model")
    seen = {}

    async def handler(**kwargs):
        seen.update(kwargs)
        return {"success": True}

    result = await SymphonyToolkit(
        SimpleNamespace(refresh_graph=handler),
        llm_config_provider=lambda: selected,
    ).refresh_graph()

    assert result["success"] is True
    assert "llm_config" not in seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "method_name"),
    [("plan", "plan")],
)
async def test_request_model_resolution_failure_is_a_structured_tool_result(
    operation,
    method_name,
):
    async def handler(*args, **kwargs):
        del args, kwargs
        raise AssertionError("service must not run with an unresolved request model")

    toolkit = SymphonyToolkit(
        SimpleNamespace(**{method_name: handler}),
        llm_config_provider=lambda: (_ for _ in ()).throw(ValueError("invalid")),
    )

    result = (
        await toolkit.plan("compose")
        if operation == "plan"
        else await toolkit.refresh_graph()
    )

    assert result == {
        "success": False,
        "reason": "llm_config_unavailable",
        "retryable": False,
        "operation": operation,
        "detail": (
            f"symphony.{operation}: request-selected model configuration "
            "is unavailable (ValueError)"
        ),
    }


@pytest.mark.asyncio
async def test_compose_tool_accepts_json_encoded_candidate_skill_ids():
    calls = []

    async def handler(query, **kwargs):
        calls.append({"query": query, **kwargs})
        return {"success": True}

    compose_tool = SymphonyToolkit(SimpleNamespace(plan=handler)).get_tools()[-1]

    await compose_tool.invoke(
        {
            "query": "plan a trip",
            "mode": "fast",
            "type": "skill",
            "candidate_skill_ids": '["openJiuwen-DeepSearch"]',
        }
    )

    assert calls == [
        {
            "query": "plan a trip",
            "mode": "fast",
            "capability_type": "skill",
            "candidate_ids": None,
            "candidate_skill_ids": ["openJiuwen-DeepSearch"],
            "progress": None,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate_skill_ids",
    ["not-json", '{"skill": "openJiuwen-DeepSearch"}'],
)
async def test_compose_tool_rejects_non_array_candidate_strings(candidate_skill_ids):
    async def handler(query, **kwargs):
        del query, kwargs
        raise AssertionError("service must not be called for invalid inputs")

    compose_tool = SymphonyToolkit(SimpleNamespace(plan=handler)).get_tools()[-1]

    with pytest.raises(ValidationError, match="candidate_skill_ids"):
        await compose_tool.invoke(
            {
                "query": "plan a trip",
                "type": "skill",
                "candidate_skill_ids": candidate_skill_ids,
            }
        )


@pytest.mark.asyncio
@pytest.mark.skipif(not supports_planning_inputs(), reason="Requires Core planning inputs support")
async def test_compose_reports_unavailable_workspace_files_by_name_without_blocking(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    facts = workspace / "facts.md"
    facts.write_text("Product data", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("Outside the project", encoding="utf-8")
    escape = workspace / "escape.md"
    try:
        escape.symlink_to(outside)
    except (NotImplementedError, OSError):
        escape = outside
    init_cwd(str(workspace), workspace=str(workspace))
    calls = []

    async def handler(query, **kwargs):
        calls.append(kwargs["inputs"])
        graph = _minimal_planned_graph()
        graph["graph"]["metadata"]["plan_kind"] = "expert_tasks"
        return {"success": True, "planned_graph": graph}

    compose = SymphonyToolkit(SimpleNamespace(plan=handler)).get_tools()[-1]
    result = await compose.invoke({
        "query": "Plan a launch",
        "type": "expert",
        "inputs": json.dumps([
            {"name": "facts", "kind": "file", "value": facts.name},
            {"name": "price", "kind": "file", "value": "missing.md"},
            {"name": "external", "kind": "file", "value": str(escape)},
        ]),
    })

    assert calls == [[{"name": "facts", "kind": "file", "value": str(facts.resolve())}]]
    graph = result["planned_graph"]["graph"]
    assert graph["metadata"]["status"] == "ready"
    assert graph["nodes"]
    assert [hint["name"] for hint in graph["metadata"]["missing_inputs"]] == ["price", "external"]
    assert str(outside) not in json.dumps(result)


@pytest.mark.asyncio
async def test_compose_tool_defaults_to_auto_without_a_capability_type():
    calls = []

    async def plan(query, **kwargs):
        calls.append(kwargs["capability_type"])
        return {"success": True, "execution_route": {
            "strategy": "direct_answer", "requires_planning": False,
        }}

    compose_tool = SymphonyToolkit(SimpleNamespace(plan=plan)).get_tools()[-1]
    result = await compose_tool.invoke({"query": "Answer from known facts"})
    assert calls == ["auto"]
    assert result == {"success": True}


@pytest.mark.asyncio
async def test_plan_passes_current_progress_callback_directly_to_service():
    events = []

    async def handler(query, *, progress=None, **kwargs):
        del query, kwargs
        callback = progress
        await callback({"event": "started", "graph": {"nodes": [], "edges": []}})
        return {
            "success": True,
            "planned_graph": _minimal_planned_graph(),
        }

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
async def test_plan_omits_route_and_node_material_bindings_from_tool_result():
    planned_graph = _minimal_planned_graph()
    nodes = planned_graph["graph"]["nodes"]
    nodes["writer"]["metadata"]["external_inputs"] = ["brief"]
    nodes["reviewer"]["metadata"]["external_inputs"] = []

    async def handler(*args, **kwargs):
        del args, kwargs
        return {
            "success": True,
            "execution_route": {"strategy": "capability_workflow", "requires_planning": True},
            "planned_graph": planned_graph,
        }

    result = await SymphonyToolkit(SimpleNamespace(plan=handler)).plan(
        "compose", mode="beam"
    )

    assert result == {
        "success": True,
        "planned_graph": _minimal_planned_graph(),
    }
    assert nodes["writer"]["metadata"]["external_inputs"] == ["brief"]
    assert nodes["reviewer"]["metadata"]["external_inputs"] == []


@pytest.mark.asyncio
async def test_plan_keeps_compact_graph_build_only_when_rebuilt():
    async def handler(*args, **kwargs):
        del args, kwargs
        return {
            "success": True,
            "planned_graph": _minimal_planned_graph(),
            "graph_build": {"success": True, "rebuilt": True, "edge_count": 3},
        }

    result = await SymphonyToolkit(SimpleNamespace(plan=handler)).plan("compose")

    assert result["graph_build"] == {
        "success": True,
        "rebuilt": True,
        "edge_count": 3,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("planned_graph", [None, [], "not-a-graph"])
async def test_plan_rejects_success_payload_without_dict_planned_graph(planned_graph):
    async def handler(*args, **kwargs):
        del args, kwargs
        return {"success": True, "planned_graph": planned_graph}

    result = await SymphonyToolkit(SimpleNamespace(plan=handler)).plan("compose")

    assert result == {
        "success": False,
        "detail": "Symphony orchestration returned no planned_graph",
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
async def test_plan_preserves_graph_preparing_status():
    async def handler(*args, **kwargs):
        del args, kwargs
        return {
            "success": False,
            "reason": "graph_preparing",
            "retryable": False,
            "build_status": "running",
            "operation": "plan",
            "detail": "技能总谱正在构建。",
        }

    result = await SymphonyToolkit(SimpleNamespace(plan=handler)).plan("compose")

    assert result == {
        "success": False,
        "reason": "graph_preparing",
        "retryable": False,
        "build_status": "running",
        "operation": "plan",
        "detail": "技能总谱正在构建。",
    }


@pytest.mark.asyncio
async def test_compose_timeout_is_compact_non_retryable_terminal_payload(monkeypatch):
    async def handler(*args, **kwargs):
        del args, kwargs
        await asyncio.Event().wait()

    timeouts: list[float] = []
    monkeypatch.setattr(
        SymphonyToolkit,
        "_resolve_timeout_s",
        staticmethod(lambda default_s=1800.0: timeouts.append(default_s) or 0.01),
    )

    result = await SymphonyToolkit(SimpleNamespace(plan=handler)).plan("compose")

    assert timeouts == [3300.0]
    assert result == {
        "success": False,
        "reason": "graph_build_timeout",
        "timed_out": True,
        "retryable": False,
        "operation": "plan",
        "timeout_s": 0.01,
        "detail": "symphony.plan: timeout after 0.01s",
    }


@pytest.mark.asyncio
async def test_plan_handler_timeout_is_an_ordinary_service_failure():
    async def handler(*args, **kwargs):
        del args, kwargs
        raise asyncio.TimeoutError("upstream timeout")

    result = await SymphonyToolkit(SimpleNamespace(plan=handler)).plan("compose")

    assert result["success"] is False
    assert result.get("reason") != "graph_build_timeout"
    assert result["detail"] == "symphony.plan: upstream timeout"


@pytest.mark.asyncio
async def test_external_plan_task_cancellation_propagates():
    handler_entered = asyncio.Event()
    handler_cancelled = asyncio.Event()

    async def handler(*args, **kwargs):
        del args, kwargs
        handler_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            handler_cancelled.set()

    task = asyncio.create_task(
        SymphonyToolkit(SimpleNamespace(plan=handler)).plan("compose")
    )
    await handler_entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert handler_cancelled.is_set()


@pytest.mark.asyncio
async def test_refresh_timeout_is_non_retryable_terminal_payload(monkeypatch):
    async def handler(*args, **kwargs):
        del args, kwargs
        await asyncio.Event().wait()

    timeouts: list[float] = []
    monkeypatch.setattr(
        SymphonyToolkit,
        "_resolve_timeout_s",
        staticmethod(lambda default_s=1800.0: timeouts.append(default_s) or 0.01),
    )

    result = await SymphonyToolkit(SimpleNamespace(refresh_graph=handler)).refresh_graph()

    assert timeouts == [1800.0]
    assert result == {
        "success": False,
        "reason": "graph_build_timeout",
        "timed_out": True,
        "retryable": False,
        "operation": "refresh_graph",
        "timeout_s": 0.01,
        "detail": "symphony.refresh_graph: timeout after 0.01s",
    }


@pytest.mark.asyncio
async def test_read_graph_timeout_remains_an_ordinary_failure(monkeypatch):
    async def handler(*args, **kwargs):
        del args, kwargs
        await asyncio.Event().wait()

    monkeypatch.setattr(
        SymphonyToolkit,
        "_resolve_timeout_s",
        staticmethod(lambda default_s=1800.0: 0.01),
    )

    result = await SymphonyToolkit(SimpleNamespace(graph_status=handler)).graph_status()

    assert result == {
        "success": False,
        "detail": "symphony.graph_status: timeout after 0.01s",
    }
    assert "reason" not in result
    assert "timed_out" not in result


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
    from jiuwenswarm.common.symphony_config import symphony_config_from_dict

    config = symphony_config_from_dict({
        "capability": {"sources": [{"type": "skill", "root": tmp_path / "skills"}]},
        "storage": {"artifact_root": str(tmp_path / "runtime")},
    })
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
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.service_build_graph",
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


@pytest.mark.asyncio
async def test_plan_timeout_classifies_cancelled_graph_build_as_terminal_timeout(
    monkeypatch,
    tmp_path,
):
    from jiuwenswarm.common.symphony_config import symphony_config_from_dict
    config = symphony_config_from_dict({"capability": {"sources": [{"type": "skill", "root": str(tmp_path / "skills")}]},
        "storage": {"artifact_root": str(tmp_path / "runtime")}, "orchestration": {"enabled": True}})
    build_entered = asyncio.Event()

    async def blocking_build(*args, **kwargs):
        del args, kwargs
        build_entered.set()
        await asyncio.Event().wait()

    async def successful_probe(_config):
        return None

    async def stale_graph_status():
        return {"success": True, "exists": True, "stale": True}

    async def passing_model_probe(_llm_config):
        return None

    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    # The build path probes the primary model before building; keep it passing so
    # the timeout under test is the one raised by the blocked graph build.
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.probe_model_connection",
        passing_model_probe,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.probe_model_connection",
        successful_probe,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.service_build_graph",
        blocking_build,
    )
    monkeypatch.setattr(
        SymphonyToolkit,
        "_resolve_timeout_s",
        staticmethod(lambda default_s=1800.0: 0.01),
    )
    service = SwarmSymphonyService()
    service.graph_status = stale_graph_status

    result = await SymphonyToolkit(service).plan("compose", type="skill")

    assert build_entered.is_set()
    assert result["reason"] == "graph_build_timeout"
    assert result["operation"] == "plan"
    assert result["timed_out"] is True
    assert result["retryable"] is False
    assert result["timeout_s"] == 0.01
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
    assert properties["type"]["enum"] == ["auto", "skill", "expert"]
    assert properties["type"]["default"] == "auto"
    assert compose_tool.card.input_params["required"] == ["query"]
    assert properties["candidate_ids"]["type"] == "array"
    assert properties["candidate_ids"]["items"] == {"type": "string"}
    assert "shortlisted" in properties["candidate_ids"]["description"]
    assert "limited to these candidates" in properties["candidate_ids"]["description"]
    assert "omit mode to use the session setting" in properties["mode"]["description"]
    assert "bidirectional" not in properties["mode"]["description"]
    assert "Omit type for automatic selection" in compose_tool.card.description
    assert "planned_graph" in compose_tool.card.description
    assert "execution_route" not in compose_tool.card.description
    assert "capability_search" not in compose_tool.card.description
    assert "skill_branch_explore" not in compose_tool.card.description
    assert all("Symphony" not in tool.card.description for tool in tools)
    assert all(
        "Symphony" not in property_schema.get("description", "")
        for property_schema in properties.values()
    )
    assert "graph_build_timeout" in tools[1].card.description
    assert "manual_graph_build" in tools[1].card.description


def test_compose_graph_is_direct_with_progressive_loading():
    manager = AbilityManager()
    manager.set_tool_exposure_policy(progressive_tool_enabled=True)
    manager.add([tool.card for tool in SymphonyToolkit().get_tools()])

    assert manager.get("symphony_compose_graph").exposure is ToolExposure.DIRECT
    assert manager.get("symphony_read_graph").exposure is ToolExposure.DEFERRED
    assert manager.get("symphony_refresh_graph").exposure is ToolExposure.DEFERRED


def test_disabled_config_hides_tools():
    assert SymphonyToolkit().get_tools({"symphony": {"orchestration": {"enabled": False}}}) == []
