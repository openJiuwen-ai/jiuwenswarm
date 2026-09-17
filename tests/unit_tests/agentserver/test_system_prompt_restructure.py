import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openjiuwen.core.foundation.llm import Model, ToolMessage
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ModelCallInputs,
    ToolCallInputs,
)
from openjiuwen.core.single_agent.ability_manager import AbilityExecutionError
from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
from openjiuwen.harness.prompts.prompt_attachment_manager import (
    PromptAttachmentManager,
)
from openjiuwen.harness.prompts import PromptSection, SystemPromptBuilder

from jiuwenswarm.agents.harness.common.browser_defaults import (
    DEFAULT_BROWSER_AGENT_MAX_ITERATIONS,
)
from jiuwenswarm.common import utils as _utils_mod
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.agents.harness.common.prompt.prompt_builder import (
    build_agent_identity_prompt,
)
from jiuwenswarm.agents.harness.common.prompt.browser_task_prompt import (
    build_browser_task_prompt,
)
from jiuwenswarm.agents.harness.common.rails import skill_retrieval_prompt_rail as _skill_retrieval_prompt_mod
from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import RuntimePromptRail
from jiuwenswarm.agents.harness.common.rails.response_prompt_rail import ResponsePromptRail
from jiuwenswarm.agents.harness.common.rails.skill_retrieval_prompt_rail import SkillRetrievalPromptRail
from jiuwenswarm.agents.harness.common.rails.symphony import (
    SymphonyOrchestrationRail,
)
from jiuwenswarm.agents.harness.common.tools.symphony_toolkits import (
    SymphonyToolkit,
)


class _TestableJiuWenSwarmDeepAdapter(JiuWenSwarmDeepAdapter):
    def set_workspace_dir(self, workspace_dir: str) -> None:
        self._workspace_dir = workspace_dir

    def build_configured_subagents(
        self,
        model: Model,
        config: dict,
        config_base: dict | None = None,
    ):
        return self._build_configured_subagents(model, config, config_base)


class _FakeSession:
    def get_session_id(self) -> str:
        return "sess1"


class _FakeAgent:
    def __init__(self, builder: SystemPromptBuilder) -> None:
        self.system_prompt_builder = builder
        self.prompt_attachment_manager = PromptAttachmentManager()


class _FakeLiveModeAgent(_FakeAgent):
    def __init__(self, builder: SystemPromptBuilder, mode: str) -> None:
        super().__init__(builder)
        self.mode = mode

    def load_state(self, session):
        return SimpleNamespace(
            plan_mode=SimpleNamespace(mode=self.mode),
        )


class _FakeAbilityManager:
    def __init__(self) -> None:
        self._items = {
            "list_skill": SimpleNamespace(name="list_skill"),
            "search_skill": SimpleNamespace(name="search_skill"),
        }
        self.added: list[str] = []
        self.removed: list[str] = []

    def add_ability(self, card, tool=None):
        self._items[card.name] = card
        return SimpleNamespace(added=True)

    def remove_ability(self, name: str):
        return self._items.pop(name, None)

    def get(self, name: str):
        return self._items.get(name)

    def remove(self, name: str):
        self.removed.append(name)
        return self._items.pop(name, None)

    def add(self, ability):
        self.added.append(ability.name)
        self._items[ability.name] = ability


class _FakeToolAgent(_FakeAgent):
    def __init__(self, builder: SystemPromptBuilder) -> None:
        super().__init__(builder)
        self.ability_manager = _FakeAbilityManager()


class _FakeResourceManager:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.removed: list[str] = []

    def add_tool(
        self,
        tool: SimpleNamespace,
        *,
        tag: object | None = None,
        refresh: bool = False,
        skip_if_exists: bool = False,
    ) -> None:
        self.added.append(tool.card.name)

    def remove_tool(self, tool_id: str) -> None:
        self.removed.append(tool_id)


class _FakeRuntimeInstance:
    def __init__(self) -> None:
        self.card = SimpleNamespace(id="jiuwenswarm")
        self.ability_manager = _FakeAbilityManager()


def _tool_call_ctx(
    tool_name: str,
    args: dict,
    *,
    extra: dict | None = None,
    result: object | None = None,
):
    tool_call = SimpleNamespace(
        id=f"{tool_name}-call",
        name=tool_name,
        arguments=dict(args),
    )
    ctx = SimpleNamespace(
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_name,
            tool_args=dict(args),
            tool_result={"success": True} if result is None else result,
        ),
        extra={} if extra is None else extra,
        exception=None,
    )
    ctx.force_finished = []
    ctx.request_force_finish = ctx.force_finished.append
    return ctx


def test_build_agent_identity_prompt_contains_stable_identity_and_task_strategy():
    prompt = build_agent_identity_prompt(language="zh")

    assert "# 身份" in prompt
    assert "# 任务执行策略" in prompt
    assert "# JiuwenSwarm 内部数据" not in prompt
    assert "## 输出文件放置规范" not in prompt
    assert "## 文件发送" not in prompt
    assert "## Skill Orchestration Contract" not in prompt
    assert "`symphony_compose_graph`" not in prompt
    assert "# 消息说明" not in prompt


@pytest.mark.asyncio
async def test_response_prompt_rail_splits_input_and_output_rules():
    builder = SystemPromptBuilder(language="cn")
    agent = _FakeAgent(builder)
    rail = ResponsePromptRail()
    rail.init(agent)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=_FakeSession(),
        extra={},
    )

    await rail.before_model_call(ctx)

    prompt = builder.build()
    assert "# 输入说明" in prompt
    assert "# 输出规则" in prompt
    assert "## 输出语言" in prompt
    assert "## 模型名称回答" in prompt
    assert "# 消息说明" not in prompt
    assert builder.has_section("input")
    assert builder.has_section("output")
    assert not builder.has_section("response")


