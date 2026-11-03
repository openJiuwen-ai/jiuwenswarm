"""动态专家的真实 TaskTool / DeepAgent / 技能工具执行和资源生命周期。"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    AssistantMessageChunk,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
)
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.extension_binder import apply_extension_hot
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.resources import ExtensionParts, load_agent_template_package
from openjiuwen.harness.resources.extension_resolver import ResolvedSkill
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.tools.subagent.task_tool import TaskTool

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.symphony import expert_team
from jiuwenswarm.symphony.expert_skills import DynamicExpertSkillRail, snapshot_public_skills


class ScriptedModel:
    """只替换模型输出，保留 Core 的回调、工具执行、会话和中断管线。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="unused", api_base="http://127.0.0.1:1",
        )
        self.model_config = ModelRequestConfig(model_name="scripted-expert")

    async def invoke(self, messages, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), **kwargs})
        if not self.responses:
            raise AssertionError("出现未编排的模型调用")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(messages, **kwargs)
            if inspect.isawaitable(response):
                response = await response
        return response

    async def stream(self, messages, **kwargs):
        response = await self.invoke(messages, **kwargs)
        yield AssistantMessageChunk(content=response.content, tool_calls=response.tool_calls)


def _call(tool_name, **arguments):
    return AssistantMessage(content="", tool_calls=[ToolCall(
        id=uuid4().hex, name=tool_name, type="function",
        arguments=json.dumps(arguments, ensure_ascii=False),
    )])


def _skill(root: Path, name: str, body: str = "技能正文") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 的技能说明\n---\n{body}\n", encoding="utf-8",
    )
    return directory


def _resolved(directory):
    return ResolvedSkill(directory=str(directory), mode="auto_list")


def _tool_messages(model):
    return [str(message.content) for call in model.calls for message in call["messages"]
            if getattr(message, "role", None) == "tool"]


@pytest.fixture(autouse=True)
async def _runner(monkeypatch):
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *a, **k: None)
    await Runner.start()
    yield
    await Runner.stop()


@pytest.fixture
def production_sandbox_enabled(monkeypatch):
    # Importing the adapter installs its real create_subagent runtime patch.
    # Drive the production switch so that patch cannot reset the child's
    # restrict_to_work_dir setting when this file runs alongside adapter tests.
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    monkeypatch.setattr(interface_deep, "get_sandbox_runtime", lambda: {"enabled": True})


async def _parent(tmp_path, *, public_rail=None):
    parent = create_deep_agent(
        model=ScriptedModel(),
        card=AgentCard(id=uuid4().hex, name="parent", description="测试主代理"),
        workspace=str(tmp_path / "workspace"),
        rails=[public_rail] if public_rail is not None else [],
        enable_skill_discovery=False, enable_read_image_multimodal=False,
        enable_security_rail=False, enable_llm_retry_rail=False,
        enable_tool_resilience_rail=False, auto_create_workspace=False,
        restrict_to_work_dir=False,
    )
    await parent.ensure_initialized()
    if public_rail is not None:
        await public_rail.reload_skills()
    return parent


def _spec(model, rail, *, name="expert", extra_rails=()):
    owner = f"expert_{uuid4().hex}"
    return SubAgentConfig(
        agent_card=AgentCard(id=owner, name=name, description="测试专家"),
        system_prompt="调用指定技能后回答。", model=model,
        rails=[rail, *extra_rails], skills=None, restrict_to_work_dir=False,
        max_iterations=10, enable_task_loop=False,
        factory_kwargs={
            "tool_owner_id": owner, "enable_skill_discovery": False,
            "enable_read_image_multimodal": False, "enable_security_rail": False,
            "enable_llm_retry_rail": False, "enable_tool_resilience_rail": False,
            "auto_create_workspace": False,
        },
    )


async def _bind(parent, *specs):
    await apply_extension_hot(parent, ExtensionParts(subagents=list(specs)))
    card = parent.ability_manager.get("task_tool")
    tool = Runner.resource_mgr.get_tool(card.id)
    assert isinstance(tool, TaskTool)
    original_create = parent.create_subagent
    def create(*args, **kwargs):
        child = original_create(*args, **kwargs)
        for spec in specs:
            if spec.agent_card.name == args[0]:
                for blueprint, runtime in zip(
                    [rail for rail in spec.rails if isinstance(rail, DynamicExpertSkillRail)],
                    child.find_rails_by_type((DynamicExpertSkillRail,)),
                ):
                    blueprint._test_runtime_rail = runtime
                    assert blueprint is not runtime  # TaskTool creates an isolated child.
        return child
    parent.create_subagent = create
    return tool


def _runtime_rail(blueprint):
    return getattr(blueprint, "_test_runtime_rail", blueprint)


async def _invoke(tool, *, name="expert", session_id="parent-session", call_id="outer-call", query=None, timeout_s=None):
    args = {"action": "dispatch", "tasks": [{"subagent_type": name, "task_description": "执行专家节点"}]}
    if timeout_s is not None:
        args["timeout_s"] = timeout_s
    if query is not None:
        args["query"] = query
    return await asyncio.wait_for(tool.invoke(
        args, session=Session(session_id=session_id), tool_call_id=call_id,
    ), timeout=10)


def _assert_clean(rail):
    rail = _runtime_rail(rail)
    child = rail._agent
    assert rail.load_record is None
    assert rail.skills_meta == []
    assert rail.bound_leaf_dirs == set()
    assert rail.skills_dir == []
    assert child._load_records == {}
    assert not child.deep_config.skills
    for name in ("skill_tool", "list_skill"):
        assert child.ability_manager.get(name) is None
        assert Runner.resource_mgr.get_tool(f"{name}_{child.deep_config.tool_owner_id}") is None


