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


@pytest.mark.asyncio
async def test_candidate_count_excludes_duplicates_groups_damaged_and_unloadable_experts(tmp_path, monkeypatch):
    local_root, cache_root = tmp_path / "experts", tmp_path / "experts_cache"
    _make_expert(local_root, "shared", "本地版本")
    _make_expert(cache_root, "shared", "缓存版本")
    _make_expert(cache_root, "cached", "缓存独立专家")
    broken = _make_expert(local_root, "damaged", "损坏专家")
    (broken / "manifest.json").write_text("{}", encoding="utf-8")
    _make_expert(cache_root, "unloadable", "无法加载资源")
    group = cache_root / "group"
    group.mkdir()
    (group / "manifest.json").write_text('{"package_type":"agent_group"}', encoding="utf-8")
    monkeypatch.setattr(expert_team, "get_agent_experts_dir", lambda: local_root)
    monkeypatch.setattr(expert_team, "get_expert_cache_dir", lambda: cache_root)
    monkeypatch.setattr(expert_store, "get_expert_cache_dir", lambda: cache_root)
    def config(_agent, expert, _model):
        if expert.id == "unloadable":
            raise ValueError("missing resource")
        return expert.id
    monkeypatch.setattr(expert_team, "_subagent_config", config)
    planner = AsyncMock(return_value={"status": "no_plan"})
    monkeypatch.setattr(expert_team, "plan_experts", planner)
    round_state = expert_team.DynamicExpertRound(FakeAgent(), "用户任务", object())
    await round_state.compose("用户任务")
    assert round_state.candidate_count == 2
    assert {candidate.id for candidate in planner.await_args.args[1]} == {"shared", "cached"}