@pytest.mark.asyncio
async def test_symphony_orchestration_rail_respects_config_snapshot():
    enabled_builder = SystemPromptBuilder(language="cn")
    enabled_agent = _FakeAgent(enabled_builder)
    enabled_ctx = AgentCallbackContext(
        agent=enabled_agent,
        inputs=SimpleNamespace(
            tools=[SimpleNamespace(name="symphony_compose_graph")],
        ),
        session=_FakeSession(),
        extra={},
    )
    enabled_rail = SymphonyOrchestrationRail(
        config_base={"symphony": {"enabled": True}},
    )
    enabled_rail.init(enabled_agent)
    await enabled_rail.before_model_call(enabled_ctx)

    disabled_builder = SystemPromptBuilder(language="cn")
    disabled_agent = _FakeAgent(disabled_builder)
    disabled_ctx = AgentCallbackContext(
        agent=disabled_agent,
        inputs=SimpleNamespace(
            tools=[SimpleNamespace(name="symphony_compose_graph")],
        ),
        session=_FakeSession(),
        extra={},
    )
    disabled_rail = SymphonyOrchestrationRail(
        config_base={"symphony": {"enabled": False}},
    )
    disabled_rail.init(disabled_agent)
    await disabled_rail.before_model_call(disabled_ctx)

    enabled_prompt = enabled_builder.build()
    disabled_prompt = disabled_builder.build()
    assert "## Skill Orchestration Contract" in enabled_prompt
    assert "`symphony_compose_graph`" in enabled_prompt
    assert "## Skill Orchestration Contract" not in disabled_prompt
    assert "`symphony_compose_graph`" not in disabled_prompt


@pytest.mark.asyncio
async def test_symphony_orchestration_rail_injects_when_tool_visible(
    monkeypatch,
):
    monkeypatch.setattr(
        "jiuwenswarm.symphony.config.load_symphony_config",
        lambda: SimpleNamespace(enabled=True),
    )
    builder = SystemPromptBuilder(language="cn")
    agent = _FakeAgent(builder)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=SimpleNamespace(
            tools=[SimpleNamespace(name="symphony_compose_graph")],
        ),
        session=_FakeSession(),
        extra={},
    )

    rail = SymphonyOrchestrationRail()
    rail.init(agent)
    await rail.before_model_call(ctx)

    prompt = builder.build()
    assert "## Skill Orchestration Contract" in prompt
    assert "`symphony_compose_graph`" in prompt
    assert "exact identifiers or names" in prompt
    assert "when ANY of these conditions is true" in prompt
    assert "two or more specialized capabilities" in prompt
    assert "identified, inspected, selected, invoked, or recommended" in prompt
    assert "Calling `skill_branch_explore` creates a mandatory orchestration follow-up" in prompt
    assert "never pass every Skill returned by exploration" in prompt
    assert "still call `symphony_compose_graph`" in prompt
    assert "none of the three trigger conditions is true" in prompt
    assert "Symphony" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "operation", "language", "expected_content"),
    [
        (
            "symphony_compose_graph",
            "plan",
            "cn",
            "技能较多时，技能图谱构建时间可能较长，本次会话内构建已超时。请前往「我的技能」>「技能图谱」，点击「增量构建」并等待构建完成；完成后请重新发送任务。为避免再次超时，本轮不会重复调用图谱构建。",
        ),
        (
            "symphony_refresh_graph",
            "refresh_graph",
            "en",
            "With many skills, building the Skill Graph can take a while",
        ),
    ],
)
async def test_symphony_timeout_is_terminal_manual_build_result(
    tool_name,
    operation,
    language,
    expected_content,
    monkeypatch,
):
    async def blocking_handler(*args, **kwargs):
        del args, kwargs
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.symphony_toolkits.load_symphony_config",
        lambda config=None: SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(
        SymphonyToolkit,
        "_resolve_timeout_s",
        staticmethod(lambda default_s=1800.0: 0.01),
    )
    toolkit = SymphonyToolkit(
        SimpleNamespace(plan=blocking_handler, refresh_graph=blocking_handler)
    )
    raw_timeout_result = (
        await toolkit.plan("compose")
        if tool_name == "symphony_compose_graph"
        else await toolkit.refresh_graph()
    )
    builder = SystemPromptBuilder(language=language)
    agent = _FakeAgent(builder)
    rail = SymphonyOrchestrationRail()
    rail.init(agent)
    ctx = _tool_call_ctx(
        tool_name,
        {"query": "compose"},
        result=raw_timeout_result,
    )

    await rail.after_tool_call(ctx)

    result = ctx.inputs.tool_result
    assert result["direct_display"] is True
    assert result["continue_after_display"] is False
    assert result["followup_action"] == "manual_graph_build"
    assert result["content"] == expected_content or expected_content in result["content"]
    assert result["operation"] == operation
    assert result["timeout_s"] == 0.01
    assert ctx.extra["symphony_graph_build_timeout"] is True
    assert ctx.force_finished == [
        {"output": result["content"], "result_type": "answer"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "operation", "timeout_s"),
    [
        ("symphony_compose_graph", "plan", 3600.0),
        ("symphony_refresh_graph", "refresh_graph", 3600.0),
    ],
)
async def test_symphony_outer_timeout_forces_manual_build(
    tool_name,
    operation,
    timeout_s,
):
    rail = SymphonyOrchestrationRail()
    ctx = _tool_call_ctx(tool_name, {})
    timeout = TimeoutError("hard timeout")
    ctx.exception = AbilityExecutionError(
        status=StatusCode.AGENT_TOOL_EXECUTION_ERROR,
        msg=f"Tool '{tool_name}' timed out after {timeout_s}s",
        cause=timeout,
        tool_message=ToolMessage(
            content=f"Tool '{tool_name}' timed out after {timeout_s}s",
            tool_call_id="timeout-call",
        ),
    )

    await rail.on_tool_exception(ctx)

    result = ctx.inputs.tool_result
    assert result["operation"] == operation
    assert result["timeout_s"] == timeout_s
    assert result["followup_action"] == "manual_graph_build"
    assert result["continue_after_display"] is False
    assert ctx.force_finished
    assert isinstance(ctx.exception, AbilityExecutionError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception",
    [
        TimeoutError("raw timeout"),
        AbilityExecutionError(
            status=StatusCode.AGENT_TOOL_EXECUTION_ERROR,
            msg="Tool execution error: current failure",
            cause=RuntimeError("wrapper"),
            tool_message=ToolMessage(
                content="Tool 'symphony_compose_graph' timed out after 3600.0s",
                tool_call_id="timeout-call",
            ),
        ),
        AbilityExecutionError(
            status=StatusCode.AGENT_TOOL_EXECUTION_ERROR,
            msg="wrong tool timeout",
            cause=TimeoutError("hard timeout"),
            tool_message=ToolMessage(
                content="Tool 'symphony_refresh_graph' timed out after 3600.0s",
                tool_call_id="timeout-call",
            ),
        ),
    ],
)
async def test_symphony_outer_timeout_rejects_non_ability_manager_shapes(exception):
    if isinstance(exception, AbilityExecutionError) and isinstance(
        exception.__cause__, RuntimeError
    ):
        exception.__cause__.__cause__ = TimeoutError("nested timeout")
    rail = SymphonyOrchestrationRail()
    ctx = _tool_call_ctx("symphony_compose_graph", {})
    ctx.exception = exception

    await rail.on_tool_exception(ctx)

    assert ctx.inputs.tool_result["success"] is True
    assert ctx.force_finished == []