def _package(root, expert_id, body="专家正文", *, with_tool=False):
    package = root / expert_id
    (package / "persona").mkdir(parents=True)
    (package / "persona" / "AGENT.md").write_text("你是测试专家。", encoding="utf-8")
    _skill(package / "skills", "private", body)
    manifest = package / "manifest.json"
    manifest.write_text(json.dumps({
        "packageType": "agent_template",
        "agentCard": {"id": expert_id, "name": expert_id, "description": "测试专家"},
        "persona": {"dir": "persona"}, "skills": [{"dir": "skills/private"}],
    }), encoding="utf-8")
    if with_tool:
        (package / "evidence.py").write_text(
            'from openjiuwen.core.foundation.tool import Tool, ToolCard\n'
            'class EvidenceTool(Tool):\n'
            '    def __init__(self):\n'
            '        super().__init__(ToolCard(id="expert_evidence", name="expert_evidence", description="专家取证"))\n'
            '    async def invoke(self, inputs, **kwargs):\n'
            '        return {"evidence": "已取得专家工具证据"}\n'
            '    async def stream(self, inputs, **kwargs):\n'
            '        yield {"evidence": "已取得专家工具证据"}\n',
            encoding="utf-8",
        )
        value = json.loads(manifest.read_text())
        value["tools"] = [{"file": "evidence.py", "class": "EvidenceTool"}]
        manifest.write_text(json.dumps(value))
    return expert_team.LocalExpert(
        id=expert_id, name=expert_id, description="测试专家",
        template=load_agent_template_package(manifest), package_dir=package, source="cache",
    )


def _production_spec(parent, expert, model):
    spec = expert_team._subagent_config(parent, expert, model)
    spec.factory_kwargs.update({
        "enable_read_image_multimodal": False, "enable_security_rail": False,
        "enable_llm_retry_rail": False, "enable_tool_resilience_rail": False,
        "auto_create_workspace": False,
    })
    rails = [rail for rail in spec.rails if isinstance(rail, DynamicExpertSkillRail)]
    assert len(rails) == 1
    assert spec.skills is None
    return spec, rails[0]


class InterruptSkillOnce(AgentRail):
    """在真正执行技能工具前产生一次真实 Core HITL 中断。"""

    def __init__(self):
        super().__init__()
        self.interrupted = False

    async def before_tool_call(self, ctx):
        if ctx.inputs.tool_name == "skill_tool" and not self.interrupted:
            self.interrupted = True
            raise AbortError("等待测试确认", cause=ToolInterruptException(InterruptRequest(message="确认继续")))