def test_expert_config_has_isolated_skill_rail_and_owner_without_parent_skill_tools(monkeypatch):
    parent_tool = SimpleNamespace(card=ToolCard(id="general_parent", name="general", description="通用工具"))
    scoped_names = [
        "skill_tool", "list_skill", "skill_index", "skill_index_build", "symphony_read_graph",
        "symphony_compose_graph", "symphony_plan_experts", "task_tool", "sessions_spawn",
        "Agent", "task", "enter_plan_mode", "exit_plan_mode", "switch_mode",
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
    nodes.update({key: {"label": f"专家{key}", "description": f"职责{key}", "task": f"处理{key}"}
                  for key in expert_ids if key not in nodes})
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

    assert result == {"success": True, "status": "no_plan", "reason": "no_suitable_plan"}
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
    round_state = expert_team.DynamicExpertRound(agent, "用户任务", model, session_id="session-1")
    return round_state, agent


async def _confirm_round(round_state):
    await round_state.compose("用户任务")
    metadata = round_state.request_confirmation("session-1", "plan-call")
    nonce = round_state.claim_confirmation_resume("session-1", "plan-call", metadata or {})
    return await round_state.confirm_and_bind(
        "session-1", "plan-call", metadata or {}, confirmation_nonce=nonce or "",
    )


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
    await _confirm_round(round_state)
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
    state, tool, rail, gates, started, _, call, execute = await _group_expert_runtime(monkeypatch)
    original = tool.parent_agent.create_subagent
    def create(kind, sid, **kwargs):
        child = original(kind, sid, **kwargs)
        if kind == state._bindings["a"]:
            async def fail(inputs):
                started["a"].set()
                return {"result_type": "error", "output": "A failed"}
            child.invoke = fail
        return child
    tool.parent_agent.create_subagent = create
    try:
        result = await execute(call("ab", "a", "b"))
        gates["b"].set()
        ready = await tool.task_manager.wait_any(state._task_scope, result.data["pending_ids"], 2)
        assert any(r["status"] == "failed" for r in ready.ready)
        # Outer success must never turn a failed expert into a completed one.
        assert state._states["a"] == "failed" and "a" not in state._completed
    finally:
        await state.close()
        await tool.close_task_groups("session-1")


@pytest.mark.asyncio
async def test_interrupted_expert_resumes_same_call_without_rewriting_interactive_input(monkeypatch):
    state, tool, rail, gates, _, children, call, execute = await _group_expert_runtime(monkeypatch, [("a", "b")])
    original = tool.parent_agent.create_subagent
    def create(kind, sid, **kwargs):
        child = original(kind, sid, **kwargs)
        calls = 0
        async def invoke(inputs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"result_type": "interrupt", "interrupt_ids": ["approval"], "state": []}
            assert isinstance(inputs["query"], InteractiveInput)
            return {"result_type": "answer", "output": "恢复后的专家结果"}
        child.invoke = invoke
        return child
    tool.parent_agent.create_subagent = create
    try:
        first = await execute(call("call-a", "a"))
        interrupted = await tool.invoke({"action": "wait", "task_ids": first.data["pending_ids"]},
                                      session=call("unused").session, tool_call_id="await-approval")
        assert state._states["a"] == "interrupted"
        answer = InteractiveInput()
        answer.update(interrupted["interrupt_ids"][0], {"confirmed": True})
        early = call("early", "b")
        await rail.before_tool_call(early)
        assert early.extra["_skip_tool"]
        resumed = call("call-a", "a")
        resumed.inputs.tool_args["query"] = answer
        resumed.inputs.tool_args.pop("timeout_s")
        result = await execute(resumed)
        assert result.data["ready"][0]["output"] == "恢复后的专家结果"
        assert resumed.inputs.tool_args["query"] is answer
        assert len(children) == 1 and state._states["a"] == "completed"
        successor = call("b", "b")
        await rail.before_tool_call(successor)
        assert "恢复后的专家结果" in successor.inputs.tool_args["tasks"][0]["task_description"]
    finally:
        await state.close()
        await tool.close_task_groups("session-1")


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
    assert await round_state.compose("普通任务") == {
        "success": True, "status": "no_plan", "reason": "no_available_experts",
    }
    assert round_state.plan is None


@pytest.mark.asyncio
async def test_one_available_expert_is_planned_by_core_and_handed_off(monkeypatch):
    expert = replace(_experts("a")["a"], source="cache", avatar="avatars/cache.png")
    model = SimpleNamespace(invoke=AsyncMock(return_value={"content": json.dumps({
        "status": "ready", "nodes": [{"id": "a", "task": "整理资料"}], "edges": [],
    })}))
    bind = AsyncMock(return_value=["bound"])
    monkeypatch.setattr(expert_team, "scan_local_experts", AsyncMock(return_value={"a": expert}))
    monkeypatch.setattr(expert_team, "_subagent_config", lambda _agent, expert, _model: expert.id)
    monkeypatch.setattr(expert_team, "apply_extension_hot", bind)
    handoff = AsyncMock(return_value=[])
    round_state = expert_team.DynamicExpertRound(
        FakeAgent(), "整理资料", model, session_id="session-1", handoff_expert=handoff,
    )
    tool = expert_team.ExpertPlanTool(lambda _sid: round_state)

    await round_state.compose("整理资料")
    await _confirm_round(round_state)
    result = await tool.invoke({"query": "整理资料"}, session=_session())

    assert result == {
        "success": True, "status": "handed_off",
        "plan_id": round_state._confirmation["dynamic_expert_plan_id"],
        "expert": {"id": "a", "name": "专家a", "source": "cache", "avatar": "avatars/cache.png"},
    }
    assert round_state.plan is None and round_state._bindings == {}
    assert "task_tool" not in round_state.guidance()
    assert "常规处理" not in round_state.guidance()
    handoff.assert_awaited_once_with(round_state, expert)
    assert await tool.invoke({"query": "重复规划"}, session=_session()) == result
    model.invoke.assert_awaited_once()
    bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_binding_failure_does_not_leave_a_plan(monkeypatch):
    round_state, _agent = await _ready_round(monkeypatch)

    monkeypatch.setattr(expert_team, "apply_extension_hot", AsyncMock(side_effect=RuntimeError("绑定失败")))
    result = await _confirm_round(round_state)
    assert result["success"] is False and round_state.plan is None


@pytest.mark.asyncio
async def test_round_close_drains_only_owned_interrupted_children_and_skill_rails(monkeypatch):
    from openjiuwen.harness.tools.subagent.task_manager import TaskRequest
    state, tool, _, gates, started, children, call, execute = await _group_expert_runtime(monkeypatch)
    await execute(call("owned", "a"))
    await asyncio.wait_for(started["a"].wait(), 2)
    async def execute_other(_query):
        await gates["b"].wait()
        return {"result_type": "answer", "output": "other"}
    other_cleanup = AsyncMock()
    foreign = tool.task_manager.submit_batch(state._task_scope, "unrelated", [TaskRequest("general-purpose", "other")],
                                            existing_ids=[], executor_factory=lambda execution: (execute_other, other_cleanup))
    skill_rail = SimpleNamespace(close=AsyncMock())
    state._skill_rails = [skill_rail]
    async def unbind(_agent, _refs):
        children[0].cleanup_task_resources.assert_awaited_once()
        skill_rail.close.assert_awaited_once()
        other_cleanup.assert_not_awaited()
    monkeypatch.setattr(expert_team, "unapply_extension_hot", unbind)
    try:
        await state.close()
        await state.close()
        assert state._states["a"] == "cancelled"
        ready = await tool.task_manager.wait_any(state._task_scope, foreign.wait_ids, 0)
        assert ready.pending_ids == foreign.wait_ids
        skill_rail.close.assert_awaited_once()
    finally:
        gates["b"].set()
        await tool.close_task_groups("session-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_fails", [False, True])
async def test_cancellation_while_binding_preserves_cancel_and_cleans_completed_batch(monkeypatch, binding_fails):
    round_state, _agent = await _ready_round(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    unbound = []

    async def bind(_agent, _parts):
        started.set()
        await release.wait()
        if binding_fails:
            raise RuntimeError("binding failed after cancellation")
        return ["bound"]

    async def unbind(_agent, refs):
        unbound.extend(refs)

    monkeypatch.setattr(expert_team, "apply_extension_hot", bind)
    monkeypatch.setattr(expert_team, "unapply_extension_hot", unbind)
    planning = asyncio.create_task(_confirm_round(round_state))
    await started.wait()
    planning.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await planning
    await round_state.close()
    assert unbound == ([] if binding_fails else ["bound"])


@pytest.mark.asyncio
async def test_dependency_order_result_passing_and_duplicate_guard(monkeypatch):
    state, tool, rail, gates, _, _, call, execute = await _group_expert_runtime(monkeypatch, [("a", "b")])
    try:
        early = call("early", "b")
        await rail.before_tool_call(early)
        assert early.extra["_skip_tool"] and "前置专家" in early.inputs.tool_msg.content
        for key in ("a", "b"):
            value = call("call-" + key, key)
            gates[key].set()
            value.inputs.tool_args.pop("timeout_s")
            result = await execute(value)
            assert result.data["ready"][0]["status"] == "completed"
            if key == "b":
                assert "a evidence" in value.inputs.tool_args["tasks"][0]["task_description"]
        assert state._completed == {"a": "a evidence", "b": "b evidence"}
        assert "所有专家节点已完成" in state.guidance()
        for call_id in ("repeat", ""):
            duplicate = call(call_id, "a")
            await rail.before_tool_call(duplicate)
            assert duplicate.extra["_skip_tool"]
    finally:
        await state.close()
        await tool.close_task_groups("session-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("tool_name", ["Agent", "sessions_spawn", "symphony_compose_graph", "task_tool"])
async def test_dynamic_plan_blocks_competing_delegation(monkeypatch, confirmed, tool_name):
    round_state, agent = await _ready_round(monkeypatch)
    if confirmed:
        await _confirm_round(round_state)
    else:
        await round_state.compose("用户任务")
    rail = expert_team.DynamicExpertRail(lambda _: round_state)
    rail.init(agent)
    ctx = _tool_ctx("general-purpose", "competing-call")
    ctx.inputs.tool_name = tool_name
    await rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"]
    assert not round_state._dispatches
    menu = SimpleNamespace(session=_session(), inputs=ModelCallInputs(tools=[
        SimpleNamespace(name="Agent"), SimpleNamespace(name="read_file"),
    ]))
    await rail.before_model_call(menu)
    assert "Agent" not in {tool.name for tool in menu.inputs.tools}
    assert "read_file" in {tool.name for tool in menu.inputs.tools}


@pytest.mark.asyncio
async def test_dynamic_tasks_remain_blocked_when_session_reenters_plan_mode(monkeypatch):
    round_state, agent = await _ready_round(monkeypatch)
    await _confirm_round(round_state)
    agent.load_state = lambda _: SimpleNamespace(plan_mode=SimpleNamespace(mode="plan"))
    rail = expert_team.DynamicExpertRail(lambda _: round_state)
    rail.init(agent)
    ctx = _tool_ctx(expert_team._subagent_type("a"), "forged-plan-task")
    await rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] and "只读规划模式" in ctx.inputs.tool_msg.content
    assert not round_state._dispatches


async def _group_expert_runtime(monkeypatch, edges=(), expert_ids=("a", "b")):
    from openjiuwen.core.session.agent import Session
    from openjiuwen.harness.tools.subagent.task_tool import TaskTool

    state, agent = await _ready_round(monkeypatch, edges, expert_ids)
    await _confirm_round(state)
    gates = {key: asyncio.Event() for key in expert_ids}
    started = {key: asyncio.Event() for key in expert_ids}
    children = []

    def create(kind, sid, **kwargs):
        key = next(key for key, value in state._bindings.items() if kind == value)
        async def invoke(inputs):
            started[key].set()
            await gates[key].wait()
            return {"result_type": "answer", "output": key + " evidence"}
        child = SimpleNamespace(card=SimpleNamespace(id=sid), invoke=invoke,
                                cleanup_task_resources=AsyncMock())
        children.append(child)
        return child

    agent.deep_config.subagents = []
    agent.resolve_subagent_type = lambda name: name if name in state._bindings.values() else None
    agent.create_subagent = create
    tool = TaskTool(ToolCard(id="group-experts", name="task_tool", description="test"), agent)
    agent.get = lambda name: tool.card if name == "task_tool" else None
    monkeypatch.setattr(expert_team.Runner.resource_mgr, "get_tool", lambda _id: tool)
    session = Session(session_id="session-1")
    rail = expert_team.DynamicExpertRail(lambda sid: state if sid == "session-1" else None)
    rail.init(agent)

    def call(call_id, *keys, action="dispatch", task_ids=()):
        args = {"action": action, "timeout_s": 0}
        if action == "dispatch":
            args["tasks"] = [{"subagent_type": state._bindings[key], "task_description": key} for key in keys]
        if task_ids:
            args["task_ids"] = list(task_ids)
        value = _tool_ctx("unused", call_id)
        value.session = session
        value.inputs.tool_args = args
        value.inputs.tool_call.arguments = args
        return value

    async def execute(value):
        await rail.before_tool_call(value)
        assert not value.extra.get("_skip_tool"), getattr(value.inputs, "tool_msg", None)
        value.inputs.tool_result = await tool.invoke(value.inputs.tool_args, session=session,
                                                     tool_call_id=value.inputs.tool_call.id)
        await rail.after_tool_call(value)
        return value.inputs.tool_result

    return state, tool, rail, gates, started, children, call, execute


@pytest.mark.asyncio
async def test_group_experts_batch_and_complete_without_wait(monkeypatch):
    state, tool, rail, gates, started, children, call, execute = await _group_expert_runtime(monkeypatch)
    try:
        result = await execute(call("ab", "a", "b"))
        assert result.success and len(result.data["pending_ids"]) == 2
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in started.values())), 2)
        gates["a"].set()
        await tool.task_manager.wait_any(tool.task_manager.scope_if_active("session-1"),
                                         [result.data["pending_ids"][0]], 2)
        assert state._states == {"a": "completed", "b": "running"}
        assert state._completed == {"a": "a evidence"}
        duplicate = call("duplicate", "a")
        await rail.before_tool_call(duplicate)
        assert duplicate.extra["_skip_tool"]
        assert len(children) == 2
    finally:
        for gate in gates.values():
            gate.set()
        await state.close()
        await tool.close_task_groups("session-1")