@pytest.mark.asyncio
async def test_symphony_timeout_removes_graph_tools_and_orchestration_prompt():
    builder = SystemPromptBuilder(language="cn")
    agent = _FakeAgent(builder)
    rail = SymphonyOrchestrationRail(
        config_base={"symphony": {"enabled": True}},
    )
    rail.init(agent)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=ModelCallInputs(
            tools=[
                {"function": {"name": "symphony_compose_graph"}},
                SimpleNamespace(name="symphony_refresh_graph"),
                SimpleNamespace(name="other_tool"),
            ]
        ),
        session=_FakeSession(),
        extra={},
    )

    timed_out_tool_ctx = _tool_call_ctx(
        "symphony_compose_graph",
        {"query": "compose"},
        extra=ctx.extra,
        result={"success": False, "reason": "graph_build_timeout"},
    )
    await rail.after_tool_call(timed_out_tool_ctx)

    await rail.before_model_call(ctx)

    assert [rail._model_tool_name(tool) for tool in ctx.inputs.tools] == ["other_tool"]
    assert "## Skill Orchestration Contract" not in builder.build()

    next_invoke_ctx = AgentCallbackContext(
        agent=agent,
        inputs=ModelCallInputs(
            tools=[
                SimpleNamespace(name="symphony_compose_graph"),
                SimpleNamespace(name="symphony_refresh_graph"),
            ]
        ),
        session=_FakeSession(),
        extra={},
    )
    await rail.before_model_call(next_invoke_ctx)

    assert [rail._model_tool_name(tool) for tool in next_invoke_ctx.inputs.tools] == [
        "symphony_compose_graph",
        "symphony_refresh_graph",
    ]
    assert "## Skill Orchestration Contract" in builder.build()


@pytest.mark.asyncio
async def test_symphony_non_timeout_failure_does_not_force_finish():
    rail = SymphonyOrchestrationRail()
    ctx = _tool_call_ctx("symphony_compose_graph", {})
    ctx.exception = AbilityExecutionError(
        status=StatusCode.AGENT_TOOL_EXECUTION_ERROR,
        msg="service unavailable",
    )

    await rail.on_tool_exception(ctx)

    assert ctx.inputs.tool_result["success"] is True
    assert ctx.force_finished == []


@pytest.mark.asyncio
async def test_symphony_orchestration_rail_backfills_viewed_skills():
    rail = SymphonyOrchestrationRail()
    invocation_extra: dict = {}

    for skill_name in (
        "creating-financial-models",
        "xlsx",
        "creating-financial-models",
    ):
        await rail.after_tool_call(
            _tool_call_ctx(
                "skill_tool",
                {"skill_name": skill_name},
                extra=invocation_extra,
            )
        )

    compose_ctx = _tool_call_ctx(
        "symphony_compose_graph",
        {"query": "build a financial model"},
        extra=invocation_extra,
    )
    await rail.before_tool_call(compose_ctx)

    expected = ["creating-financial-models", "xlsx"]
    assert compose_ctx.inputs.tool_args["candidate_skill_ids"] == expected
    assert compose_ctx.inputs.tool_call.arguments["candidate_skill_ids"] == expected


@pytest.mark.asyncio
async def test_symphony_orchestration_rail_preserves_explicit_candidates():
    rail = SymphonyOrchestrationRail()
    invocation_extra: dict = {}
    await rail.after_tool_call(
        _tool_call_ctx(
            "skill_tool",
            {"skill_name": "viewed-skill"},
            extra=invocation_extra,
        )
    )
    compose_ctx = _tool_call_ctx(
        "symphony_compose_graph",
        {"query": "task", "candidate_skill_ids": ["explicit-skill"]},
        extra=invocation_extra,
    )

    await rail.before_tool_call(compose_ctx)

    assert compose_ctx.inputs.tool_args["candidate_skill_ids"] == [
        "explicit-skill"
    ]


@pytest.mark.asyncio
async def test_symphony_orchestration_rail_does_not_reuse_other_invocation():
    rail = SymphonyOrchestrationRail()
    await rail.after_tool_call(
        _tool_call_ctx(
            "skill_tool",
            {"skill_name": "previous-skill"},
            extra={},
        )
    )
    compose_ctx = _tool_call_ctx(
        "symphony_compose_graph",
        {"query": "new task"},
        extra={},
    )

    await rail.before_tool_call(compose_ctx)

    assert "candidate_skill_ids" not in compose_ctx.inputs.tool_args


@pytest.mark.asyncio
async def test_symphony_orchestration_rail_ignores_disclosure_and_failed_views():
    rail = SymphonyOrchestrationRail()
    invocation_extra: dict = {}
    await rail.after_tool_call(
        _tool_call_ctx(
            "skill_branch_explore",
            {"node_ids": ["FinanceBusiness"]},
            extra=invocation_extra,
        )
    )
    await rail.after_tool_call(
        _tool_call_ctx(
            "skill_tool",
            {"skill_name": "failed-skill"},
            extra=invocation_extra,
            result={"success": False},
        )
    )
    compose_ctx = _tool_call_ctx(
        "symphony_compose_graph",
        {"query": "task"},
        extra=invocation_extra,
    )

    await rail.before_tool_call(compose_ctx)

    assert "candidate_skill_ids" not in compose_ctx.inputs.tool_args


@pytest.mark.asyncio
async def test_symphony_orchestration_rail_clears_when_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(
        "jiuwenswarm.symphony.config.load_symphony_config",
        lambda: SimpleNamespace(enabled=True),
    )
    builder = SystemPromptBuilder(language="cn")
    builder.add_section(
        PromptSection(
            name="symphony_orchestration",
            content={"cn": "stale orchestration prompt"},
            priority=42,
        )
    )
    agent = _FakeAgent(builder)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=SimpleNamespace(tools=[SimpleNamespace(name="other_tool")]),
        session=_FakeSession(),
        extra={},
    )

    rail = SymphonyOrchestrationRail()
    rail.init(agent)
    await rail.before_model_call(ctx)

    assert "stale orchestration prompt" not in builder.build()