@pytest.mark.asyncio
@pytest.mark.parametrize("work_mode", ["work", "code", "design"])
@pytest.mark.parametrize(("choice", "answer_other_first"), [
    ("confirm", False), ("cancel", False), ("feedback", False), ("skip", False), ("confirm", True),
    ("load_failure", False), ("metadata_failure", False),
])
async def test_real_deep_agent_confirms_then_hands_off_original_turn(
    tmp_path, monkeypatch, choice, answer_other_first, work_mode,
):
    from openjiuwen.harness.rails.interrupt.ask_user_rail import AskUserRail
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail, convert_interactions_to_ask_user_questions,
    )
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.symphony.expert_confirmation import DynamicExpertConfirmationRail, confirmation_request_id

    public_root = tmp_path / "public"
    public_root.mkdir()
    parent = await _parent(tmp_path, public_rail=SkillUseRail(skills_dir=str(public_root), skill_mode="all"))
    permission_config = {
        "enabled": True, "permission_mode": "strict",
        "tools": {expert_team.PLAN_TOOL_NAME: "ask", "task_tool": "allow", "skill_tool": "allow",
                  "expert_evidence": "allow"},
    }
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist."
        "get_permissions_with_session_overlay", lambda **_: copy.deepcopy(permission_config),
    )
    permission_rail = build_permission_rail({"permissions": permission_config})
    assert permission_rail is not None
    expert = _package(tmp_path / "experts_cache", "researcher", "确认后执行的真实技能正文", with_tool=True)
    model = parent.deep_config.model
    plan_call = _call(expert_team.PLAN_TOOL_NAME, query="请专家整理资料")
    if answer_other_first:
        other_call = _call("ask_user", questions=[{"question": "资料范围", "header": "范围"}]).tool_calls[0]
        plan_call.tool_calls.append(other_call)
    display_summary = "归纳资料中的关键事实和依据，形成便于主理人复核与汇总的清晰结论。"
    revised_summary = "根据补充条件重新梳理资料范围，提炼关键事实和可执行的后续建议。"
    planned = AssistantMessage(content=json.dumps({
        "status": "ready", "nodes": [{"id": expert.id, "task": "整理资料", "summary": display_summary}],
        "edges": [],
    }))
    model.responses.extend([plan_call, planned])
    if choice == "confirm":
        model.responses.extend([
            _call("expert_evidence"),
            _call("skill_tool", skill_name="private"),
            AssistantMessage(content="专家整理完成：已读技能资料"),
        ])
    elif choice == "feedback":
        model.responses.append(AssistantMessage(content=json.dumps({
            "status": "ready",
            "nodes": [{"id": expert.id, "task": "整理资料", "summary": revised_summary}],
            "edges": [],
        })))
    elif choice in {"cancel", "skip"}:
        model.responses.append(AssistantMessage(content="小艺Work独立完成：资料已整理"))
    scan = AsyncMock(return_value={expert.id: expert})
    monkeypatch.setattr(expert_team, "scan_local_experts", scan)
    spec, _skill_rail = _production_spec(parent, expert, model)
    monkeypatch.setattr(expert_team, "_subagent_config", lambda *_: spec)
    session = Session(session_id=f"confirmed-real-task-{uuid4().hex}")
    emitted = AsyncMock()
    monkeypatch.setattr(session, "write_stream", emitted)
    from jiuwenswarm.server.runtime.session import session_metadata

    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    from jiuwenswarm.common.mode_profiles import profile_for_work_mode
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter

    profile = profile_for_work_mode(work_mode)
    adapter = JiuWenSwarmDeepAdapter() if work_mode == "work" else JiuwenSwarmCodeAdapter()
    adapter._session_instance_mode = profile.manager_mode
    adapter._static_prompt_profile = work_mode
    session_metadata.update_session_metadata(
        session_id=session.get_session_id(), expert_id="", work_mode=work_mode,
        mode=profile.single_canonical, sync=True,
    )
    adapter._instance = parent
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = session.get_session_id()
    parent._loop_session = session
    round_state = expert_team.DynamicExpertRound(
        parent, "请专家整理资料", model, session_id=session.get_session_id(),
        handoff_expert=adapter._handoff_confirmed_expert,
    )
    adapter._dynamic_expert_rounds = {session.get_session_id(): round_state}
    def provider(sid):
        return adapter._dynamic_expert_rounds.get(sid)
    mode_rail = (adapter._build_work_agent_mode_rail() if work_mode == "work"
                 else adapter._build_agent_mode_rail())
    assert mode_rail is not None
    await apply_extension_hot(parent, ExtensionParts(
        tools=[expert_team.ExpertPlanTool(provider)],
        rails=[mode_rail, permission_rail, expert_team.DynamicExpertRail(provider), DynamicExpertConfirmationRail(provider),
               JiuSwarmStreamEventRail()]
              + ([AskUserRail()] if answer_other_first else []),
    ))
    try:
        interrupted = await asyncio.wait_for(parent.invoke({"query": "请专家整理资料"}, session=session), 10)
        assert interrupted["result_type"] == "interrupt"
        assert set(interrupted["interrupt_ids"]) == {call.id for call in plan_call.tool_calls}
        assert round_state.plan is None and round_state.pending_plan is not None
        assert round_state._refs == [] and len(model.calls) == 2
        assert round_state.confirmation_display(round_state._confirmation["dynamic_expert_plan_id"])[
            "members"
        ][0]["summary"] == expert.description
        graph = round_state.pending_plan
        if answer_other_first:
            metadata = dict(round_state._confirmation)
            cards = convert_interactions_to_ask_user_questions([item.payload for item in interrupted["state"]])
            plan_card = next(card for card in cards if card["source"] == "confirm_interrupt")
            other_answer = JiuWenSwarm._build_interactive_input_from_answers(
                other_call.id, [{"question": "资料范围", "custom_input": "国内"}], source="ask_user_interrupt",
            )
            waiting = await asyncio.wait_for(parent.invoke({"query": other_answer}, session=session), 10)
            assert waiting["result_type"] == "interrupt"
            assert waiting["interrupt_ids"] == [plan_call.tool_calls[0].id]
            assert convert_interactions_to_ask_user_questions([item.payload for item in waiting["state"]]) == [plan_card]
            assert round_state.pending_plan is graph and round_state._confirmation == metadata
            assert not round_state.closed and round_state._refs == [] and len(model.calls) == 2
            scan.assert_awaited_once()
        answers = {
            "confirm": [{"selected_options": ["确认并切换专家"]}],
            "cancel": [{"selected_options": ["不用专家，由小艺Work继续"]}],
            "feedback": [{"selected_options": [], "custom_input": "请调整分工"}],
            "skip": [],
            "load_failure": [{"selected_options": ["确认并切换专家"]}],
            "metadata_failure": [{"selected_options": ["确认并切换专家"]}],
        }[choice]
        if choice == "load_failure":
            monkeypatch.setattr(parent, "load_agent_template", AsyncMock(side_effect=ValueError("装载失败")))
        elif choice == "metadata_failure":
            original_write = session_metadata._write_metadata_sync
            def fail_expert_write(session_id, value, **kwargs):
                if value.get("expert_id") == expert.id:
                    raise OSError("写入失败")
                return original_write(session_id, value, **kwargs)
            monkeypatch.setattr(session_metadata, "_write_metadata_sync", fail_expert_write)
        request_id = confirmation_request_id(round_state._confirmation)
        inputs = {"query": JiuWenSwarm._build_interactive_input_from_answers(
            request_id, answers, source="confirm_interrupt",
        )}
        assert adapter._prepare_confirmation_resume(session.get_session_id(), {"request_id": request_id}, inputs)
        assert set(inputs["query"].user_inputs) == {plan_call.tool_calls[0].id}
        result = await asyncio.wait_for(parent.invoke(inputs, session=session), 10)
        tool_results = [
            call.args[0].payload["tool_result"] for call in emitted.await_args_list
            if getattr(call.args[0], "type", None) == "tool_result"
        ]
        if choice in {"load_failure", "metadata_failure"}:
            assert "请再试一次" in result["output"]
            assert round_state.response["status"] == "handoff_failed"
            assert adapter._current_expert_id is None
            assert session_metadata.get_session_metadata(session.get_session_id(), cache_bust=True)["expert_id"] == ""
            assert len(model.calls) == 2
            assert any(item.get("raw_output") == round_state.response for item in tool_results)
            return
        if choice == "feedback":
            assert result["result_type"] == "interrupt"
            assert round_state.pending_plan is not None and round_state.plan is None
            assert round_state._confirmation["dynamic_expert_plan_id"] != request_id.removeprefix("symphony-plan:")
            assert round_state.feedback == "请调整分工"
            assert not round_state.closed and round_state._refs == []
            assert len(model.calls) == 3
            assert round_state.confirmation_display(
                round_state._confirmation["dynamic_expert_plan_id"],
            )["members"][0]["summary"] == expert.description
            assert scan.await_count == 2
            assert not parent.deep_config.subagents
            return
        if choice in ("cancel", "skip"):
            assert round_state.closed and round_state.plan is None and round_state._refs == []
            assert round_state.response["status"] == "declined"
            assert result["output"] == "小艺Work独立完成：资料已整理"
            assert len(model.calls) == 3
            assert not parent.deep_config.subagents
            return
        assert result["output"] == "专家整理完成：已读技能资料"
        assert round_state.plan is None and round_state._completed == {}
        assert round_state.response["status"] == "handed_off"
        assert round_state.response["plan_id"] == request_id.removeprefix("symphony-plan:")
        handoff_results = [item for item in tool_results if item["tool_name"] == expert_team.PLAN_TOOL_NAME]
        assert len(handoff_results) == 1
        assert handoff_results[0]["raw_output"] == round_state.response
        assert handoff_results[0]["tool_call_id"] == plan_call.tool_calls[0].id
        assert adapter._current_expert_id == expert.id
        persisted = session_metadata.get_session_metadata(session.get_session_id(), cache_bust=True)
        assert persisted["expert_id"] == expert.id
        assert (persisted["mode"], persisted["work_mode"]) == (profile.single_canonical, work_mode)
        assert adapter._session_instance_mode == profile.manager_mode
        assert "确认后执行的真实技能正文" in "\n".join(_tool_messages(model))
        assert "已取得专家工具证据" in "\n".join(_tool_messages(model))
        assert not parent.deep_config.subagents
        assert "task_tool" not in round_state.guidance()
        scan.assert_awaited_once()
        assert len(model.calls) == 5
        assert "expert_evidence" not in {tool.name for tool in model.calls[0]["tools"]}
        assert "expert_evidence" in {tool.name for tool in model.calls[2]["tools"]}
        assert "你是测试专家" in str(model.calls[2]["messages"])
        assert sum(getattr(message, "role", None) == "user" and message.content == "请专家整理资料"
                   for message in model.calls[2]["messages"]) == 1
        assert display_summary not in str(model.calls[2:])
    finally:
        await adapter._close_dynamic_expert_round(session.get_session_id())
    assert not parent.deep_config.subagents
    assert adapter._current_expert_id == expert.id
    assert "private" in {skill.name for skill in parent.find_rails_by_type((SkillUseRail,))[0].get_skills_for_session()}
    model.responses.extend([_call("skill_tool", skill_name="private"), AssistantMessage(content="专家继续处理后续任务")])
    continued = await asyncio.wait_for(parent.invoke({"query": "继续处理下一部分"}, session=session), 10)
    assert continued["output"] == "专家继续处理后续任务"
    assert adapter._current_expert_id == expert.id
    await adapter._apply_expert(None)


