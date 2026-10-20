"""动态专家的真实 TaskTool / DeepAgent / 技能工具执行和资源生命周期。"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from pathlib import Path
from uuid import uuid4

import pytest

from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    AssistantMessageChunk,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
)
from openjiuwen.core.foundation.tool import McpServerConfig, McpToolCard, Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr
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


async def _parent(tmp_path, *, public_rail=None, card_id=None, tools=None, mcps=None):
    parent = create_deep_agent(
        model=ScriptedModel(),
        card=AgentCard(id=card_id or uuid4().hex, name="parent", description="测试主代理"),
        workspace=str(tmp_path / "workspace"),
        tools=tools,
        mcps=mcps,
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
    return tool


async def _invoke(tool, *, name="expert", session_id="parent-session", call_id="outer-call", query=None):
    args = {"subagent_type": name, "task_description": "执行专家节点"}
    if query is not None:
        args["query"] = query
    return await asyncio.wait_for(tool.invoke(
        args, session=Session(session_id=session_id), tool_call_id=call_id,
    ), timeout=10)


def _assert_clean(rail):
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


def _package(root, expert_id, body="专家正文", *, tools=(), rails=(), mcps=(), files=None):
    package = root / expert_id
    (package / "persona").mkdir(parents=True)
    (package / "persona" / "AGENT.md").write_text("你是测试专家。", encoding="utf-8")
    _skill(package / "skills", "private", body)
    for relative_path, content in (files or {}).items():
        path = package / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    manifest = package / "manifest.json"
    manifest.write_text(json.dumps({
        "packageType": "agent_template",
        "agentCard": {"id": expert_id, "name": expert_id, "description": "测试专家"},
        "persona": {"dir": "persona"}, "skills": [{"dir": "skills/private"}],
        "tools": list(tools), "rails": list(rails), "mcps": list(mcps),
    }), encoding="utf-8")
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


class OwnedTestTool(Tool):
    def __init__(self, name="template_owned"):
        super().__init__(ToolCard(
            id=name, name=name, description="测试有状态工具", stateless=False,
            input_params={"type": "object", "properties": {}},
        ))

    async def invoke(self, inputs, **kwargs):
        return f"{self.card.name} 实际执行"

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class OfflineMcpClient:
    """Only the MCP transport is replaced; Core owns registration and tools."""

    def __init__(self, config):
        self.config = config
        self.connections = 0
        self.disconnections = 0
        self.calls = []

    async def connect(self):
        self.connections += 1
        return True

    async def disconnect(self):
        self.disconnections += 1

    async def list_tools(self):
        return [McpToolCard(
            id="echo", name="echo", description="离线 MCP 回声工具",
            input_params={"type": "object", "properties": {}},
            server_id=self.config.server_id, server_name=self.config.server_name,
        )]

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return f"{self.config.server_name} MCP 实际执行"


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

    assert result.success and result.data["output"] == "完成"
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
@pytest.mark.parametrize("public", [False, True])
async def test_expert_without_own_skills_can_run_with_empty_or_public_view(tmp_path, public):
    parent_rail = None
    responses = []
    if public:
        _skill(tmp_path / "public", "available", "公共正文")
        parent_rail = SkillUseRail(skills_dir=str(tmp_path / "public"))
        responses.append(_call("skill_tool", skill_name="available"))
    parent = await _parent(tmp_path, public_rail=parent_rail)
    model = ScriptedModel(*responses, AssistantMessage(content="完成"))
    rail = DynamicExpertSkillRail(public_skills=snapshot_public_skills(parent), expert_skills=[])

    assert (await _invoke(await _bind(parent, _spec(model, rail)))).success
    assert len(model.calls) == (2 if public else 1)
    _assert_clean(rail)


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["missing", "empty", "invalid_yaml", "bind_error", "partial_bind_error"])
async def test_missing_or_unmountable_skill_stops_before_model(tmp_path, monkeypatch, broken):
    directory = tmp_path / "expert" / "required"
    if broken != "missing":
        directory.mkdir(parents=True)
    if broken in {"invalid_yaml", "bind_error", "partial_bind_error"}:
        _skill(directory.parent, directory.name)
    if broken == "invalid_yaml":
        (directory / "SKILL.md").write_text("---\ndescription: [broken\n---\n", encoding="utf-8")
    model = ScriptedModel(AssistantMessage(content="不应调用"))
    rail = DynamicExpertSkillRail(public_skills=[], expert_skills=[_resolved(directory)])
    if broken == "partial_bind_error":
        second = _skill(directory.parent, "second")
        rail = DynamicExpertSkillRail(public_skills=[], expert_skills=[_resolved(directory), _resolved(second)])
        original_reload = rail.reload_skills
        reload_count = 0

        async def fail_second_bind():
            nonlocal reload_count
            reload_count += 1
            if reload_count == 2:
                raise RuntimeError("second skill bind failed")
            await original_reload()

        monkeypatch.setattr(rail, "reload_skills", fail_second_bind)
    parent = await _parent(tmp_path)
    tool = await _bind(parent, _spec(model, rail))
    if broken == "bind_error":
        async def fail_bind(*_args, **_kwargs):
            raise RuntimeError("skill bind failed")
        monkeypatch.setattr("openjiuwen.harness.deep_agent.DeepAgent.load_plugin_ability", fail_bind)

    with pytest.raises(Exception):
        await _invoke(tool)
    assert model.calls == []
    _assert_clean(rail)


@pytest.mark.asyncio
async def test_public_skill_snapshot_excludes_later_discovery_and_parent_mutations(tmp_path):
    _skill(tmp_path / "public", "visible", "快照中的公共正文")
    public_rail = SkillUseRail(skills_dir=str(tmp_path / "public"))
    parent = await _parent(tmp_path, public_rail=public_rail)
    snapshot = snapshot_public_skills(parent)
    assert snapshot[0] is not public_rail.skills_meta[0]
    public_rail.skills[0].description = "父代理后来修改"
    _skill(tmp_path / "public", "later", "后来发现的正文")
    await public_rail.reload_skills()
    assert {skill.name for skill in public_rail.skills_meta} == {"visible", "later"}
    assert snapshot[0].description != "父代理后来修改"
    model = ScriptedModel(
        _call("list_skill"), _call("skill_tool", skill_name="visible"),
        _call("skill_tool", skill_name="later"), AssistantMessage(content="完成"),
    )
    rail = DynamicExpertSkillRail(public_skills=snapshot, expert_skills=[])

    assert (await _invoke(await _bind(parent, _spec(model, rail)))).success
    messages = _tool_messages(model)
    assert "later" not in messages[0]
    assert "快照中的公共正文" in "\n".join(messages)
    assert "后来发现的正文" not in "\n".join(messages)
    assert "Skill not found: later" in "\n".join(messages)
    _assert_clean(rail)


@pytest.mark.asyncio
async def test_deleted_public_snapshot_skill_blocks_model(tmp_path):
    directory = _skill(tmp_path / "public", "visible")
    public_rail = SkillUseRail(skills_dir=str(directory.parent))
    parent = await _parent(tmp_path, public_rail=public_rail)
    rail = DynamicExpertSkillRail(public_skills=snapshot_public_skills(parent), expert_skills=[])
    (directory / "SKILL.md").unlink()
    model = ScriptedModel(AssistantMessage(content="不应调用"))

    with pytest.raises(Exception, match="Public skill is no longer available"):
        await _invoke(await _bind(parent, _spec(model, rail)))
    assert model.calls == []
    _assert_clean(rail)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_expert", [False, True])
async def test_concurrent_experts_and_same_expert_sessions_have_independent_tools(tmp_path, same_expert):
    entered = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    def first_response(index):
        async def wait_for_other(_messages, **_kwargs):
            entered[index].set()
            await release.wait()
            return _call("skill_tool", skill_name="private")
        return wait_for_other

    models = [ScriptedModel(first_response(index), AssistantMessage(content=f"完成{index}")) for index in range(2)]
    first_expert = _package(tmp_path / "cache", "first", "专家甲正文")
    experts = [first_expert, first_expert if same_expert else _package(tmp_path / "cache", "second", "专家乙正文")]
    first_parent = await _parent(tmp_path / "a", card_id="shared-parent")
    parents = [first_parent, await _parent(tmp_path / "b", card_id="shared-parent") if same_expert else first_parent]
    configured = [_production_spec(parents[index], experts[index], models[index]) for index in range(2)]
    specs, rails = zip(*configured)
    assert specs[0].agent_card.id != specs[1].agent_card.id
    assert specs[0].factory_kwargs["tool_owner_id"] != specs[1].factory_kwargs["tool_owner_id"]
    tools = [await _bind(parents[index], specs[index]) for index in range(2)]
    tasks = [asyncio.create_task(_invoke(
        tools[index], name=specs[index].agent_card.name, session_id=f"session-{index}",
    )) for index in range(2)]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), timeout=10)
        cards = [rail._agent.ability_manager.get("skill_tool") for rail in rails]
        assert cards[0] is not cards[1] and cards[0].id != cards[1].id
        assert Runner.resource_mgr.get_tool(cards[0].id) is not Runner.resource_mgr.get_tool(cards[1].id)
        assert rails[0]._agent.card.id == specs[0].agent_card.id
        assert rails[1]._agent.card.id == specs[1].agent_card.id
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert all(result.success for result in results)
    for index, model in enumerate(models):
        expected = "专家甲正文" if index == 0 or same_expert else "专家乙正文"
        assert expected in "\n".join(_tool_messages(model))
        if not same_expert:
            other = "专家乙正文" if index == 0 else "专家甲正文"
            assert other not in "\n".join(_tool_messages(model))
        _assert_clean(rails[index])
    assert all(parent.card.id == "shared-parent" for parent in parents)


@pytest.mark.asyncio
async def test_real_model_failure_releases_skill_load(tmp_path):
    directory = _skill(tmp_path / "expert", "required")
    model = ScriptedModel(RuntimeError("scripted model failed"))
    rail = DynamicExpertSkillRail(public_skills=[], expert_skills=[_resolved(directory)])
    parent = await _parent(tmp_path)
    with pytest.raises(Exception, match="scripted model failed"):
        await _invoke(await _bind(parent, _spec(model, rail)))
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
    task = asyncio.create_task(_invoke(tool))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert rail._agent._load_records
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
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
    child = rail._agent
    record_id = rail.load_record.load_id
    assert len(tool._pending_subagents) == 1
    assert child.ability_manager.get("skill_tool") is not None
    feedback = InteractiveInput()
    feedback.update(interrupted["interrupt_ids"][0], {"approved": True})

    result = await _invoke(tool, query=feedback)

    assert result.success and result.data["output"] == "恢复完成"
    assert rail._agent is child and load_ids == [record_id]
    assert tool._pending_subagents == {}
    assert "恢复后真实技能正文" in "\n".join(_tool_messages(model))
    _assert_clean(rail)


@pytest.mark.asyncio
async def test_cleanup_restores_config_when_skill_file_disappears_after_mount(tmp_path):
    directory = _skill(tmp_path / "expert", "required")

    def remove_skill(_messages, **_kwargs):
        (directory / "SKILL.md").unlink()
        return AssistantMessage(content="完成")

    model = ScriptedModel(remove_skill)
    rail = DynamicExpertSkillRail(public_skills=[], expert_skills=[_resolved(directory)])
    parent = await _parent(tmp_path)
    assert (await _invoke(await _bind(parent, _spec(model, rail)))).success
    _assert_clean(rail)


@pytest.mark.asyncio
async def test_production_sandbox_can_read_selected_skills_without_widening_regular_file_access(
    tmp_path, production_sandbox_enabled,
):
    expert = _package(
        tmp_path / "experts_cache", "sandboxed", "沙箱内获准的专家正文",
        rails=[{"file": "rails/files.py", "class": "PackageFileRail"}],
        files={"rails/files.py": (
            "from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail\n"
            "class PackageFileRail(SysOperationRail):\n"
            "    def __init__(self):\n"
            "        super().__init__(read_only=True, enable_read_image_multimodal=False)\n"
        )},
    )
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
        child = rail._agent
        operation = child.deep_config.sys_operation
        assert operation._run_config.restrict_to_sandbox is True
        assert rail.sys_operation is not operation
        assert Runner.resource_mgr.get_sys_operation(rail.sys_operation.id) is None
        read_tool = Runner.resource_mgr.get_tool(child.ability_manager.get("read_file").id)
        assert read_tool.operation is operation
        normal_roots.append(copy.deepcopy(operation._run_config.sandbox_root))
        return requests[0]

    model = ScriptedModel(inspect_operations, *requests[1:], AssistantMessage(content="沙箱任务完成"))
    spec, rail = _production_spec(parent, expert, model)
    assert spec.restrict_to_work_dir is True
    assert "你是测试专家" in spec.system_prompt
    result = await _invoke(await _bind(parent, spec), name=spec.agent_card.name)

    assert result.success and result.data["output"] == "沙箱任务完成"
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
    assert rail._agent.deep_config.sys_operation._run_config.sandbox_root == normal_roots[0]
    assert parent.deep_config.sys_operation._run_config.sandbox_root == parent_roots
    assert parent.deep_config.sys_operation._run_config.restrict_to_sandbox is True
    _assert_clean(rail)
    with pytest.raises(PermissionError):
        rail.sys_operation.fs()
    await rail.close()


@pytest.mark.asyncio
async def test_plain_skill_expert_keeps_real_read_file_and_bash_on_ordinary_operation(
    tmp_path, production_sandbox_enabled,
):
    expert = _package(tmp_path / "cache", "plain")
    parent = await _parent(tmp_path)
    parent.deep_config.restrict_to_work_dir = True

    def first_call(_messages, **_kwargs):
        child = rail._agent
        operation = child.deep_config.sys_operation
        assert operation._run_config.restrict_to_sandbox is True
        read_tool = Runner.resource_mgr.get_tool(child.ability_manager.get("read_file").id)
        bash_tool = Runner.resource_mgr.get_tool(child.ability_manager.get("bash").id)
        assert read_tool.operation is operation and bash_tool._operation is operation
        assert operation is not rail.sys_operation
        workspace = Path(child.deep_config.workspace.root_path)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "allowed.txt").write_text("普通工作区文件正文", encoding="utf-8")
        return _call("read_file", file_path=str(workspace / "allowed.txt"))

    model = ScriptedModel(
        first_call, _call("bash", command="echo dynamic-expert-shell-ok"),
        _call("skill_tool", skill_name="private"), AssistantMessage(content="缺省工具完成"),
    )
    spec, rail = _production_spec(parent, expert, model)
    assert spec.rails == [rail] and not spec.tools

    result = await _invoke(await _bind(parent, spec), name=spec.agent_card.name)

    assert result.success and result.data["output"] == "缺省工具完成"
    outputs = "\n".join(_tool_messages(model))
    assert "普通工作区文件正文" in outputs
    assert "dynamic-expert-shell-ok" in outputs
    assert "专家正文" in outputs
    _assert_clean(rail)
    await rail.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["completed", "interrupted", "init_failed"])
async def test_round_close_reclaims_child_resources_and_preserves_parent(tmp_path, monkeypatch, phase):
    """三种退出路径共用真实子代理，检查技能、工具、文件操作和 MCP 的归属。"""
    clients = {}

    def create_client(config):
        assert config.server_id not in clients
        client = clients[config.server_id] = OfflineMcpClient(config)
        return client

    monkeypatch.setattr(ToolMgr, "_create_client", staticmethod(create_client))
    shared_id, exclusive_id = f"shared_{uuid4().hex}", f"exclusive_{uuid4().hex}"
    shared_config = McpServerConfig(
        server_id=shared_id, server_name="shared", server_path="unused-test-mcp", client_type="stdio",
    )
    parent_tool = OwnedTestTool("parent_owned")
    parent = await _parent(tmp_path, tools=[parent_tool], mcps=[shared_config])
    parent_tool_id = parent_tool.card.id
    parent_operation = parent.deep_config.sys_operation
    expert = _package(
        tmp_path / "cache", "resource-owner",
        tools=[{"file": "tools/owned.py", "class": "OwnedTestTool"}],
        rails=[{"file": "rails/failing.py", "class": "FailBeforeSkillInit"}] if phase == "init_failed" else [],
        mcps=[{
            "server_id": exclusive_id, "server_name": "exclusive",
            "type": "stdio", "command": "unused-test-mcp",
        }],
        files={
            "tools/owned.py": (
                "from openjiuwen.core.foundation.tool import Tool, ToolCard\n\n" + inspect.getsource(OwnedTestTool)
            ),
            "rails/failing.py": (
                "from openjiuwen.core.single_agent.rail.base import AgentRail\n"
                "class FailBeforeSkillInit(AgentRail):\n"
                "    priority = 100000\n"
                "    def init(self, agent):\n"
                "        raise RuntimeError('template init exploded')\n"
            ),
        },
    )
    model = ScriptedModel(
        _call("template_owned"), _call("parent_owned"),
        _call("mcp_shared_echo"), _call("mcp_exclusive_echo"),
        _call("skill_tool", skill_name="private"), AssistantMessage(content="资源任务完成"),
    )

    async def scan():
        return {expert.id: expert}

    async def plan(*_args):
        return {"status": "ready", "graph": {
            "nodes": {expert.id: {"label": expert.name, "task": "执行资源节点"}}, "edges": [],
        }}

    monkeypatch.setattr(expert_team, "scan_local_experts", scan)
    monkeypatch.setattr(expert_team, "plan_experts", plan)
    round_state = expert_team.DynamicExpertRound(parent, "组织专家", model)
    assert (await round_state.compose("组织专家"))["success"]
    spec = parent.deep_config.subagents[0]
    spec.factory_kwargs.update({
        "enable_read_image_multimodal": False, "enable_security_rail": False,
        "enable_llm_retry_rail": False, "enable_tool_resilience_rail": False,
        "auto_create_workspace": False,
    })
    if phase == "interrupted":
        spec.rails.append(InterruptSkillOnce())
    rail = next(item for item in spec.rails if isinstance(item, DynamicExpertSkillRail))
    template_tools = [item for item in spec.tools if isinstance(item, Tool)]
    assert len(template_tools) == 1
    child_tool = template_tools[0]
    manager = Runner.resource_mgr
    task_tool = manager.get_tool(parent.ability_manager.get("task_tool").id)
    owner_id = spec.agent_card.id

    if phase == "init_failed":
        with pytest.raises(Exception, match="template init exploded"):
            await _invoke(task_tool, name=spec.agent_card.name)
        assert model.calls == [] and rail._agent is None
        operation_id = f"{spec.agent_card.name}_{owner_id}"
    else:
        result = await _invoke(task_tool, name=spec.agent_card.name)
        if phase == "interrupted":
            assert result["result_type"] == "interrupt" and rail.load_record is not None
            assert task_tool._pending_subagents
        else:
            assert result.success and result.data["output"] == "资源任务完成"
            _assert_clean(rail)
        child = rail._agent
        assert child.card.id == child.deep_config.tool_owner_id == owner_id
        assert child.ability_manager.get("parent_owned").id == parent_tool_id
        operation_id = child.deep_config.sys_operation.id
        assert manager.get_sys_operation(operation_id) is child.deep_config.sys_operation
        for name in ("template_owned", "parent_owned"):
            assert f"{name} 实际执行" in "\n".join(_tool_messages(model))
        assert clients[shared_id].calls == clients[exclusive_id].calls == [("echo", {})]

    assert child_tool.card.id == f"template_owned_{owner_id}"
    assert manager.get_tool(child_tool.card.id) is child_tool
    assert manager.get_sys_operation(operation_id) is not None
    operation_tools = manager.get_sys_op_tool_cards(operation_id)
    assert operation_tools and all(manager.get_tool(card.id) is not None for card in operation_tools)
    shared_tools = manager.get_mcp_tool_ids(shared_id)
    exclusive_tools = manager.get_mcp_tool_ids(exclusive_id)
    assert len(shared_tools) == len(exclusive_tools) == 1
    for resource_id in [shared_id, *shared_tools]:
        assert set(manager.get_resource_tag(resource_id)) == {parent.card.id, owner_id}
    for resource_id in [exclusive_id, *exclusive_tools]:
        assert set(manager.get_resource_tag(resource_id)) == {owner_id}
    assert all(client.connections == 1 and client.disconnections == 0 for client in clients.values())
    removed = []
    original_remove = manager.remove_mcp_server

    async def observe_remove(server_id=None, **kwargs):
        removed.append(server_id)
        return await original_remove(server_id, **kwargs)

    monkeypatch.setattr(manager, "remove_mcp_server", observe_remove)
    await round_state.close()
    await round_state.close()

    if phase != "init_failed":
        _assert_clean(rail)
        with pytest.raises(RuntimeError, match="ended"):
            await rail.prepare()
    assert not task_tool._pending_subagents and not parent.deep_config.subagents
    assert manager.get_tool(child_tool.card.id) is None
    assert manager.get_sys_operation(operation_id) is None
    assert all(manager.get_tool(card.id) is None for card in operation_tools)
    assert removed == [exclusive_id]
    assert clients[exclusive_id].disconnections == 1 and clients[shared_id].disconnections == 0
    assert manager.get_mcp_server_config(exclusive_id) is None
    for resource_id in [exclusive_id, *exclusive_tools]:
        assert manager.get_resource_tag(resource_id) is None
    assert all(manager.get_tool(tool_id) is None for tool_id in exclusive_tools)
    assert manager.get_mcp_server_config(shared_id) == shared_config
    for resource_id in [shared_id, *shared_tools]:
        assert set(manager.get_resource_tag(resource_id)) == {parent.card.id}
    assert (await manager.get_tool(shared_tools[0]).invoke({}))["result"] == "shared MCP 实际执行"
    assert manager.get_sys_operation(parent_operation.id) is parent_operation
    assert parent_tool.card.id == parent.ability_manager.get("parent_owned").id == parent_tool_id
    assert manager.get_tool(parent_tool_id) is parent_tool
    assert await parent_tool.invoke({}) == "parent_owned 实际执行"