@pytest.mark.asyncio
async def test_symphony_orchestration_rail_clears_when_disabled(
    monkeypatch,
):
    monkeypatch.setattr(
        "jiuwenswarm.symphony.config.load_symphony_config",
        lambda: SimpleNamespace(enabled=False),
    )
    builder = SystemPromptBuilder(language="cn")
    agent = _FakeAgent(builder)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=SimpleNamespace(
            tools=[SimpleNamespace(name="symphony_compose_graph")],
        ),
        session=_FakeSession(),
        extra={},
    )

    rail = SymphonyOrchestrationRail()
    rail.init(agent)
    await rail.before_model_call(ctx)

    assert "## Skill Orchestration Contract" not in builder.build()


def test_deep_adapter_syncs_symphony_tools_from_config_snapshot(monkeypatch):
    fake_resource = _FakeResourceManager()
    fake_instance = _FakeRuntimeInstance()
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = fake_instance
    adapter._is_session_scoped_adapter = False
    adapter._tool_cards = []
    adapter._symphony_tools = []
    adapter._symphony_tools_registered = False
    seen_configs: list[dict] = []

    tools = [
        SimpleNamespace(card=SimpleNamespace(id=name, name=name))
        for name in (
            "symphony_read_graph",
            "symphony_refresh_graph",
            "symphony_compose_graph",
        )
    ]

    class FakeSymphonyToolkit:
        def get_tools(self, config_base=None):
            seen_configs.append(config_base)
            return tools

    monkeypatch.setattr(interface_module.Runner, "resource_mgr", fake_resource)
    monkeypatch.setattr(interface_module, "SymphonyToolkit", FakeSymphonyToolkit)

    adapter._sync_symphony_tools_for_runtime({"symphony": {"enabled": True}})

    assert seen_configs == [{"symphony": {"enabled": True}}]
    assert adapter._symphony_tools_registered is True
    assert [card.name for card in adapter._tool_cards] == [
        "symphony_read_graph",
        "symphony_refresh_graph",
        "symphony_compose_graph",
    ]
    assert fake_resource.added == [
        "symphony_read_graph",
        "symphony_refresh_graph",
        "symphony_compose_graph",
    ]
    assert fake_instance.ability_manager.added == fake_resource.added

    adapter._sync_symphony_tools_for_runtime({"symphony": {"enabled": False}})

    assert adapter._symphony_tools == []
    assert adapter._symphony_tools_registered is False
    assert adapter._tool_cards == []
    # Symphony tools are shared across adapters, so disabling them here detaches
    # them from this agent only; the process-global registration stays for any
    # sibling adapter still running on it.
    assert fake_resource.removed == []
    assert fake_instance.ability_manager.removed == [
        "symphony_read_graph",
        "symphony_refresh_graph",
        "symphony_compose_graph",
    ]


@pytest.mark.asyncio
async def test_runtime_environment_section_participates_in_priority_order():
    builder = SystemPromptBuilder(language="cn")
    builder.add_section(PromptSection(name="identity", content={"cn": "identity"}, priority=10))
    builder.add_section(PromptSection(name="tools", content={"cn": "# 可用工具"}, priority=30))
    builder.add_section(PromptSection(name="workspace", content={"cn": "# 工作空间"}, priority=70))

    agent = _FakeAgent(builder)
    runtime_rail = RuntimePromptRail(
        language="cn",
        channel="web"
    )
    runtime_rail.init(agent)

    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=_FakeSession(),
        extra={},
    )
    await runtime_rail.before_model_call(ctx)

    prompt = builder.build()
    ordered_markers = [
        "identity",
        "# 可用工具",
        "# 工作空间",
        "# 运行环境",
    ]
    positions = [prompt.index(marker) for marker in ordered_markers]
    assert positions == sorted(positions)
    assert builder.has_section("env")
    assert not builder.has_section("time")
    assert not builder.has_section("runtime.model_answer_policy")
    assert not builder.has_section("language_output")
    assert not builder.has_section("runtime")
    assert "# 运行时状态" not in prompt


@pytest.mark.asyncio
async def test_runtime_dynamic_sections_go_to_prompt_attachment_when_manager_available(tmp_path, monkeypatch):
    monkeypatch.setattr(_utils_mod, "get_config_dir", lambda: tmp_path)
    builder = SystemPromptBuilder(language="en")
    agent = _FakeAgent(builder)
    runtime_rail = RuntimePromptRail(language="en", channel="web")
    runtime_rail.init(agent)
    runtime_rail.set_model_name("model-x")
    runtime_rail.set_mode("agent.plan")

    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=_FakeSession(),
        extra={},
    )
    await runtime_rail.before_model_call(ctx)

    prompt = builder.build()
    assert "# Runtime Environment" in prompt
    assert "# Runtime State" not in prompt
    assert "# Time Description" not in prompt
    assert "# Language" not in prompt
    assert "# Model Name Answer Policy" not in prompt
    assert "# Browser Tool Policy" not in prompt
    assert "## Browser Subagent Rules" not in prompt
    assert "browser_preflight_submit" not in prompt
    assert "hotel_option_select" not in prompt
    assert "gmail_email_select" not in prompt
    assert "social_post_draft_select" not in prompt
    assert "Mandatory Web A2UI account-action gate" not in prompt
    assert 'subagent_type` set to `"browser_agent"`' not in prompt
    assert "## Platform and Shell" in prompt
    assert "## Time-sensitive Queries" in prompt
    assert "## Current Channel" in prompt

    items = await agent.prompt_attachment_manager.collect_for_session("sess1")
    assert [item.id for item in items] == ["session.sess1.runtime.setting"]
    rendered = agent.prompt_attachment_manager.render(items)
    assert "model-x" in rendered
    assert "Always respond in English" not in prompt
    assert "# Browser Tool Policy" not in prompt
    assert "## Browser Subagent Rules" not in prompt


@pytest.mark.asyncio
async def test_browser_policy_is_localized_and_merged_into_task_tool_section():
    rail = JiuWenSwarmDeepAdapter._build_subagent_rail()
    if rail is None:
        pytest.skip("SubagentRail is unavailable with the installed openjiuwen API")
    rail.tools = [object()]
    rail.system_prompt_builder = SystemPromptBuilder(language="en")

    ctx = AgentCallbackContext(
        agent=SimpleNamespace(),
        inputs=None,
        session=_FakeSession(),
        extra={},
    )
    await rail.before_model_call(ctx)

    task_section = rail.system_prompt_builder.get_section("task_tool")
    if task_section is None:
        pytest.skip("task_tool prompt section is unavailable in this tool configuration")
    assert "# Subagent Usage Rules" in task_section.content["en"]
    assert "## task_tool" not in task_section.content["en"]
    assert "## Browser Subagent Rules" in task_section.content["en"]
    assert 'set `subagent_type` to `"browser_agent"`' in task_section.content["en"]
    assert not rail.system_prompt_builder.has_section("browser_tool_policy")
    assert "浏览器子智能体规则" in build_browser_task_prompt("cn")

    rail.set_channel("tui")
    rail.system_prompt_builder = SystemPromptBuilder(language="en")
    await rail.before_model_call(ctx)
    non_web_task_section = rail.system_prompt_builder.get_section("task_tool")
    if non_web_task_section is None:
        pytest.skip("task_tool prompt section is unavailable in this tool configuration")
    assert "# Subagent Usage Rules" in non_web_task_section.content["en"]
    assert "## Browser Subagent Rules" not in non_web_task_section.content["en"]