@pytest.mark.asyncio
async def test_real_task_uses_own_and_public_skills_without_disabled_siblings(tmp_path):
    public_root = tmp_path / "public"
    _skill(public_root, "same", "公共同名正文")
    _skill(public_root, "public-only", "公共独有正文")
    _skill(public_root, "hidden", "不可暴露正文")
    parent_rail = SkillUseRail(skills_dir=str(public_root), disabled_skills={"hidden"})
    parent = await _parent(tmp_path, public_rail=parent_rail)
    parent_tool = parent.ability_manager.get("skill_tool")
    own = _skill(tmp_path / "expert", "same", "专家优先正文")
    model = ScriptedModel(
        _call("list_skill"), _call("skill_tool", skill_name="same"),
        _call("skill_tool", skill_name="public-only"),
        _call("skill_tool", skill_name="hidden"), AssistantMessage(content="完成"),
    )
    rail = DynamicExpertSkillRail(public_skills=snapshot_public_skills(parent), expert_skills=[_resolved(own)])
    tool = await _bind(parent, _spec(model, rail))

    result = await _invoke(tool)

    assert result.success and result.data["ready"][0]["output"] == "完成"
    seen = "\n".join(_tool_messages(model))
    assert "专家优先正文" in seen and "公共独有正文" in seen
    assert "公共同名正文" not in seen and "不可暴露正文" not in seen
    assert "Skill not found: hidden" in seen
    assert "hidden" not in _tool_messages(model)[0]
    assert all({"skill_tool", "list_skill"} <= {tool.name for tool in call["tools"]} for call in model.calls)
    assert parent.ability_manager.get("skill_tool") is parent_tool
    assert {skill.name for skill in parent_rail.skills_meta} == {"same", "public-only"}
    _assert_clean(rail)


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["missing", "partial_bind_error"])
async def test_missing_or_unmountable_skill_stops_before_model(tmp_path, monkeypatch, broken):
    directory = tmp_path / "expert" / "required"
    model = ScriptedModel(AssistantMessage(content="不应调用"))
    rail = DynamicExpertSkillRail(public_skills=[], expert_skills=[_resolved(directory)])
    if broken == "partial_bind_error":
        _skill(directory.parent, directory.name)
        second = _skill(directory.parent, "second")
        rail = DynamicExpertSkillRail(public_skills=[], expert_skills=[_resolved(directory), _resolved(second)])
        original_reload = DynamicExpertSkillRail.reload_skills
        reload_count = 0

        async def fail_second_bind(self):
            nonlocal reload_count
            reload_count += 1
            if reload_count == 2:
                raise RuntimeError("second skill bind failed")
            await original_reload(self)

        monkeypatch.setattr(DynamicExpertSkillRail, "reload_skills", fail_second_bind)
    parent = await _parent(tmp_path)
    tool = await _bind(parent, _spec(model, rail))
    result = await _invoke(tool)
    assert result.success and result.data["ready"][0]["status"] == "failed"
    assert result.data["ready"][0]["error"]
    assert model.calls == []
    _assert_clean(rail)


@pytest.mark.asyncio
async def test_real_model_failure_releases_skill_load(tmp_path):
    directory = _skill(tmp_path / "expert", "required")
    model = ScriptedModel(RuntimeError("scripted model failed"))
    rail = DynamicExpertSkillRail(public_skills=[], expert_skills=[_resolved(directory)])
    parent = await _parent(tmp_path)
    result = await _invoke(await _bind(parent, _spec(model, rail)))
    assert result.success and result.data["ready"][0]["status"] == "failed"
    assert "scripted model failed" in str(result.data["ready"][0]["error"])
    assert len(model.calls) == 1
    _assert_clean(rail)


@pytest.mark.asyncio
@pytest.mark.parametrize("during_mount", [False, True])
async def test_real_task_cancellation_drains_mount_and_releases_skills(tmp_path, monkeypatch, during_mount):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_model(_messages, **_kwargs):
        entered.set()
        await release.wait()
        return AssistantMessage(content="完成")

    model = ScriptedModel(blocked_model)
    directory = _skill(tmp_path / "expert", "required")
    rail = DynamicExpertSkillRail(public_skills=[], expert_skills=[_resolved(directory)])
    parent = await _parent(tmp_path)
    tool = await _bind(parent, _spec(model, rail))
    if during_mount:
        original_load = DeepAgent.load_plugin_ability

        async def blocked_mount(agent, **kwargs):
            record = await original_load(agent, **kwargs)
            entered.set()
            await release.wait()
            return record

        monkeypatch.setattr(DeepAgent, "load_plugin_ability", blocked_mount)
    receipt = await _invoke(tool, timeout_s=0)
    scope = tool.task_manager.scope_if_active("parent-session")
    cancelling = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert _runtime_rail(rail)._agent._load_records
        cancelling = asyncio.create_task(tool.task_manager.cancel(scope, receipt.data["pending_ids"]))
        await asyncio.sleep(0)
    finally:
        release.set()
        if cancelling is not None:
            await cancelling
        await tool.close_task_groups("parent-session")
    assert len(model.calls) == (0 if during_mount else 1)
    _assert_clean(rail)