@pytest.mark.asyncio
async def test_group_experts_reject_invalid_batch_atomically_and_pass_dependency_result(monkeypatch):
    state, tool, rail, gates, started, _, call, execute = await _group_expert_runtime(monkeypatch, [("a", "b")])
    try:
        invalid = call("invalid", "a", "b")
        await rail.before_tool_call(invalid)
        assert invalid.extra["_skip_tool"] and state._states == {"a": "pending", "b": "pending"}
        a = await execute(call("a", "a"))
        gates["a"].set()
        await tool.task_manager.wait_any(tool.task_manager.scope_if_active("session-1"), a.data["pending_ids"], 2)
        b = call("b", "b")
        await rail.before_tool_call(b)
        assert not b.extra.get("_skip_tool")
        assert "a evidence" in b.inputs.tool_args["tasks"][0]["task_description"]
        foreign = call("foreign", action="cancel", task_ids=["not-ours"])
        await rail.before_tool_call(foreign)
        assert foreign.extra["_skip_tool"]
    finally:
        for gate in gates.values():
            gate.set()
        await state.close()
        await tool.close_task_groups("session-1")


@pytest.mark.asyncio
async def test_expert_batch_has_no_pre_dispatch_semaphore_or_hard_five_limit(monkeypatch):
    keys = tuple("abcdef")
    state, tool, _, gates, started, children, call, execute = await _group_expert_runtime(monkeypatch, expert_ids=keys)
    try:
        result = await asyncio.wait_for(execute(call("six", *keys)), 2)
        assert result.success and len(result.data["pending_ids"]) == 6
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 2)
        assert len(children) == 6
    finally:
        for gate in gates.values():
            gate.set()
        await state.close()
        await tool.close_task_groups("session-1")