def test_task_planning_tools_remain_enabled_without_todo_prompt_section():
    rail = JiuWenSwarmDeepAdapter._build_task_planning_rail()
    if rail is None:
        pytest.skip("TaskPlanningRail is unavailable with the installed openjiuwen API")
    assert rail.inject_prompt is False


@pytest.mark.asyncio
async def test_runtime_attachment_tracks_live_code_agent_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(_utils_mod, "get_config_dir", lambda: tmp_path)
    runtime_state = tmp_path / "runtime_state" / "default.yaml"
    runtime_state.parent.mkdir(parents=True, exist_ok=True)
    runtime_state.write_text(
        "model: model-x\n"
        "available_models:\n"
        "  - model-x\n"
        "mode: code.normal\n",
        encoding="utf-8",
    )
    builder = SystemPromptBuilder(language="en")
    agent = _FakeLiveModeAgent(builder, mode="plan")
    runtime_rail = RuntimePromptRail(language="en", channel="tui")
    runtime_rail.init(agent)
    ctx = AgentCallbackContext(
        # Inner ReactAgent callbacks do not expose DeepAgent.load_state().
        agent=SimpleNamespace(),
        inputs=None,
        session=_FakeSession(),
        extra={},
    )

    await runtime_rail.before_model_call(ctx)
    items = await agent.prompt_attachment_manager.collect_for_session("sess1")
    rendered = agent.prompt_attachment_manager.render(items)
    assert "Current mode: code.plan" in rendered
    assert "Current mode: code.normal" not in rendered

    agent.mode = "normal"
    await runtime_rail.before_model_call(ctx)
    items = await agent.prompt_attachment_manager.collect_for_session("sess1")
    rendered = agent.prompt_attachment_manager.render(items)
    assert "Current mode: code.normal" in rendered
    assert "Current mode: code.plan" not in rendered


@pytest.mark.asyncio
async def test_runtime_git_status_attachment_clears_when_git_context_disappears(tmp_path, monkeypatch):
    monkeypatch.setattr(_utils_mod, "get_config_dir", lambda: tmp_path)
    runtime_state = tmp_path / "runtime_state" / "default.yaml"
    runtime_state.parent.mkdir(parents=True, exist_ok=True)
    runtime_state.write_text(
        "git_branch: feature/test\n"
        "git_status: M file.py\n"
        "git_recent_commits: abc init\n",
        encoding="utf-8",
    )
    builder = SystemPromptBuilder(language="en")
    agent = _FakeAgent(builder)
    runtime_rail = RuntimePromptRail(language="en", channel="web")
    runtime_rail.init(agent)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=_FakeSession(),
        extra={},
    )

    await runtime_rail.before_model_call(ctx)
    session_items = await agent.prompt_attachment_manager.list_by_filter(session_id="sess1")
    assert [item.id for item in session_items if item.id.endswith(".git_status")] == ["session.sess1.git_status"]

    runtime_state.write_text("git_branch: ''\n", encoding="utf-8")
    await runtime_rail.before_model_call(ctx)
    session_items = await agent.prompt_attachment_manager.list_by_filter(session_id="sess1")
    assert [item.id for item in session_items if item.id.endswith(".git_status")] == []


@pytest.mark.asyncio
async def test_runtime_prompt_uses_runtime_cwd_over_stale_trusted_dir(tmp_path, monkeypatch):
    builder = SystemPromptBuilder(language="en")
    agent = _FakeAgent(builder)
    stale_dir = tmp_path / "missing-worktree"
    project_dir = tmp_path / "project"
    current_dir = project_dir / "current"
    extra_dir = tmp_path / "extra"
    agent_data_dir = tmp_path / "agent-data"
    current_dir.mkdir(parents=True)
    extra_dir.mkdir()
    agent_data_dir.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_agent_workspace_dir",
        lambda: agent_data_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_user_workspace_dir",
        lambda: tmp_path / "jiuwenswarm-data",
    )

    runtime_rail = RuntimePromptRail(language="en", channel="tui")
    runtime_rail.init(agent)
    runtime_rail.set_trusted_dirs([str(stale_dir), str(current_dir), str(extra_dir)])
    runtime_rail.set_runtime_paths(cwd=str(current_dir), project_dir=str(project_dir))

    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=_FakeSession(),
        extra={},
    )
    await runtime_rail.before_model_call(ctx)

    prompt = builder.build()
    assert "# Directory and File-Operation Boundaries" in prompt
    assert "# Runtime Directory Context" not in prompt
    assert "# Working Directory Runtime Values" not in prompt
    assert "The project directory is your current workspace" in prompt
    assert f"the current project directory is: `{project_dir}`" in prompt
    assert "Agent internal data directory" in prompt
    assert "## JiuwenSwarm Internal Directories" in prompt
    assert str(project_dir) in prompt
    assert str(current_dir) not in prompt
    assert str(stale_dir) not in prompt
    assert str(extra_dir) not in prompt
    assert "System directory" not in prompt

    items = await agent.prompt_attachment_manager.list_by_filter(session_id="sess1")
    assert [item.id for item in items if item.id.endswith(".trusted_dirs_policy")] == []


@pytest.mark.asyncio
async def test_runtime_prompt_describes_external_cwd_without_project(tmp_path, monkeypatch):
    builder = SystemPromptBuilder(language="en")
    agent = _FakeAgent(builder)
    agent_data_dir = tmp_path / "agent-data"
    task_dir = tmp_path / "standalone-task"
    agent_data_dir.mkdir()
    task_dir.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_agent_workspace_dir",
        lambda: agent_data_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_user_workspace_dir",
        lambda: tmp_path / "jiuwenswarm-data",
    )

    runtime_rail = RuntimePromptRail(language="en", channel="web")
    runtime_rail.init(agent)
    runtime_rail.set_runtime_paths(cwd=str(task_dir), project_dir=None)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=_FakeSession(),
        extra={},
    )

    await runtime_rail.before_model_call(ctx)

    prompt = builder.build()
    assert "The project directory is your current workspace" in prompt
    assert f"the current project directory is: `{task_dir}`" in prompt
    assert "Other accessible directories" not in prompt
    assert "fallen back to the Agent internal data directory" not in prompt