@pytest.mark.asyncio
async def test_real_task_interrupt_resume_keeps_one_mount_and_child(tmp_path, monkeypatch):
    directory = _skill(tmp_path / "expert", "required", "恢复后真实技能正文")
    model = ScriptedModel(_call("skill_tool", skill_name="required"), AssistantMessage(content="恢复完成"))
    rail = DynamicExpertSkillRail(public_skills=[], expert_skills=[_resolved(directory)])
    parent = await _parent(tmp_path)
    tool = await _bind(parent, _spec(model, rail, extra_rails=[InterruptSkillOnce()]))
    original_load = DeepAgent.load_plugin_ability
    load_ids = []

    async def observe_load(agent, **kwargs):
        record = await original_load(agent, **kwargs)
        load_ids.append(record.load_id)
        return record

    monkeypatch.setattr(DeepAgent, "load_plugin_ability", observe_load)
    interrupted = await _invoke(tool)
    assert interrupted["result_type"] == "interrupt"
    child = _runtime_rail(rail)._agent
    record_id = _runtime_rail(rail).load_record.load_id
    scope = tool.task_manager.scope_if_active("parent-session")
    assert len(tool.task_manager.interrupted_records(scope)) == 1
    assert child.ability_manager.get("skill_tool") is not None
    feedback = InteractiveInput()
    feedback.update(interrupted["interrupt_ids"][0], {"approved": True})

    result = await _invoke(tool, query=feedback)

    assert result.success and result.data["ready"][0]["output"] == "恢复完成"
    assert _runtime_rail(rail)._agent is child and load_ids == [record_id]
    assert tool.task_manager.interrupted_records(scope) == []
    assert "恢复后真实技能正文" in "\n".join(_tool_messages(model))
    _assert_clean(rail)


@pytest.mark.asyncio
async def test_production_sandbox_can_read_selected_skills_without_widening_regular_file_access(
    tmp_path, production_sandbox_enabled,
):
    expert = _package(tmp_path / "experts_cache", "sandboxed", "沙箱内获准的专家正文")
    own = expert.package_dir / "skills" / "private"
    (own / "references").mkdir()
    (own / "references" / "guide.txt").write_text("获准的相对技能资源", encoding="utf-8")
    forbidden = tmp_path / "outside-workspace.txt"
    forbidden.write_text("不得读取的包外秘密", encoding="utf-8")
    (own / "escape.txt").symlink_to(forbidden)
    _skill(tmp_path / "public", "shared", "沙箱内获准的公共正文")
    parent_rail = SkillUseRail(skills_dir=str(tmp_path / "public"))
    parent = await _parent(tmp_path, public_rail=parent_rail)
    # The parent snapshot is already prepared. Match the production adapter's
    # sandbox.enabled policy before constructing the actual expert child.
    parent.deep_config.restrict_to_work_dir = True
    parent.deep_config.sys_operation._run_config.restrict_to_sandbox = True
    parent_roots = copy.deepcopy(parent.deep_config.sys_operation._run_config.sandbox_root)
    requests = [
        _call("skill_tool", skill_name="private"),
        _call("skill_tool", skill_name="shared"),
        _call("skill_tool", skill_name="private", relative_file_path="references/guide.txt"),
        _call("read_file", file_path=str(forbidden)),
        _call("read_file", file_path=str(own / "SKILL.md")),
        _call("skill_tool", skill_name="private", relative_file_path="escape.txt"),
    ]
    normal_roots = []

    def inspect_operations(_messages, **_kwargs):
        runtime = _runtime_rail(rail)
        child = runtime._agent
        operation = child.deep_config.sys_operation
        assert operation._run_config.restrict_to_sandbox is True
        assert runtime.sys_operation is not operation
        assert Runner.resource_mgr.get_sys_operation(runtime.sys_operation.id) is None
        read_tool = Runner.resource_mgr.get_tool(child.ability_manager.get("read_file").id)
        bash_tool = Runner.resource_mgr.get_tool(child.ability_manager.get("bash").id)
        assert read_tool.operation is operation
        assert bash_tool._operation is operation
        normal_roots.append(copy.deepcopy(operation._run_config.sandbox_root))
        return requests[0]

    model = ScriptedModel(inspect_operations, *requests[1:], AssistantMessage(content="沙箱任务完成"))
    spec, rail = _production_spec(parent, expert, model)
    assert spec.restrict_to_work_dir is True
    assert "你是测试专家" in spec.system_prompt
    result = await _invoke(await _bind(parent, spec), name=spec.agent_card.name)

    assert result.success and result.data["ready"][0]["output"] == "沙箱任务完成"
    assert "你是测试专家" in str(model.calls[0]["messages"])
    assert str(own) in "\n".join(_tool_messages(model))
    outputs = {
        message.tool_call_id: str(message.content)
        for message in model.calls[-1]["messages"] if getattr(message, "role", None) == "tool"
    }
    assert "沙箱内获准的专家正文" in outputs[requests[0].tool_calls[0].id]
    assert "沙箱内获准的公共正文" in outputs[requests[1].tool_calls[0].id]
    assert "获准的相对技能资源" in outputs[requests[2].tool_calls[0].id]
    for request in requests[3:]:
        output = outputs[request.tool_calls[0].id]
        assert "outside sandbox" in output
        assert "不得读取的包外秘密" not in output
        assert "沙箱内获准的专家正文" not in output
    assert _runtime_rail(rail)._agent.deep_config.sys_operation._run_config.sandbox_root == normal_roots[0]
    assert parent.deep_config.sys_operation._run_config.sandbox_root == parent_roots
    assert parent.deep_config.sys_operation._run_config.restrict_to_sandbox is True
    _assert_clean(rail)
    with pytest.raises(PermissionError):
        rail.sys_operation.fs()
    await rail.close()


@pytest.mark.asyncio
async def test_mcp_cleanup_keeps_shared_resources_and_releases_child_owner(monkeypatch):
    """只检查 Swarm 的资源归属策略，MCP 连接协议由 Core 测试负责。"""
    tags = {"shared": {"parent", "child"}, "shared-tool": {"parent", "child"},
            "private": {"child"}, "private-tool": {"child"}}
    ok = SimpleNamespace(is_err=lambda: False)

    def remove_tag(resource_id, owner_id, **_kwargs):
        tags[resource_id].discard(owner_id)
        return ok

    disconnect = AsyncMock(return_value=ok)
    rail = DynamicExpertSkillRail(
        public_skills=[], expert_skills=[], runtime_owner_id="child", runtime_mcp_ids=["shared", "private"],
    )
    # Restore the real manager before the Runner fixture shuts down.
    with monkeypatch.context() as patch:
        manager = Runner.resource_mgr
        patch.setattr(manager, "get_mcp_server_config", lambda _id: object())
        patch.setattr(manager, "get_mcp_tool_ids", lambda server_id: [f"{server_id}-tool"])
        patch.setattr(manager, "get_resource_tag", lambda resource_id: tags[resource_id])
        patch.setattr(manager, "remove_resource_tag", remove_tag)
        patch.setattr(manager, "remove_mcp_server", disconnect)
        await rail.close()

    assert tags == {"shared": {"parent"}, "shared-tool": {"parent"}, "private": set(), "private-tool": set()}
    disconnect.assert_awaited_once_with("private", skip_if_tag_not_exists=True, ignore_exception=True)
    with pytest.raises(RuntimeError, match="ended"):
        await rail.prepare()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["continue", "summon"])