@pytest.mark.asyncio
async def test_core_dispatch_rejection_rolls_back_expert_reservations(monkeypatch):
    state, tool, _, gates, _, children, call, execute = await _group_expert_runtime(monkeypatch)
    try:
        tool.task_manager.max_tasks = 0
        rejected = await execute(call("ab", "a", "b"))
        assert not rejected.success and not children
        assert state._states == {"a": "pending", "b": "pending"}
        tool.task_manager.max_tasks = 128
        retry = await execute(call("ab", "a", "b"))
        assert retry.success and len(retry.data["pending_ids"]) == 2
    finally:
        for gate in gates.values():
            gate.set()
        await state.close()
        await tool.close_task_groups("session-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "timed_out", "cancelled"])
async def test_unsuccessful_expert_blocks_successor_and_preserves_real_status(monkeypatch, outcome):
    state, tool, rail, gates, started, _, call, execute = await _group_expert_runtime(monkeypatch, [("a", "b")])
    if outcome == "failed":
        original = tool.parent_agent.create_subagent
        def create(*args, **kwargs):
            child = original(*args, **kwargs)
            async def fail(inputs):
                started["a"].set()
                return {"result_type": "error", "output": "failed evidence"}
            child.invoke = fail
            return child
        tool.parent_agent.create_subagent = create
    if outcome == "timed_out":
        tool.task_manager.task_timeout_s = 0.01
    try:
        result = await execute(call("a", "a"))
        await asyncio.wait_for(started["a"].wait(), 2)
        if outcome == "cancelled":
            await execute(call("cancel", action="cancel", task_ids=result.data["pending_ids"]))
        await tool.task_manager.wait_any(state._task_scope, result.data["pending_ids"], 2)
        assert state._states["a"] == outcome and "a" not in state._completed
        successor = call("b", "b")
        await rail.before_tool_call(successor)
        assert successor.extra["_skip_tool"] and "前置专家执行失败" in successor.inputs.tool_msg.content
    finally:
        await state.close()
        await tool.close_task_groups("session-1")