@pytest.mark.asyncio
async def test_runtime_prompt_describes_agent_data_cwd_fallback(tmp_path, monkeypatch):
    builder = SystemPromptBuilder(language="cn")
    agent = _FakeAgent(builder)
    agent_data_dir = tmp_path / "agent-data"
    agent_data_dir.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_agent_workspace_dir",
        lambda: agent_data_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_user_workspace_dir",
        lambda: tmp_path / "jiuwenswarm-data",
    )

    runtime_rail = RuntimePromptRail(language="cn", channel="web")
    runtime_rail.init(agent)
    runtime_rail.set_runtime_paths(cwd=None, project_dir=None)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=_FakeSession(),
        extra={},
    )

    await runtime_rail.before_model_call(ctx)

    prompt = builder.build()
    assert "# 目录与文件操作边界" in prompt
    assert f"当前项目目录是：`{agent_data_dir}`" in prompt
    assert "其他可访问目录" not in prompt


@pytest.mark.asyncio
async def test_runtime_prompt_clears_directory_boundaries_outside_web_and_tui(
    tmp_path,
    monkeypatch,
):
    builder = SystemPromptBuilder(language="cn")
    agent = _FakeAgent(builder)
    agent_data_dir = tmp_path / "agent-data"
    agent_data_dir.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_agent_workspace_dir",
        lambda: agent_data_dir,
    )

    runtime_rail = RuntimePromptRail(language="cn", channel="web")
    runtime_rail.init(agent)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=_FakeSession(),
        extra={},
    )

    await runtime_rail.before_model_call(ctx)
    assert "# 目录与文件操作边界" in builder.build()

    runtime_rail.set_channel("a2a")
    await runtime_rail.before_model_call(ctx)
    assert "# 目录与文件操作边界" not in builder.build()


@pytest.mark.asyncio
async def test_runtime_prompt_reports_powershell_and_removes_generic_shell_rules(monkeypatch):
    import jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail as runtime_module

    monkeypatch.setattr(runtime_module.sys, "platform", "win32")
    monkeypatch.setattr(
        runtime_module.shutil,
        "which",
        lambda command: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if command == "powershell"
        else None,
    )

    builder = SystemPromptBuilder(language="cn")
    agent = _FakeAgent(builder)
    runtime_rail = RuntimePromptRail(language="cn", channel="web")
    runtime_rail.init(agent)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=_FakeSession(),
        extra={},
    )

    await runtime_rail.before_model_call(ctx)

    prompt = builder.build()
    assert "- Shell：PowerShell" in prompt
    assert "Shell 规则：" not in prompt
    assert "### 项目目录规则" in prompt
    assert "### 项目录规则" not in prompt


@pytest.mark.asyncio
async def test_runtime_prompt_language_output_prefers_rail_language_over_runtime_state(
    monkeypatch,
    tmp_path,
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    state_dir = config_dir / "runtime_state"
    state_dir.mkdir()
    (state_dir / "default.yaml").write_text(
        "model: test-model\nmode: team.plan\nlanguage: en\nchannel: tui\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_utils_mod, "get_config_dir", lambda: config_dir)

    builder = SystemPromptBuilder(language="cn")
    agent = _FakeAgent(builder)
    runtime_rail = RuntimePromptRail(language="cn", channel="tui")
    runtime_rail.init(agent)

    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=_FakeSession(),
        extra={},
    )
    await runtime_rail.before_model_call(ctx)

    prompt = builder.build()
    # The runtime rail now keeps the selected language in the runtime state
    # instead of emitting the legacy ``language_output`` section.
    assert "Always respond in Chinese (Simplified)" not in prompt
    rendered = agent.prompt_attachment_manager.render(
        await agent.prompt_attachment_manager.list_by_filter(session_id="sess1")
    )
    assert "Always respond in Chinese (Simplified)" not in rendered
    assert "Always respond in English." not in rendered
    assert "Always respond in English." not in prompt
    # Runtime context is attached separately and rendered by the attachment
    # manager, rather than being part of the main system-prompt sections.
    assert "当前语言：cn" in rendered


@pytest.mark.asyncio
async def test_skill_retrieval_prompt_hides_legacy_list_skill(monkeypatch):
    monkeypatch.setattr(
        _skill_retrieval_prompt_mod,
        "is_agentic_retrieval_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        _skill_retrieval_prompt_mod,
        "render_skill_retrieval_prompt_for_visible_skills",
        lambda manager, language, visible_skill_names=None: "# Agentic 技能检索\n使用 skill_branch_explore。",
    )
    builder = SystemPromptBuilder(language="cn")
    builder.add_section(PromptSection(name="skills", content={"cn": "旧 list_skill 提示"}, priority=40))
    agent = _FakeToolAgent(builder)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=SimpleNamespace(
            tools=[
                SimpleNamespace(name="list_skill"),
                SimpleNamespace(name="list_skills"),
                SimpleNamespace(name="skill_branch_explore"),
            ],
        ),
        session=_FakeSession(),
        extra={},
    )

    rail = SkillRetrievalPromptRail()
    rail.init(agent)
    await rail.before_model_call(ctx)

    assert [tool.name for tool in ctx.inputs.tools] == ["skill_branch_explore"]
    assert agent.ability_manager.get("list_skill") is None
    prompt = builder.build()
    assert "旧 list_skill 提示" not in prompt
    assert "Agentic 技能检索" in prompt

    await rail.after_model_call(ctx)

    assert agent.ability_manager.get("list_skill") is not None
    assert "旧 list_skill 提示" in builder.build()