async def test_real_deep_agent_no_plan_choice_continues_or_finishes_normally(tmp_path, monkeypatch, decision):
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.symphony.expert_confirmation import DynamicExpertConfirmationRail, confirmation_request_id

    parent = await _parent(tmp_path)
    expert = _package(tmp_path / "experts_cache", "researcher")
    model = parent.deep_config.model
    model.responses.extend([
        _call(expert_team.PLAN_TOOL_NAME, query="请专家整理资料"),
        AssistantMessage(content=json.dumps({"status": "no_plan"})),
    ])
    if decision == "continue":
        model.responses.append(AssistantMessage(content="小艺Work继续完成原任务"))
    scan = AsyncMock(return_value={expert.id: expert})
    monkeypatch.setattr(expert_team, "scan_local_experts", scan)
    spec, _ = _production_spec(parent, expert, model)
    monkeypatch.setattr(expert_team, "_subagent_config", lambda *_: spec)
    session = Session(session_id=f"real-no-plan-{decision}")
    emitted = AsyncMock()
    monkeypatch.setattr(session, "write_stream", emitted)
    parent._loop_session = session
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = parent
    round_state = expert_team.DynamicExpertRound(parent, "请专家整理资料", model, session_id=session.get_session_id())
    adapter._dynamic_expert_rounds = {session.get_session_id(): round_state}
    provider = adapter._dynamic_expert_rounds.get
    await apply_extension_hot(parent, ExtensionParts(
        tools=[expert_team.ExpertPlanTool(provider)],
        rails=[expert_team.DynamicExpertRail(provider), DynamicExpertConfirmationRail(provider),
               JiuSwarmStreamEventRail()],
    ))
    try:
        paused = await asyncio.wait_for(parent.invoke({"query": "请专家整理资料"}, session=session), 10)
        assert paused["result_type"] == "interrupt" and len(model.calls) == 2
        request_id = confirmation_request_id(round_state._confirmation)
        assert request_id.startswith("symphony-no-plan:")
        inputs = {"query": JiuWenSwarm._build_interactive_input_from_answers(
            request_id, [{"selected_options": [decision]}], source="confirm_interrupt",
        )}
        assert adapter._prepare_confirmation_resume(session.get_session_id(), {"request_id": request_id}, inputs)
        result = await asyncio.wait_for(parent.invoke(inputs, session=session), 10)
        assert result["result_type"] == "answer"
        assert result["output"] == (
            "小艺Work继续完成原任务" if decision == "continue"
            else "这次先停在这里。召唤更多专家后，你可以发起新任务。"
        )
        assert len(model.calls) == (3 if decision == "continue" else 2)
        assert round_state.response["decision"] == decision and not parent.deep_config.subagents
        tool_results = [call.args[0].payload["tool_result"] for call in emitted.await_args_list
                        if getattr(call.args[0], "type", None) == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0]["raw_output"] == round_state.response
        assert tool_results[0]["success"] is True
        scan.assert_awaited_once()
    finally:
        await adapter._close_dynamic_expert_round(session.get_session_id())


