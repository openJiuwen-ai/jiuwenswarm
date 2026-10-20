"""真实专家规划工具、依赖拦截和热绑定生命周期。"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.single_agent.rail.base import ModelCallInputs, ToolCallInputs
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.common.tool_display import build_tool_display_name
from jiuwenswarm.server.runtime.expert import expert_store
from jiuwenswarm.symphony import expert_team


def _experts(*ids):
    return {
        key: expert_team.LocalExpert(
            id=key, name=f"专家{key}", description=f"职责{key}", template=object(),
        )
        for key in ids
    }


def _make_expert(root: Path, expert_id: str, name: str, *, avatar: bool = False) -> Path:
    package = root / expert_id
    (package / "persona").mkdir(parents=True)
    (package / "persona" / "AGENT.md").write_text(f"你是{name}。", encoding="utf-8")
    manifest = {
        "packageType": "agent_template",
        "agentCard": {"id": expert_id, "name": name, "description": f"{name}的职责"},
        "persona": {"dir": "persona"},
    }
    if avatar:
        (package / "avatar.png").write_bytes(b"png")
        manifest["metadata"] = {"avatar": "avatar.png"}
    (package / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return package


@pytest.mark.asyncio
async def test_scan_experts_merges_local_and_cached_packages_with_correct_roots(tmp_path, monkeypatch):
    local_root = tmp_path / "experts"
    cache_root = tmp_path / "experts_cache"
    local = _make_expert(local_root, "shared", "本地版本", avatar=True)
    _make_expert(cache_root, "shared", "缓存版本", avatar=True)
    cached = _make_expert(cache_root, "downloaded", "缓存专家", avatar=True)
    invalid_local = _make_expert(local_root, "fallback", "损坏本地专家")
    (invalid_local / "manifest.json").write_text("{}", encoding="utf-8")
    fallback = _make_expert(cache_root, "fallback", "有效缓存专家")
    group = local_root / "group"
    group.mkdir(parents=True)
    (group / "manifest.json").write_text('{"package_type":"agent_group"}', encoding="utf-8")
    monkeypatch.setattr(expert_team, "get_agent_experts_dir", lambda: local_root)
    monkeypatch.setattr(expert_team, "get_expert_cache_dir", lambda: cache_root)
    monkeypatch.setattr(expert_store, "get_expert_cache_dir", lambda: cache_root)

    found = await expert_team.scan_local_experts()

    assert set(found) == {"shared", "downloaded", "fallback"}
    assert (found["shared"].name, found["shared"].source, found["shared"].package_dir) == (
        "本地版本", "local", local,
    )
    assert (found["downloaded"].source, found["downloaded"].package_dir, found["downloaded"].avatar) == (
        "cache", cached, "avatar.png",
    )
    assert (found["fallback"].source, found["fallback"].package_dir) == ("cache", fallback)
    agent = FakeAgent()
    agent.deep_config = SimpleNamespace(
        language="cn", workspace=None, tools=[], mcps=[], max_iterations=8, restrict_to_work_dir=False,
    )
    config = expert_team._subagent_config(agent, found["downloaded"], object())
    assert "你是缓存专家" in config.system_prompt


@pytest.mark.asyncio
async def test_scan_experts_keeps_local_candidates_when_cache_directory_unreadable(tmp_path, monkeypatch):
    local_root = tmp_path / "experts"
    _make_expert(local_root, "local-agent", "本地专家")
    monkeypatch.setattr(expert_team, "get_agent_experts_dir", lambda: local_root)
    monkeypatch.setattr(expert_team, "get_expert_cache_dir", lambda: tmp_path / "experts_cache")

    async def unreadable(_source):
        raise PermissionError("cache unavailable")

    monkeypatch.setattr(expert_team.CachedExpertPackageSource, "list", unreadable)
    assert list(await expert_team.scan_local_experts()) == ["local-agent"]


def test_expert_config_has_isolated_skill_rail_and_owner_without_parent_skill_tools(monkeypatch):
    parent_tool = SimpleNamespace(card=ToolCard(id="general_parent", name="general", description="通用工具"))
    scoped_names = [
        "skill_tool", "list_skill", "skill_index", "skill_index_build", "symphony_read_graph",
        "symphony_compose_graph", "symphony_plan_experts", "task_tool", "sessions_spawn",
    ]
    parent_tools = [parent_tool, *(ToolCard(name=name, description=name) for name in scoped_names)]
    parent_rail = SkillUseRail(skills_dir=[])
    monkeypatch.setattr(expert_team, "resolve_agent_template_parts", lambda *_args: SimpleNamespace(
        prompt_sections=[], tools=[], mcps=[], rails=[parent_rail], skills=[],
    ))
    agent = SimpleNamespace(
        card=SimpleNamespace(id="same-main-id"),
        deep_config=SimpleNamespace(
            language="cn", workspace=None, tools=parent_tools, mcps=[], max_iterations=8,
            restrict_to_work_dir=False,
        ),
    )
    first, second = [expert_team._subagent_config(agent, _experts("a")["a"], object()) for _ in range(2)]
    assert first.agent_card.name == second.agent_card.name == expert_team._subagent_type("a")
    assert first.agent_card.id != second.agent_card.id
    assert first.factory_kwargs["tool_owner_id"] == first.agent_card.id
    assert [tool.name for tool in first.tools] == ["general"]
    assert first.tools[0].id == parent_tool.card.id == "general_parent"
    assert len(first.rails) == 1 and isinstance(first.rails[0], expert_team.DynamicExpertSkillRail)
    assert first.rails[0] is not second.rails[0] and first.rails[0] is not parent_rail
    assert first.skills is None
    assert first.enable_task_loop is False
    assert "不得再次编排专家" in first.system_prompt


def _graph(edges=(), expert_ids=("a", "b")):
    nodes = {
        "a": {"label": "专家a", "description": "职责a", "task": "整理资料"},
        "b": {"label": "专家b", "description": "职责b", "task": "审阅资料"},
    }
    return {
        "id": "plan-1", "type": "planned_graph", "directed": True,
        "metadata": {"status": "ready", "missing_inputs": [], "leader": {"id": "xiaoyiwork"}},
        "nodes": {expert_id: nodes[expert_id] for expert_id in expert_ids},
        "edges": [{"source": source, "target": target, "relation": "can_feed"} for source, target in edges],
    }


class FakeAgent:
    def __init__(self):
        self.card = SimpleNamespace(id="main")
        self.ability_manager = self
        self.deep_config = SimpleNamespace(model=object())
        self.system_prompt_builder = SimpleNamespace(add_section=lambda _section: None, remove_section=lambda _name: None)

    def get(self, name):
        return object() if name == "task_tool" else None

    async def list_tool_info(self, names):
        return [SimpleNamespace(name=name, description=name, parameters={}) for name in names]

    def load_state(self, _session):
        return SimpleNamespace(plan_mode=SimpleNamespace(mode="agent"))


@pytest.mark.asyncio
async def test_planning_omits_skill_names_but_preserves_executable_template(tmp_path, monkeypatch):
    local_root = tmp_path / "experts"
    cache_root = tmp_path / "experts_cache"
    package = _make_expert(cache_root, "writer", "写作专家")
    skill = package / "skills" / "private-writing-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: private-writing-skill\ndescription: 专家写作技能\n---\n# 技能正文",
        encoding="utf-8",
    )
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"] = [{"dir": "skills/private-writing-skill"}]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(expert_team, "get_agent_experts_dir", lambda: local_root)
    monkeypatch.setattr(expert_team, "get_expert_cache_dir", lambda: cache_root)
    monkeypatch.setattr(expert_store, "get_expert_cache_dir", lambda: cache_root)
    loaded = []

    def build_config(_agent, expert, _model):
        loaded.append(expert)
        return object()

    monkeypatch.setattr(expert_team, "_subagent_config", build_config)
    model = SimpleNamespace(invoke=AsyncMock(return_value={"content": '{"status":"no_plan"}'}))
    round_state = expert_team.DynamicExpertRound(FakeAgent(), "请专家写报告", model)
    result = await round_state.compose("请专家写报告")

    assert result == {"success": True, "status": "no_plan"}
    model.invoke.assert_awaited_once()
    payload = json.loads(model.invoke.await_args.kwargs["messages"][1]["content"])
    assert payload["experts"] == [{
        "id": "writer", "name": "写作专家", "description": "写作专家的职责", "skills": [],
    }]
    assert "private-writing-skill" not in json.dumps(payload)
    assert Path(loaded[0].template.skills[0].dir).resolve() == skill.resolve()
    assert loaded[0].package_dir == package.resolve()


def _session():
    return SimpleNamespace(get_session_id=lambda: "session-1")


def _tool_ctx(subagent_type, call_id, task="执行节点"):
    return SimpleNamespace(
        session=_session(),
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id=call_id),
            tool_name="task_tool",
            tool_args=json.dumps({"subagent_type": subagent_type, "task_description": task}),
        ),
        extra={},
    )


async def _ready_round(monkeypatch, edges=(), expert_ids=("a", "b")):
    agent = FakeAgent()
    model = object()

    async def scan():
        return _experts(*expert_ids)

    async def core_plan(_query, _candidates, passed_model):
        assert passed_model is model
        return {"status": "ready", "graph": _graph(edges, expert_ids)}

    async def bind(_agent, parts):
        assert parts.subagents == list(expert_ids)
        return ["bound"]

    monkeypatch.setattr(expert_team, "scan_local_experts", scan)
    monkeypatch.setattr(expert_team, "plan_experts", core_plan)
    monkeypatch.setattr(expert_team, "_subagent_config", lambda _agent, expert, _model: expert.id)
    monkeypatch.setattr(expert_team, "apply_extension_hot", bind)
    round_state = expert_team.DynamicExpertRound(agent, "用户任务", model)
    return round_state, agent


@pytest.mark.asyncio
async def test_expert_plan_tool_is_unavailable_without_an_ordinary_active_round():
    tool = expert_team.ExpertPlanTool(lambda _session_id: None)
    assert await tool.invoke({"query": "请专家处理"}) == {
        "success": False, "status": "error", "reason": "expert_plan_unavailable",
    }


@pytest.mark.asyncio
async def test_model_tool_menu_refreshes_only_plan_and_task_tool(monkeypatch):
    round_state, agent = await _ready_round(monkeypatch)
    rail = expert_team.DynamicExpertRail(lambda _sid: round_state)
    rail.init(agent)
    ctx = SimpleNamespace(session=_session(), inputs=ModelCallInputs(tools=[
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="symphony_compose_graph"),
    ]))

    await rail.before_model_call(ctx)
    assert [tool.name for tool in ctx.inputs.tools] == [
        "read_file", "symphony_compose_graph", expert_team.PLAN_TOOL_NAME,
    ]
    await round_state.compose("用户任务")
    await rail.before_model_call(ctx)
    assert [tool.name for tool in ctx.inputs.tools] == ["read_file", "task_tool"]

    fixed_session = expert_team.DynamicExpertRail(lambda _sid: None)
    fixed_session.init(agent)
    ctx.inputs.tools.append(SimpleNamespace(name=expert_team.PLAN_TOOL_NAME))
    await fixed_session.before_model_call(ctx)
    assert [tool.name for tool in ctx.inputs.tools] == ["read_file", "task_tool"]
    assert build_tool_display_name(expert_team.PLAN_TOOL_NAME, {"query": "完成报告"}).startswith("规划本轮专家协作")


@pytest.mark.asyncio
async def test_independent_nodes_can_run_together_but_failure_blocks_successors(monkeypatch):
    round_state, agent = await _ready_round(monkeypatch)
    await round_state.compose("用户任务")
    rail = expert_team.DynamicExpertRail(lambda _sid: round_state)
    rail.init(agent)
    a = _tool_ctx(expert_team._subagent_type("a"), "call-a")
    b = _tool_ctx(expert_team._subagent_type("b"), "call-b")
    await asyncio.gather(rail.before_tool_call(a), rail.before_tool_call(b))
    assert not a.extra.get("_skip_tool") and not b.extra.get("_skip_tool")
    await rail.on_tool_exception(a)
    b.inputs.tool_result = ToolOutput(success=True, data={"output": "完成"})
    await rail.after_tool_call(b)
    assert round_state._states == {"a": "failed", "b": "completed"}

    dependent, _agent = await _ready_round(monkeypatch, [("a", "b")])
    await dependent.compose("用户任务")
    gate = expert_team.DynamicExpertRail(lambda _sid: dependent)
    gate.init(agent)
    failed_a = _tool_ctx(expert_team._subagent_type("a"), "failed-a")
    await gate.before_tool_call(failed_a)
    await gate.on_tool_exception(failed_a)
    blocked_b = _tool_ctx(expert_team._subagent_type("b"), "blocked-b")
    await gate.before_tool_call(blocked_b)
    assert "失败" in blocked_b.inputs.tool_msg.content


@pytest.mark.asyncio
async def test_interrupted_expert_resumes_same_call_without_rewriting_interactive_input(monkeypatch):
    round_state, agent = await _ready_round(monkeypatch, [("a", "b")])
    await round_state.compose("用户任务")
    rail = expert_team.DynamicExpertRail(lambda _sid: round_state)
    rail.init(agent)
    call = _tool_ctx(expert_team._subagent_type("a"), "call-a")
    await rail.before_tool_call(call)
    original_args = json.loads(call.inputs.tool_args)
    call.inputs.tool_result = {"result_type": "interrupt", "interrupt_ids": ["approval"]}
    await rail.after_tool_call(call)
    assert round_state._states["a"] == "interrupted"

    early = _tool_ctx(expert_team._subagent_type("b"), "early-b")
    await rail.before_tool_call(early)
    assert "尚未完成" in early.inputs.tool_msg.content
    answer = InteractiveInput()
    answer.update("approval", {"confirmed": True})
    wrong = _tool_ctx(expert_team._subagent_type("a"), "different-call")
    wrong.inputs.tool_args = {**original_args, "query": answer}
    await rail.before_tool_call(wrong)
    assert wrong.extra["_skip_tool"] is True

    resumed = _tool_ctx(expert_team._subagent_type("a"), "call-a")
    resumed.inputs.tool_args = {**original_args, "query": answer}
    await rail.before_tool_call(resumed)
    assert not resumed.extra.get("_skip_tool")
    assert resumed.inputs.tool_args["query"] is answer
    assert resumed.inputs.tool_args["task_description"] == original_args["task_description"]
    resumed.inputs.tool_result = ToolOutput(success=True, data={"output": "恢复后的专家结果"})
    await rail.after_tool_call(resumed)
    assert round_state._states["a"] == "completed" and round_state._interrupted == {}

    successor = _tool_ctx(expert_team._subagent_type("b"), "call-b")
    await rail.before_tool_call(successor)
    assert "恢复后的专家结果" in json.loads(successor.inputs.tool_args)["query"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [("no_plan", "no_plan"), ("unexpected", "error")])
async def test_no_plan_and_invalid_status_do_not_bind(monkeypatch, status, expected):
    round_state, agent = await _ready_round(monkeypatch)
    bind = AsyncMock()
    monkeypatch.setattr(expert_team, "plan_experts", AsyncMock(return_value={"status": status}))
    monkeypatch.setattr(expert_team, "apply_extension_hot", bind)
    result = await round_state.compose("用户任务")
    assert result["status"] == expected
    assert result["success"] is (status == "no_plan")
    assert round_state.plan is None

    rail = expert_team.DynamicExpertRail(lambda _sid: round_state)
    rail.init(agent)
    ctx = SimpleNamespace(session=_session(), inputs=ModelCallInputs(tools=[
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="symphony_compose_graph"),
        SimpleNamespace(name=expert_team.PLAN_TOOL_NAME),
    ]))
    await rail.before_model_call(ctx)
    assert [tool.name for tool in ctx.inputs.tools] == ["read_file", "symphony_compose_graph"]
    bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_available_experts_skips_core_model_and_binding(monkeypatch):
    async def scan():
        return {}

    monkeypatch.setattr(expert_team, "scan_local_experts", scan)
    monkeypatch.setattr(expert_team, "_subagent_config", lambda _agent, expert, _model: expert.id)
    round_state = expert_team.DynamicExpertRound(FakeAgent(), "普通任务", object())
    assert await round_state.compose("普通任务") == {"success": True, "status": "no_plan"}
    assert round_state.plan is None


@pytest.mark.asyncio
async def test_one_available_expert_is_planned_by_core_and_bound(monkeypatch):
    expert = replace(_experts("a")["a"], source="cache", avatar="avatars/cache.png")
    model = SimpleNamespace(invoke=AsyncMock(return_value={"content": json.dumps({
        "status": "ready", "nodes": [{"id": "a", "task": "整理资料"}], "edges": [],
    })}))
    bind = AsyncMock(return_value=["bound"])
    monkeypatch.setattr(expert_team, "scan_local_experts", AsyncMock(return_value={"a": expert}))
    monkeypatch.setattr(expert_team, "_subagent_config", lambda _agent, expert, _model: expert.id)
    monkeypatch.setattr(expert_team, "apply_extension_hot", bind)
    round_state = expert_team.DynamicExpertRound(FakeAgent(), "整理资料", model)
    tool = expert_team.ExpertPlanTool(lambda _sid: round_state)

    result = await tool.invoke({"query": "整理资料"}, session=_session())

    assert result["success"] is True and result["status"] == "ready"
    assert result["bindings"] == {"a": expert_team._subagent_type("a")}
    assert result["planned_graph"]["graph"]["edges"] == []
    assert result["expert_assets"] == {"a": {"source": "cache", "avatar": "avatars/cache.png"}}
    assert bind.await_args.args[1].subagents == ["a"]
    assert await tool.invoke({"query": "重复规划"}, session=_session()) == result
    model.invoke.assert_awaited_once()
    bind.assert_awaited_once()


@pytest.mark.asyncio
async def test_binding_failure_does_not_leave_a_plan(monkeypatch):
    round_state, _agent = await _ready_round(monkeypatch)

    monkeypatch.setattr(expert_team, "apply_extension_hot", AsyncMock(side_effect=RuntimeError("绑定失败")))
    result = await round_state.compose("用户任务")
    assert result["success"] is False and round_state.plan is None


@pytest.mark.asyncio
async def test_round_close_drains_only_owned_interrupted_children_and_skill_rails(monkeypatch):
    round_state, agent = await _ready_round(monkeypatch)
    await round_state.compose("用户任务")
    owned = SimpleNamespace(card=SimpleNamespace(id="this-round"), cleanup_task_resources=AsyncMock())
    other = SimpleNamespace(card=SimpleNamespace(id="other-round"), cleanup_task_resources=AsyncMock())
    pending = {"ours": (owned, False), "theirs": (other, False)}
    tool = SimpleNamespace(_pending_subagents=pending)
    agent.get = lambda _name: ToolCard(id="parent-task-tool", name="task_tool", description="delegate")
    monkeypatch.setattr(expert_team.Runner.resource_mgr, "get_tool", lambda _id: tool)
    skill_rail = SimpleNamespace(close=AsyncMock())
    round_state._skill_rails = [skill_rail]
    round_state._child_ids = {"this-round"}

    async def unbind(_agent, _refs):
        assert set(pending) == {"theirs"}
        owned.cleanup_task_resources.assert_awaited_once()
        skill_rail.close.assert_awaited_once()

    monkeypatch.setattr(expert_team, "unapply_extension_hot", unbind)
    await round_state.close()
    await round_state.close()
    other.cleanup_task_resources.assert_not_awaited()
    skill_rail.close.assert_awaited_once()
    assert (await round_state.compose("用户任务"))["reason"] == "round_closed"


@pytest.mark.asyncio
async def test_cancellation_while_binding_unbinds_completed_batch(monkeypatch):
    round_state, _agent = await _ready_round(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    unbound = []

    async def bind(_agent, _parts):
        started.set()
        await release.wait()
        return ["bound"]

    async def unbind(_agent, refs):
        unbound.extend(refs)

    monkeypatch.setattr(expert_team, "apply_extension_hot", bind)
    monkeypatch.setattr(expert_team, "unapply_extension_hot", unbind)
    planning = asyncio.create_task(round_state.compose("用户任务"))
    await started.wait()
    planning.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await planning
    await round_state.close()
    assert unbound == ["bound"]


@pytest.mark.asyncio
async def test_dependency_order_result_passing_and_duplicate_guard(monkeypatch):
    round_state, agent = await _ready_round(monkeypatch, [("a", "b")])
    await round_state.compose("用户任务")
    rail = expert_team.DynamicExpertRail(lambda _sid: round_state)
    rail.init(agent)

    early = _tool_ctx(expert_team._subagent_type("b"), "early-b")
    await rail.before_tool_call(early)
    assert early.extra["_skip_tool"] and "前置专家" in early.inputs.tool_msg.content
    for expert_id, output in [("a", "资料已整理"), ("b", "审阅完成")]:
        ctx = _tool_ctx(expert_team._subagent_type(expert_id), f"call-{expert_id}")
        await rail.before_tool_call(ctx)
        assert not ctx.extra.get("_skip_tool")
        query = json.loads(ctx.inputs.tool_args)["query"]
        assert round_state.plan["nodes"][expert_id]["task"] in query
        if expert_id == "b":
            assert "资料已整理" in query
        ctx.inputs.tool_result = ToolOutput(success=True, data={"output": output})
        await rail.after_tool_call(ctx)
    assert round_state._completed == {"a": "资料已整理", "b": "审阅完成"}
    assert "所有专家节点已完成" in round_state.guidance()

    for call_id in ("repeat", ""):
        rejected = _tool_ctx(expert_team._subagent_type("a"), call_id)
        await rail.before_tool_call(rejected)
        assert rejected.extra["_skip_tool"]
        assert ("不可重复" if call_id else "工具调用 ID") in rejected.inputs.tool_msg.content