@pytest.mark.asyncio
async def test_skill_retrieval_prompt_hides_native_skill_prompt_after_skill_use_rail(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        _skill_retrieval_prompt_mod,
        "is_agentic_retrieval_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        _skill_retrieval_prompt_mod,
        "render_skill_retrieval_prompt_for_visible_skills",
        lambda manager, language, visible_skill_names=None: "# Agentic 技能检索\n使用 skill_branch_explore。",
    )
    builder = SystemPromptBuilder(language="cn")
    agent = _FakeToolAgent(builder)
    agent.card = SimpleNamespace(id="test-agent")
    agent.deep_config = SimpleNamespace(enable_read_image_multimodal=False)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=SimpleNamespace(
            tools=[
                SimpleNamespace(name="list_skill"),
                SimpleNamespace(name="skill_branch_explore"),
            ],
        ),
        session=_FakeSession(),
        extra={},
    )
    skill_rail = SkillUseRail(
        str(tmp_path),
        skill_mode=SkillUseRail.SKILL_MODE_AUTO_LIST,
        include_tools=False,
    )
    retrieval_rail = SkillRetrievalPromptRail()
    skill_rail.init(agent)
    retrieval_rail.init(agent)

    rails = sorted([skill_rail, retrieval_rail], key=lambda rail: rail.priority, reverse=True)
    await rails[0].before_model_call(ctx)
    await rails[1].before_model_call(ctx)

    prompt = builder.build()
    assert "需要时先调用 list_skill 查看可用技能" not in prompt
    assert "# 技能" not in prompt
    assert "Agentic 技能检索" in prompt
    assert [tool.name for tool in ctx.inputs.tools] == ["skill_branch_explore"]


@pytest.mark.asyncio
async def test_skill_retrieval_prompt_clears_section_when_disabled(monkeypatch):
    monkeypatch.setattr(
        _skill_retrieval_prompt_mod,
        "is_agentic_retrieval_enabled",
        lambda: False,
    )
    builder = SystemPromptBuilder(language="cn")
    builder.add_section(PromptSection(name="skill_retrieval", content={"cn": "残留 Agentic 技能检索"}, priority=41))
    agent = _FakeToolAgent(builder)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=SimpleNamespace(tools=[SimpleNamespace(name="list_skill")]),
        session=_FakeSession(),
        extra={},
    )

    rail = SkillRetrievalPromptRail()
    rail.init(agent)
    await rail.before_model_call(ctx)

    assert [tool.name for tool in ctx.inputs.tools] == ["list_skill"]
    assert "残留 Agentic 技能检索" not in builder.build()
    assert agent.ability_manager.get("list_skill") is not None


@pytest.mark.asyncio
async def test_skill_retrieval_prompt_disabled_restores_hidden_skills_section(monkeypatch):
    enabled = True
    monkeypatch.setattr(
        _skill_retrieval_prompt_mod,
        "is_agentic_retrieval_enabled",
        lambda: enabled,
    )
    monkeypatch.setattr(
        _skill_retrieval_prompt_mod,
        "render_skill_retrieval_prompt_for_visible_skills",
        lambda manager, language, visible_skill_names=None: "# Agentic 技能检索\n使用 skill_branch_explore。",
    )
    builder = SystemPromptBuilder(language="cn")
    builder.add_section(PromptSection(name="skills", content={"cn": "原生技能提示"}, priority=40))
    agent = _FakeToolAgent(builder)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=SimpleNamespace(tools=[SimpleNamespace(name="skill_branch_explore")]),
        session=_FakeSession(),
        extra={},
    )

    rail = SkillRetrievalPromptRail()
    rail.init(agent)
    await rail.before_model_call(ctx)
    assert "原生技能提示" not in builder.build()

    enabled = False
    await rail.before_model_call(ctx)

    prompt = builder.build()
    assert "Agentic 技能检索" not in prompt
    assert "原生技能提示" in prompt


@pytest.mark.asyncio
async def test_skill_retrieval_prompt_render_empty_restores_native_skills(monkeypatch):
    monkeypatch.setattr(
        _skill_retrieval_prompt_mod,
        "is_agentic_retrieval_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        _skill_retrieval_prompt_mod,
        "render_skill_retrieval_prompt_for_visible_skills",
        lambda manager, language, visible_skill_names=None: "",
    )
    builder = SystemPromptBuilder(language="cn")
    builder.add_section(PromptSection(name="skills", content={"cn": "原生技能提示"}, priority=40))
    agent = _FakeToolAgent(builder)
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=SimpleNamespace(tools=[SimpleNamespace(name="list_skill")]),
        session=_FakeSession(),
        extra={},
    )

    rail = SkillRetrievalPromptRail()
    rail.init(agent)
    await rail.before_model_call(ctx)

    assert "原生技能提示" in builder.build()
    assert agent.ability_manager.get("list_skill") is not None
    assert [tool.name for tool in ctx.inputs.tools] == ["list_skill"]


def test_resolve_skill_mode_accepts_all_and_auto_list(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.is_skill_retrieval_enabled",
        lambda: False,
    )
    assert JiuWenSwarmDeepAdapter._resolve_skill_mode({"skill_mode": "all"}) == "all"
    assert JiuWenSwarmDeepAdapter._resolve_skill_mode({"skill_mode": "auto_list"}) == "auto_list"
    assert JiuWenSwarmDeepAdapter._resolve_skill_mode({"skill_mode": "invalid"}) == "all"

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.is_skill_retrieval_enabled",
        lambda: True,
    )
    assert JiuWenSwarmDeepAdapter._resolve_skill_mode({"skill_mode": "all"}) == "auto_list"


def test_deep_adapter_visible_skill_names_match_list_skill(monkeypatch, tmp_path):
    for name in ("alpha", "beta", "_internal", ".hidden"):
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    (tmp_path / "not-a-skill").mkdir()

    adapter = _TestableJiuWenSwarmDeepAdapter()
    adapter.set_skill_manager(
        SimpleNamespace(list_execution_disabled_skills=lambda: ["beta"])
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_agent_skills_dir",
        lambda: tmp_path,
    )

    assert adapter._visible_skill_names_for_list_skill() == {"alpha"}


def test_deep_adapter_skill_retrieval_prompt_uses_visible_skill_provider(monkeypatch):
    captured: dict[str, object] = {}

    class FakeRail:
        def __init__(self, *, manager, visible_skill_names):
            captured["manager"] = manager
            captured["visible_skill_names"] = visible_skill_names

    manager = SimpleNamespace(list_execution_disabled_skills=lambda: [])
    adapter = _TestableJiuWenSwarmDeepAdapter()
    adapter.set_skill_manager(manager)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.is_skill_retrieval_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.SkillRetrievalPromptRail",
        FakeRail,
    )

    rail = adapter._build_skill_retrieval_prompt_rail()

    assert isinstance(rail, FakeRail)
    assert captured["manager"] is manager
    assert captured["visible_skill_names"] == adapter._visible_skill_names_for_list_skill