@pytest.mark.asyncio
@pytest.mark.parametrize("work_mode", ["work", "code", "design"])
async def test_plan_approval_precedes_expert_planning_and_handoff(tmp_path, monkeypatch, work_mode):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
    from jiuwenswarm.server.runtime.session import session_metadata
    from jiuwenswarm.symphony.expert_confirmation import DynamicExpertConfirmationRail, confirmation_request_id

    public_root = tmp_path / "public"
    public_root.mkdir()
    parent = await _parent(tmp_path, public_rail=SkillUseRail(skills_dir=str(public_root), skill_mode="all"))
    adapter = JiuWenSwarmDeepAdapter() if work_mode == "work" else JiuwenSwarmCodeAdapter()
    adapter._static_prompt_profile = work_mode
    session = Session(session_id=f"plan-to-expert-{work_mode}")
    adapter._instance = parent
    adapter.mark_as_session_scoped(session.get_session_id())
    parent._loop_session = session
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    session_metadata.update_session_metadata(session_id=session.get_session_id(), work_mode=work_mode, sync=True)
    expert = _package(tmp_path / "experts", "planner", with_tool=True)
    scan = AsyncMock(return_value={expert.id: expert})
    monkeypatch.setattr(expert_team, "scan_local_experts", scan)
    model = parent.deep_config.model
    round_state = expert_team.DynamicExpertRound(
        parent, "请产品内的专家完成原任务", model, session_id=session.get_session_id(),
        handoff_expert=adapter._handoff_confirmed_expert,
    )
    adapter._dynamic_expert_rounds = {session.get_session_id(): round_state}
    def provider(sid):
        return adapter._dynamic_expert_rounds.get(sid)
    mode_rail = (adapter._build_work_agent_mode_rail() if work_mode == "work"
                 else adapter._build_agent_mode_rail())
    approval_rail = adapter._build_work_plan_approval_rail()
    tool = expert_team.ExpertPlanTool(provider)
    confirmation_rail = DynamicExpertConfirmationRail(provider)
    await apply_extension_hot(parent, ExtensionParts(
        tools=[tool], rails=[mode_rail, approval_rail, expert_team.DynamicExpertRail(provider), confirmation_rail],
    ))
    parent.switch_mode(session, "plan")
    exit_call = _call("exit_plan_mode")
    model.responses.extend([_call(expert_team.PLAN_TOOL_NAME, query=round_state.query), exit_call])
    try:
        interrupted = await parent.invoke({"query": round_state.query}, session=session)
        assert interrupted["result_type"] == "interrupt"
        assert interrupted["interrupt_ids"] == [exit_call.tool_calls[0].id]
        assert all(expert_team.PLAN_TOOL_NAME not in {tool.name for tool in call["tools"]} for call in model.calls)
        assert parent.load_state(session).plan_mode.mode == "plan"
        assert not round_state.attempted and adapter._current_expert_id is None
        scan.assert_not_awaited()
        assert (await tool.invoke({}, session=session))["reason"] == "expert_plan_unavailable"

        # The normal approval tool actually restores state before the planner is visible.
        model.responses.extend([
            _call(expert_team.PLAN_TOOL_NAME, query=round_state.query),
            AssistantMessage(content=json.dumps({
                "status": "ready", "nodes": [{"id": expert.id, "task": "完成原任务"}], "edges": [],
            })),
        ])
        approved = InteractiveInput()
        approved.update(exit_call.tool_calls[0].id, {"approved": True})
        waiting = await parent.invoke({"query": approved}, session=session)
        assert waiting["result_type"] == "interrupt"
        assert parent.load_state(session).plan_mode.mode == "normal"
        assert round_state.pending_plan is not None and adapter._current_expert_id is None
        scan.assert_awaited_once()
        assert expert_team.PLAN_TOOL_NAME in {tool.name for tool in model.calls[2]["tools"]}

        # A stale confirmation cannot load the expert if the session has returned to plan mode.
        parent.switch_mode(session, "plan")
        from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        rejected = await confirmation_rail.resolve_interrupt(
            AgentCallbackContext(agent=parent, session=session),
            _call(expert_team.PLAN_TOOL_NAME).tool_calls[0], {"approved": True},
        )
        assert isinstance(rejected, RejectResult) and adapter._current_expert_id is None
        parent.restore_mode_after_plan_exit(session)
        request_id = confirmation_request_id(round_state._confirmation)
        inputs = {"query": JiuWenSwarm._build_interactive_input_from_answers(
            request_id, [{"selected_options": ["交给这位专家"]}], source="confirm_interrupt",
        )}
        assert adapter._prepare_confirmation_resume(session.get_session_id(), {"request_id": request_id}, inputs)
        model.responses.extend([_call("expert_evidence"), AssistantMessage(content="原任务已完成")])
        result = await parent.invoke(inputs, session=session)
        assert result["output"] == "原任务已完成" and adapter._current_expert_id == expert.id
        assert round_state.response["status"] == "handed_off"
        assert round_state.query == "请产品内的专家完成原任务"
        assert "已取得专家工具证据" in "\n".join(_tool_messages(model))
    finally:
        await adapter._close_dynamic_expert_round(session.get_session_id())
        await adapter._apply_expert(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("work_mode", ["work", "code", "design"])
@pytest.mark.parametrize("child_approval", [False, True])
async def test_confirmed_multi_experts_execute_real_dependency_tasks_and_keep_host_tools(tmp_path, monkeypatch, work_mode, child_approval):
    from openjiuwen.core.foundation.tool import Tool, ToolCard
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
    from jiuwenswarm.symphony.expert_confirmation import DynamicExpertConfirmationRail, confirmation_request_id

    class CompetingAgent(Tool):
        def __init__(self):
            super().__init__(ToolCard(id="competing_agent", name="Agent", description="自定义代码代理"))
            self.called = False

        async def invoke(self, inputs, **kwargs):
            self.called = True
            return {"output": "绕过专家图"}

        async def stream(self, inputs, **kwargs):
            raise NotImplementedError

    public_root = tmp_path / "public"
    _skill(public_root, "host-skill", "宿主公共技能正文")
    parent = await _parent(tmp_path, public_rail=SkillUseRail(skills_dir=str(public_root), skill_mode="all"))
    adapter = JiuWenSwarmDeepAdapter() if work_mode == "work" else JiuwenSwarmCodeAdapter()
    adapter._static_prompt_profile = work_mode
    adapter._instance = parent
    session = Session(session_id=f"multi-{work_mode}-{uuid4().hex}")
    adapter.mark_as_session_scoped(session.get_session_id())
    parent._loop_session = session
    experts = {key: _package(tmp_path / "experts", key, f"{key} 私有技能正文", with_tool=True)
               for key in ("researcher", "reviewer")}
    model = parent.deep_config.model
    specs = {key: _production_spec(parent, expert, model)[0] for key, expert in experts.items()}
    if child_approval:
        specs["researcher"].rails.append(InterruptSkillOnce())
    monkeypatch.setattr(expert_team, "scan_local_experts", AsyncMock(return_value=experts))
    monkeypatch.setattr(expert_team, "_subagent_config", lambda _parent, expert, _model: specs[expert.id])
    round_state = expert_team.DynamicExpertRound(parent, "请专家合作完成报告", model, session_id=session.get_session_id())
    adapter._dynamic_expert_rounds = {session.get_session_id(): round_state}
    def provider(sid):
        return adapter._dynamic_expert_rounds.get(sid)
    competing = CompetingAgent()
    mode_rail = (adapter._build_work_agent_mode_rail() if work_mode == "work"
                 else adapter._build_agent_mode_rail())
    await apply_extension_hot(parent, ExtensionParts(
        tools=[expert_team.ExpertPlanTool(provider), competing],
        rails=[mode_rail, expert_team.DynamicExpertRail(provider), DynamicExpertConfirmationRail(provider),
               JiuSwarmStreamEventRail()],
    ))
    model.responses.extend([
        _call(expert_team.PLAN_TOOL_NAME, query=round_state.query),
        AssistantMessage(content=json.dumps({
            "status": "ready", "nodes": [{"id": key, "task": f"{key} 的任务"} for key in experts],
            "edges": [{"source": "researcher", "target": "reviewer"}],
        })),
    ])
    original_workspace = parent.deep_config.workspace
    try:
        waiting = await parent.invoke({"query": round_state.query}, session=session)
        assert waiting["result_type"] == "interrupt" and not parent.deep_config.subagents
        request_id = confirmation_request_id(round_state._confirmation)
        inputs = {"query": JiuWenSwarm._build_interactive_input_from_answers(
            request_id, [{"selected_options": ["请他们一起做"]}], source="confirm_interrupt",
        )}
        assert adapter._prepare_confirmation_resume(session.get_session_id(), {"request_id": request_id}, inputs)
        model.responses.extend([
            _call("Agent", prompt="尝试绕过专家计划"),
            _call("task_tool", action="dispatch", tasks=[{"subagent_type": expert_team._subagent_type("reviewer"), "task_description": "提前执行"}]),
            _call("task_tool", action="dispatch", tasks=[{"subagent_type": expert_team._subagent_type("researcher"), "task_description": "调研"}]),
            _call("skill_tool", skill_name="private"), _call("skill_tool", skill_name="host-skill"),
            _call("expert_evidence"), AssistantMessage(content="调研发现：关键事实"),
            _call("task_tool", action="dispatch", tasks=[{"subagent_type": expert_team._subagent_type("reviewer"), "task_description": "复核"}]),
            _call("skill_tool", skill_name="private"), AssistantMessage(content="复核已确认关键事实"),
            AssistantMessage(content="专家报告完成"),
        ])
        result = await asyncio.wait_for(parent.invoke(inputs, session=session), 15)
        if child_approval:
            assert result["result_type"] == "interrupt"
            assert round_state._states["researcher"] == "interrupted"
            feedback = InteractiveInput()
            feedback.update(result["interrupt_ids"][0], {"approved": True})
            result = await asyncio.wait_for(parent.invoke({"query": feedback}, session=session), 15)
        assert result.get("result_type") == "answer", result
        assert result["output"] == "专家报告完成"
        assert not competing.called
        assert round_state.response["status"] == "ready"
        assert round_state._states == {"researcher": "completed", "reviewer": "completed"}
        assert round_state._completed == {"researcher": "调研发现：关键事实", "reviewer": "复核已确认关键事实"}
        messages = "\n".join(_tool_messages(model))
        assert "前置专家" in messages and "动态专家协作须" in messages
        assert "researcher 私有技能正文" in messages and "reviewer 私有技能正文" in messages
        assert "宿主公共技能正文" in messages and "已取得专家工具证据" in messages
        assert parent.deep_config.workspace is original_workspace
        assert adapter._current_expert_id is None
        assert all("Agent" not in {tool.name for tool in call.get("tools", [])}
                   for call in model.calls[2:])
    finally:
        await adapter._close_dynamic_expert_round(session.get_session_id())
    assert not parent.deep_config.subagents and parent.ability_manager.get("Agent") is not None
    assert "host-skill" in {skill.name for skill in parent.find_rails_by_type((SkillUseRail,))[0].get_skills_for_session()}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["Agent", "sessions_spawn", "task_tool"])