@pytest.mark.asyncio
async def test_deep_adapter_skill_retrieval_prompt_rail_sync_hot_toggles(monkeypatch):
    registered: list[object] = []
    unregistered: list[object] = []

    class FakeDeepAgent:
        async def register_rail(self, rail):
            registered.append(rail)

        async def unregister_rail(self, rail):
            unregistered.append(rail)

    adapter = _TestableJiuWenSwarmDeepAdapter()
    adapter._instance = FakeDeepAgent()
    rail = SimpleNamespace(name="skill_retrieval_prompt")
    monkeypatch.setattr(adapter, "_build_skill_retrieval_prompt_rail", lambda: rail)
    monkeypatch.setattr(
        adapter,
        "_skill_retrieval_tools_enabled_for_runtime",
        lambda config_base=None: True,
    )

    await adapter._sync_skill_retrieval_prompt_rail_for_runtime()
    await adapter._sync_skill_retrieval_prompt_rail_for_runtime()

    assert adapter._skill_retrieval_prompt_rail is rail
    assert registered == [rail]
    assert unregistered == []

    monkeypatch.setattr(
        adapter,
        "_skill_retrieval_tools_enabled_for_runtime",
        lambda config_base=None: False,
    )

    await adapter._sync_skill_retrieval_prompt_rail_for_runtime()

    assert adapter._skill_retrieval_prompt_rail is None
    assert unregistered == [rail]


def test_code_adapter_skill_retrieval_sync_respects_configured_tools(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter

    adapter = JiuwenSwarmCodeAdapter()
    monkeypatch.setattr(
        interface_module,
        "is_skill_retrieval_enabled",
        lambda: True,
    )

    assert (
        adapter._skill_retrieval_tools_enabled_for_runtime(
            {"modes": {"code": {"tools": ["skill_toolkit"]}}}
        )
        is False
    )
    assert (
        adapter._skill_retrieval_tools_enabled_for_runtime(
            {"modes": {"code": {"tools": ["skill_toolkit", "skill_retrieval"]}}}
        )
        is True
    )


def test_resolve_enable_task_loop_forces_true_when_skill_evolution_enabled():
    assert (
        JiuWenSwarmDeepAdapter._resolve_enable_task_loop(
            {"enable_task_loop": False},
            {"react": {"evolution": {"skill_evolution": True}}},
        )
        is True
    )


def test_resolve_enable_task_loop_ignores_legacy_review_trigger():
    assert (
        JiuWenSwarmDeepAdapter._resolve_enable_task_loop(
            {"enable_task_loop": False},
            {"react": {"evolution": {"review_trigger": True}}},
        )
        is False
    )


def test_resolve_enable_task_loop_ignores_legacy_auto_scan():
    assert (
        JiuWenSwarmDeepAdapter._resolve_enable_task_loop(
            {"enable_task_loop": False},
            {"react": {"evolution": {"auto_scan": True}}},
        )
        is False
    )


def test_resolve_enable_task_loop_ignores_legacy_evolution_enabled():
    assert (
        JiuWenSwarmDeepAdapter._resolve_enable_task_loop(
            {"enable_task_loop": False},
            {
                "react": {"evolution": {
                    "enabled": True,
                    "signal_trigger": True,
                    "review_trigger": False,
                    "skill_create": False,
                }}
            },
        )
        is False
    )


def test_resolve_enable_task_loop_preserves_false_without_enforcers():
    assert (
        JiuWenSwarmDeepAdapter._resolve_enable_task_loop(
            {"enable_task_loop": False},
            {"react": {"evolution": {"skill_evolution": False}}},
        )
        is False
    )


# DeepAdapter only builds research_agent + browser_agent (agent mode).
# code_agent / explore_agent belong to CodeAdapter.

def test_deep_adapter_subagents_includes_optional_browser_and_configured_research():
    adapter = _TestableJiuWenSwarmDeepAdapter()
    adapter.set_workspace_dir("/tmp/jiuwenswarm-workspace")
    model = object()
    config = {
        "max_iterations": 9,
        "subagents": {
            "research_agent": {"enabled": True},
            "browser_agent": {"max_iterations": 7},
        },
    }

    with (
        patch.object(adapter, "_resolve_runtime_language", return_value="cn"),
        patch.object(adapter, "_browser_runtime_enabled", return_value=True),
        patch(
            "jiuwenswarm.server.runtime.agent_adapter.interface_deep.build_research_agent_config",
            return_value="research_spec",
        ) as mock_research,
        patch(
            "jiuwenswarm.server.runtime.agent_adapter.interface_deep.build_browser_agent_config",
            return_value="browser_spec",
        ) as mock_browser,
    ):
        subagents, _ = adapter.build_configured_subagents(model, config)

    assert [
        item.agent_card.name if hasattr(item, "agent_card") else item
        for item in subagents
    ] == ["statusline-setup", "research_spec", "browser_spec"]
    # sys_operation is forwarded so the subagent shares the parent's filesystem
    # boundary; this bare adapter has none configured.
    mock_research.assert_called_once_with(
        model,
        workspace="/tmp/jiuwenswarm-workspace",
        sys_operation=None,
        language="cn",
        max_iterations=9,
    )
    mock_browser.assert_called_once_with(
        model,
        workspace="/tmp/jiuwenswarm-workspace",
        sys_operation=None,
        language="cn",
        max_iterations=7,
    )


def test_deep_adapter_subagents_omits_research_without_explicit_enable():
    adapter = _TestableJiuWenSwarmDeepAdapter()
    adapter.set_workspace_dir("/tmp/jiuwenswarm-workspace")
    model = object()
    config = {"max_iterations": 9}

    with (
        patch.object(adapter, "_resolve_runtime_language", return_value="cn"),
        patch.object(adapter, "_browser_runtime_enabled", return_value=True),
        patch(
            "jiuwenswarm.server.runtime.agent_adapter.interface_deep.build_research_agent_config",
            return_value="research_spec",
        ) as mock_research,
        patch(
            "jiuwenswarm.server.runtime.agent_adapter.interface_deep.build_browser_agent_config",
            return_value="browser_spec",
        ) as mock_browser,
    ):
        subagents, _ = adapter.build_configured_subagents(model, config)

    # DeepAdapter: no research_agent configured; built-in status-line setup and
    # browser remain available.
    assert [
        item.agent_card.name if hasattr(item, "agent_card") else item
        for item in subagents
    ] == ["statusline-setup", "browser_spec"]
    mock_research.assert_not_called()
    mock_browser.assert_called_once_with(
        model,
        workspace="/tmp/jiuwenswarm-workspace",
        sys_operation=None,
        language="cn",
        max_iterations=DEFAULT_BROWSER_AGENT_MAX_ITERATIONS,
    )