@pytest.mark.parametrize("planner_first", [False, True])
@pytest.mark.parametrize("no_plan", [False, True])
@pytest.mark.parametrize("competing_hitl", [False, True])
async def test_planning_batch_blocks_parallel_delegation_then_restores_normal_use(
    tmp_path, monkeypatch, tool_name, planner_first, no_plan, competing_hitl,
):
    from openjiuwen.core.foundation.tool import Tool, ToolCard
    from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmInterruptRail
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
    from jiuwenswarm.symphony.expert_confirmation import DynamicExpertConfirmationRail, confirmation_request_id

    class ApprovalBeforeExpertGuard(ConfirmInterruptRail):
        priority = 100

    executed = []

    class DelegationTool(Tool):
        def __init__(self):
            super().__init__(ToolCard(id=f"test_{tool_name}", name=tool_name, description="普通任务委派"))

        async def invoke(self, inputs, **kwargs):
            executed.append(inputs)
            return {"output": "普通委派已完成"}

        async def stream(self, inputs, **kwargs):
            raise NotImplementedError

    parent = await _parent(tmp_path)
    adapter = JiuwenSwarmCodeAdapter()
    adapter._instance = parent
    session = Session(session_id=f"parallel-expert-{uuid4().hex}")
    adapter.mark_as_session_scoped(session.get_session_id())
    parent._loop_session = session
    model = parent.deep_config.model
    round_state = expert_team.DynamicExpertRound(
        parent, "请产品内专家处理任务", model, session_id=session.get_session_id(),
    )
    adapter._dynamic_expert_rounds = {session.get_session_id(): round_state}
    def provider(sid):
        return adapter._dynamic_expert_rounds.get(sid)
    await apply_extension_hot(parent, ExtensionParts(
        tools=[expert_team.ExpertPlanTool(provider), DelegationTool()],
        rails=[adapter._build_agent_mode_rail(), expert_team.DynamicExpertRail(provider),
               DynamicExpertConfirmationRail(provider), JiuSwarmStreamEventRail()]
              + ([ApprovalBeforeExpertGuard(tool_names=[tool_name])] if competing_hitl else []),
    ))
    expert = expert_team.LocalExpert("expert-a", "专家", "整理资料", object())
    monkeypatch.setattr(expert_team, "scan_local_experts", AsyncMock(return_value={expert.id: expert}))
    monkeypatch.setattr(expert_team, "_subagent_config", lambda *_: SimpleNamespace(rails=[]))
    started, release = asyncio.Event(), asyncio.Event()

    async def plan_response(*_args, **_kwargs):
        started.set()
        await release.wait()
        return AssistantMessage(content=json.dumps(
            {"status": "no_plan"} if no_plan else {
                "status": "ready", "nodes": [{"id": expert.id, "task": "整理资料"}], "edges": [],
            },
        ))

    planner_call = _call(expert_team.PLAN_TOOL_NAME, query=round_state.query)
    competing_call = _call(tool_name, subagent_type="general-purpose", task_description="绕过专家规划")
    calls = [planner_call.tool_calls[0], competing_call.tool_calls[0]]
    model.responses.extend([AssistantMessage(content="", tool_calls=calls if planner_first else calls[::-1]), plan_response])
    invocation = asyncio.create_task(parent.invoke({"query": round_state.query}, session=session))
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert executed == []
        release.set()
        result = await asyncio.wait_for(invocation, 10)
        assert result["result_type"] == "interrupt"
        assert executed == [] and round_state.plan is None
        if competing_hitl:
            assert competing_call.tool_calls[0].id in result["interrupt_ids"]

        request_id = confirmation_request_id(round_state._confirmation)
        choice = "小艺Work继续" if no_plan else "还是小艺Work来做"
        inputs = {"query": JiuWenSwarm._build_interactive_input_from_answers(
            request_id, [{"selected_options": [choice]}], source="confirm_interrupt",
        )}
        assert adapter._prepare_confirmation_resume(session.get_session_id(), {"request_id": request_id}, inputs)
        if competing_hitl:
            # Even remembered permission for a competing call cannot bypass the
            # original batch guard when both interrupted tools resume together.
            inputs["query"].update(competing_call.tool_calls[0].id, {"approved": True, "auto_confirm": True})
        # Providers may reuse a call ID in a later response. Only the original
        # batch is blocked; ordinary delegation remains available after decline/no_plan.
        ordinary_call = _call(tool_name, subagent_type="general-purpose", task_description="普通任务")
        ordinary_call.tool_calls[0].id = competing_call.tool_calls[0].id
        model.responses.extend([ordinary_call, AssistantMessage(content="原任务已完成")])
        continued = await asyncio.wait_for(parent.invoke(inputs, session=session), 10)
        assert continued["output"] == "原任务已完成"
        assert len(executed) == 1 and executed[0]["task_description"] == "普通任务"
    finally:
        release.set()
        await asyncio.gather(invocation, return_exceptions=True)
        await adapter._close_dynamic_expert_round(session.get_session_id())
