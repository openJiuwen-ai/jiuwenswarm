from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)
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
    return SimpleNamespace(
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_name,
            tool_args=dict(args),
            tool_result={"success": True} if result is None else result,
        ),
        extra={} if extra is None else extra,
        exception=None,
    )


def test_build_agent_identity_prompt_contains_stable_identity_and_task_strategy():
    prompt = build_agent_identity_prompt(language="zh")

    assert "You are 小艺Work, a personal agent" in prompt
    assert "小艺 work" not in prompt
    assert "# Identity" in prompt
    assert "# Task Execution Strategy" in prompt
    assert "# JiuwenSwarm 内部数据" not in prompt
    assert "## 输出文件放置规范" not in prompt
    assert "## 文件发送" not in prompt
    assert "## Symphony Orchestration" not in prompt
    assert "`symphony_compose_score`" not in prompt
    assert "# 消息说明" not in prompt


def test_work_code_and_design_share_the_same_static_prefix():
    from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import (
        build_code_system_prompt,
    )
    from jiuwenswarm.agents.harness.design.prompt.design_prompt_builder import (
        build_design_system_prompt,
    )

    work_prompt = build_agent_identity_prompt(language="zh")
    code_prompt = build_code_system_prompt()
    design_prompt = build_design_system_prompt()
    shared_prefix = work_prompt[: work_prompt.index("# Task Execution Strategy")]

    assert code_prompt.startswith(shared_prefix)
    assert design_prompt.startswith(shared_prefix)
    # The mode-specific tone section is deliberately the first static section
    # after Safety: the runtime Tools section (P30) is inserted between them
    # in the fully assembled prompt.
    assert code_prompt[len(shared_prefix) :].startswith("# Tone and style")
    assert design_prompt[len(shared_prefix) :].startswith("# Tone and style")


def test_all_modes_place_the_shared_system_section_before_regional_conventions():
    from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import (
        build_code_system_prompt,
    )
    from jiuwenswarm.agents.harness.design.prompt.design_prompt_builder import (
        build_design_system_prompt,
    )

    prompts = (
        build_agent_identity_prompt(language="zh"),
        build_code_system_prompt(),
        build_design_system_prompt(),
    )
    for prompt in prompts:
        assert prompt.index("# Content policy") < prompt.index("# System")
        assert prompt.index("# System") < prompt.index("# Regional conventions")


def test_design_mode_static_section_priorities_are_explicitly_ordered():
    from jiuwenswarm.agents.harness.design.prompt.design_prompt_builder import (
        DesignPromptPriority,
    )

    ordered = [
        DesignPromptPriority.SYSTEM,
        DesignPromptPriority.TONE_AND_STYLE,
        DesignPromptPriority.INTRO,
        DesignPromptPriority.CORE_CAPABILITIES,
    ]

    assert ordered == sorted(ordered)
    assert len(ordered) == len(set(ordered))


def test_code_and_design_tone_follow_tool_usage_rules_priority():
    from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import CodePromptPriority
    from jiuwenswarm.agents.harness.design.prompt.design_prompt_builder import (
        DesignPromptPriority,
    )

    for priority in (CodePromptPriority, DesignPromptPriority):
        assert priority.SAFETY == 13
        # agent-core's runtime Tool Usage Rules is priority 30 in the packaged
        # application.  Static guidance must follow it without relying on a
        # monkey-patch of an external module.
        assert priority.TONE_AND_STYLE == 31
        assert priority.INTRO == 32

    assert CodePromptPriority.DOING_TASKS == 33
    assert CodePromptPriority.USING_YOUR_TOOLS == 34
    assert CodePromptPriority.SESSION_GUIDANCE == 35
    assert CodePromptPriority.ACTIONS_WITH_CARE == 36
    assert DesignPromptPriority.CORE_CAPABILITIES == 33


@pytest.mark.parametrize(
    ("profile", "expected_after_tools"),
    (
        ("work", ("# Task Execution Strategy",)),
        ("code", ("# Tone and style", "# Code mode")),
        ("design", ("# Tone and style", "# Design mode")),
    ),
)
def test_runtime_builder_keeps_tools_between_safety_and_profile_guidance(
    profile, expected_after_tools
):
    """Guard the real DeepAgent path, not only a pre-flattened prompt string."""
    from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import (
        build_code_system_prompt,
    )
    from jiuwenswarm.agents.harness.design.prompt.design_prompt_builder import (
        build_design_system_prompt,
    )
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    initial_prompts = {
        "work": build_agent_identity_prompt(language="en"),
        "code": build_code_system_prompt(),
        "design": build_design_system_prompt(),
    }
    builder = SystemPromptBuilder(language="en")
    # This is exactly what agent-core does to a string passed as system_prompt:
    # it makes the whole rendered prompt one priority-10 identity section.
    builder.add_section(
        PromptSection(name="identity", content={"en": initial_prompts[profile]}, priority=10)
    )
    applied = []
    instance = SimpleNamespace(
        system_prompt_builder=builder,
        apply_prompt_builder_to_react_agent=lambda: applied.append(True),
    )
    if profile == "work":
        adapter = object.__new__(JiuWenSwarmDeepAdapter)
    else:
        adapter = object.__new__(JiuwenSwarmCodeAdapter)
        adapter._static_prompt_profile = profile
    adapter._instance = instance

    adapter._install_structured_static_prompt_sections()
    builder.add_section(
        PromptSection(name="tools", content={"en": "# Tool Usage Rules\n"}, priority=14)
    )
    prompt = builder.build()

    assert applied == [True]
    assert prompt.index("# Safety") < prompt.index("# Tool Usage Rules")
    for heading in expected_after_tools:
        assert prompt.index("# Tool Usage Rules") < prompt.index(heading)


def test_code_session_guidance_precedes_executing_actions_with_care():
    from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import (
        build_code_system_prompt,
    )

    prompt = build_code_system_prompt()
    assert prompt.index("# Session-specific guidance") < prompt.index(
        "# Executing actions with care"
    )


def test_find_skills_policy_lives_in_tool_usage_rules_not_skills_preamble():
    from openjiuwen.core.foundation.tool.base import ToolCard
    from openjiuwen.harness.prompts.sections import context
    from jiuwenswarm.agents.harness.common.prompt import skills_goal_override

    ability_manager = SimpleNamespace(list=lambda: [ToolCard(name="read_file")])
    tools_content = context.build_tools_content(ability_manager, language="en")

    assert tools_content is not None
    assert "# Tool Usage Rules" in tools_content
    assert "Only call tools that are actually available in the current request." in tools_content
    assert "Tool results are the source of truth" not in tools_content
    assert "## Skill Discovery and Installation (`find-skills-win`)" in tools_content
    assert "Skill Discovery and Installation" not in skills_goal_override._SKILLS_PREAMBLE_EN
    assert skills_goal_override._SKILLS_PREAMBLE_EN.startswith("# Skills")


def test_office_static_task_execution_follows_agent_core_tools_priority():
    """The real agent-core Tools section is P30 in the packaged application."""
    from openjiuwen.core.foundation.tool.base import ToolCard
    from openjiuwen.harness.prompts.sections import context

    ability_manager = SimpleNamespace(list=lambda: [ToolCard(name="read_file")])
    section = context.build_tools_section(ability_manager, language="en")

    assert section is not None
    assert section.priority == 14

    runtime_section = PromptSection(
        name=section.name,
        content=section.content,
        priority=30,
    )

    builder = SystemPromptBuilder(language="en")
    builder.add_section(PromptSection(name="safety", content={"en": "# Safety\n"}, priority=13))
    builder.add_section(runtime_section)
    # Cover the real unpatched agent-core priority seen in capture files: Task
    # Execution remains after Tools even when the optional P14 patch cannot
    # apply to the runtime import path.
    builder.add_section(PromptSection(name="task", content={"en": "# Task\n"}, priority=31))
    prompt = builder.build()
    assert prompt.index("# Safety") < prompt.index("# Tool Usage Rules") < prompt.index("# Task")


@pytest.mark.asyncio
async def test_ordered_context_assemble_rail_sets_tools_priority_locally():
    from openjiuwen.harness.rails.context_engineer.context_assemble_rail import (
        ContextAssembleRail,
    )
    from jiuwenswarm.agents.harness.common.rails import tool_usage_prompt_rail
    from jiuwenswarm.agents.harness.common.rails.tool_usage_prompt_rail import (
        OrderedContextAssembleRail,
    )

    builder = SystemPromptBuilder(language="en")
    rail = OrderedContextAssembleRail()
    rail.system_prompt_builder = builder
    rail.workspace = object()
    rail._ability_manager = SimpleNamespace()
    upstream_section = PromptSection(
        name="tools", content={"en": "# Upstream Tools\n"}, priority=30
    )
    product_section = PromptSection(
        name="tools", content={"en": "# Tool Usage Rules\n"}, priority=30
    )

    async def write_upstream_tools(_self, _ctx):
        builder.add_section(upstream_section)

    with (
        patch.object(ContextAssembleRail, "before_model_call", write_upstream_tools),
        patch.object(
            tool_usage_prompt_rail.context_sections,
            "build_tools_section",
            return_value=product_section,
        ),
    ):
        await rail.before_model_call(SimpleNamespace())

    section = builder.get_section("tools")
    assert section is product_section
    assert section.priority == 14


def test_runtime_subagent_usage_rules_are_present_while_office_omits_outer_rules():
    from jiuwenswarm.agents.harness.common.prompt.prompt_builder import (
        _runtime_env_message_rules_text,
    )
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter

    assert "## Subagent Usage Rules" not in _runtime_env_message_rules_text(False)
    assert "## Subagent Usage Rules" in _runtime_env_message_rules_text(True)
    # Runtime guidance is retained for every mode. Office alone omits the
    # separate top-level task-tool section.
    assert JiuWenSwarmDeepAdapter._include_runtime_subagent_usage_rules(None) is True
    assert JiuWenSwarmDeepAdapter._include_outer_subagent_usage_rules(None) is False
    assert JiuwenSwarmCodeAdapter._include_outer_subagent_usage_rules(None) is False


@pytest.mark.asyncio
async def test_office_subagent_rail_removes_only_the_prompt_section():
    from openjiuwen.harness.prompts.sections import SectionName
    from openjiuwen.harness.rails.subagent import SubagentRail
    from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import (
        BrowserTaskPromptRail,
    )

    async def inject_task_section(rail, ctx):
        del rail, ctx
        builder.add_section(
            PromptSection(name=SectionName.TASK_TOOL, content={"en": "# Subagent Usage Rules\n"}, priority=85)
        )

    builder = SystemPromptBuilder(language="en")
    office_rail = BrowserTaskPromptRail(include_usage_rules=False)
    office_rail.system_prompt_builder = builder
    with patch.object(SubagentRail, "before_model_call", inject_task_section):
        await office_rail.before_model_call(SimpleNamespace())
    assert "# Subagent Usage Rules" not in builder.build()

    code_rail = BrowserTaskPromptRail(include_usage_rules=True)
    code_rail.system_prompt_builder = builder
    with patch.object(SubagentRail, "before_model_call", inject_task_section):
        await code_rail.before_model_call(SimpleNamespace())
    assert "# Subagent Usage Rules" in builder.build()


@pytest.mark.asyncio
async def test_code_tool_usage_rail_injects_rules_after_safety():
    from openjiuwen.core.foundation.tool.base import ToolCard
    from jiuwenswarm.agents.harness.common.rails.tool_usage_prompt_rail import (
        ToolUsagePromptRail,
    )

    builder = SystemPromptBuilder(language="en")
    builder.add_section(
        PromptSection(name="safety", content={"en": "# Safety\n"}, priority=13)
    )
    builder.add_section(PromptSection(name="intro", content={"en": "# Intro\n"}, priority=31))
    agent = SimpleNamespace(
        system_prompt_builder=builder,
        ability_manager=SimpleNamespace(list=lambda: [ToolCard(name="read_file")]),
    )
    rail = ToolUsagePromptRail()
    rail.init(agent)

    await rail.before_model_call(SimpleNamespace())

    prompt = builder.build()
    assert prompt.index("# Safety") < prompt.index("# Tool Usage Rules")
    assert prompt.index("# Tool Usage Rules") < prompt.index("# Intro")
    assert "## Skill Discovery and Installation (`find-skills-win`)" in prompt


@pytest.mark.asyncio
async def test_response_prompt_rail_no_longer_injects_input_or_output_sections():
    """Input/Output rules moved from ResponsePromptRail into RuntimePromptRail's
    ``env`` section. ResponsePromptRail now only syncs the A2UI section, so it
    must not leave behind ``input`` / ``output`` / ``response`` sections.
    """
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
    assert not builder.has_section("input")
    assert not builder.has_section("output")
    assert not builder.has_section("response")
    assert "## Input Instructions" not in prompt
    assert "## Output Rules" not in prompt
    assert "### Output Language" not in prompt
    assert "### Model Name Answers" not in prompt
    assert "## Subagent Usage Rules" not in prompt
    assert "# 消息说明" not in prompt


@pytest.mark.asyncio
async def test_runtime_env_section_includes_message_rules_subsections():
    """RuntimePromptRail's ``env`` section now hosts the Input Instructions /
    Output Rules / Subagent Usage Rules subsections (headings demoted one level)
    so they read as subsections of ``# Runtime Environment`` and are shared
    across office / code / design / team profiles.
    """
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
    assert "## Input Instructions" in prompt
    assert "### User Messages" in prompt
    assert "### System Messages" in prompt
    assert "## Output Rules" in prompt
    assert "### Final Response Rules" in prompt
    assert "### Artifact and Deliverable Rules" in prompt
    assert "### Output Language" in prompt
    assert "### Model Name Answers" in prompt
    assert "## Subagent Usage Rules" in prompt
    assert builder.has_section("env")
    assert not builder.has_section("input")
    assert not builder.has_section("output")
    assert "# 消息说明" not in prompt
    # Text output (does not apply to tool calls) was relocated from the
    # per-mode prompt builders (code/design) into the shared Runtime
    # Environment ``env`` section so all three modes (office/code/design)
    # receive the same output-efficiency guidance.
    assert "## Text output (does not apply to tool calls)" in prompt
    assert "Go straight to the point." in prompt


@pytest.mark.asyncio
async def test_symphony_orchestration_rail_respects_config_snapshot():
    enabled_builder = SystemPromptBuilder(language="cn")
    enabled_agent = _FakeAgent(enabled_builder)
    enabled_ctx = AgentCallbackContext(
        agent=enabled_agent,
        inputs=SimpleNamespace(
            tools=[SimpleNamespace(name="symphony_compose_score")],
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
            tools=[SimpleNamespace(name="symphony_compose_score")],
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
    assert "## Symphony Orchestration" in enabled_prompt
    assert "`symphony_compose_score`" in enabled_prompt
    assert "## Symphony Orchestration" not in disabled_prompt
    assert "`symphony_compose_score`" not in disabled_prompt


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
            tools=[SimpleNamespace(name="symphony_compose_score")],
        ),
        session=_FakeSession(),
        extra={},
    )

    rail = SymphonyOrchestrationRail()
    rail.init(agent)
    await rail.before_model_call(ctx)

    prompt = builder.build()
    assert "## Symphony Orchestration" in prompt
    assert "`symphony_compose_score`" in prompt
    assert "exact identifiers or names" in prompt
    assert "Do not omit this field" in prompt
    assert "skill_branch_explore" not in prompt


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
        "symphony_compose_score",
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
        "symphony_compose_score",
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
        "symphony_compose_score",
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
        "symphony_compose_score",
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
            tools=[SimpleNamespace(name="symphony_compose_score")],
        ),
        session=_FakeSession(),
        extra={},
    )

    rail = SymphonyOrchestrationRail()
    rail.init(agent)
    await rail.before_model_call(ctx)

    assert "## Symphony Orchestration" not in builder.build()


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
            "symphony_read_score",
            "symphony_refresh_score",
            "symphony_compose_score",
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
        "symphony_read_score",
        "symphony_refresh_score",
        "symphony_compose_score",
    ]
    assert fake_resource.added == [
        "symphony_read_score",
        "symphony_refresh_score",
        "symphony_compose_score",
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
        "symphony_read_score",
        "symphony_refresh_score",
        "symphony_compose_score",
    ]


@pytest.mark.asyncio
async def test_runtime_environment_section_participates_in_priority_order():
    builder = SystemPromptBuilder(language="cn")
    builder.add_section(PromptSection(name="identity", content={"cn": "identity"}, priority=10))
    builder.add_section(PromptSection(name="tools", content={"cn": "# 可用工具"}, priority=30))

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
    # The openjiuwen WORKSPACE section is removed and its content folded into
    # the consolidated directory_boundaries section (priority 96, last).
    ordered_markers = [
        "identity",
        "# 可用工具",
        "# 运行环境",
        "# 目录与运行时上下文",
    ]
    positions = [prompt.index(marker) for marker in ordered_markers]
    assert positions == sorted(positions)
    assert builder.has_section("env")
    assert not builder.has_section("time")
    assert not builder.has_section("runtime.model_answer_policy")
    assert not builder.has_section("language_output")
    assert not builder.has_section("runtime")
    assert "# 运行时状态" not in prompt
    assert builder.has_section("directory_boundaries")


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


def test_task_planning_rail_injects_todo_prompt_section():
    # inject_prompt=True 是有意决策：向 system prompt 注入 todo 规划段，
    # 仅纯简单对话（无工具调用）才不用 todo（interface_deep._build_task_planning_rail）。
    rail = JiuWenSwarmDeepAdapter._build_task_planning_rail()
    if rail is None:
        pytest.skip("TaskPlanningRail is unavailable with the installed openjiuwen API")
    assert rail.inject_prompt is True


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
    assert "# Directory and Runtime Context" in prompt
    assert "## Runtime Directory Context" in prompt
    assert "# Working Directory Runtime Values" not in prompt
    assert "The project directory is your current workspace" in prompt
    assert f"the current project directory is: `{project_dir}`" in prompt
    assert "Agent internal data directory" in prompt
    assert "## 小艺 work Internal Data Directories" in prompt
    assert str(project_dir) in prompt
    # current_dir is the runtime cwd; it legitimately appears in the Runtime
    # Directory Context, but must NOT leak into Other Authorized Directories
    # (it is filtered because it equals cwd).
    assert "Other Authorized Directories" not in prompt or (
        str(current_dir) not in prompt.split("Other Authorized Directories")[-1]
    )
    assert str(stale_dir) not in prompt
    # extra_dir is a valid trusted dir distinct from project/cwd; it SHOULD
    # appear in Other Authorized Directories.
    assert str(extra_dir) in prompt
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
    assert "# 目录与运行时上下文" in prompt
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
    assert "# 目录与运行时上下文" in builder.build()

    runtime_rail.set_channel("a2a")
    await runtime_rail.before_model_call(ctx)
    assert "# 目录与运行时上下文" not in builder.build()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ("__cron__", "cron"))
async def test_runtime_prompt_directory_boundaries_for_cron_channels(
    tmp_path,
    monkeypatch,
    channel,
):
    builder = SystemPromptBuilder(language="cn")
    agent = _FakeAgent(builder)
    agent_data_dir = tmp_path / "agent-data"
    project_dir = tmp_path / "定时任务-workspace"
    agent_data_dir.mkdir()
    project_dir.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_agent_workspace_dir",
        lambda: agent_data_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_user_workspace_dir",
        lambda: tmp_path / "jiuwenswarm-data",
    )

    runtime_rail = RuntimePromptRail(language="cn", channel=channel)
    runtime_rail.init(agent)
    runtime_rail.set_runtime_paths(cwd=str(project_dir), project_dir=str(project_dir))
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=_FakeSession(),
        extra={},
    )

    await runtime_rail.before_model_call(ctx)

    prompt = builder.build()
    assert "# 目录与文件操作边界" in prompt
    assert f"当前项目目录是：`{project_dir}`" in prompt
    assert str(agent_data_dir) in prompt


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
    assert "## 项目目录规则" in prompt
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
    assert builder.get_section(rail.SECTION_NAME).priority == 56

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


def test_resolve_enable_task_loop_forces_true_when_skill_create_enabled(monkeypatch):
    monkeypatch.delenv("SKILL_CREATE", raising=False)
    assert (
        JiuWenSwarmDeepAdapter._resolve_enable_task_loop(
            {"enable_task_loop": False},
            {"evolution": {"skill_create": True}},
        )
        is True
    )


def test_resolve_enable_task_loop_forces_true_when_review_trigger_enabled(monkeypatch):
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    assert (
        JiuWenSwarmDeepAdapter._resolve_enable_task_loop(
            {"enable_task_loop": False},
            {"evolution": {"review_trigger": True}},
        )
        is True
    )


def test_resolve_enable_task_loop_forces_true_when_legacy_auto_scan_enabled(monkeypatch):
    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    assert (
        JiuWenSwarmDeepAdapter._resolve_enable_task_loop(
            {"enable_task_loop": False},
            {"evolution": {"auto_scan": True}},
        )
        is True
    )


def test_resolve_enable_task_loop_preserves_false_when_only_evolution_enabled(monkeypatch):
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    monkeypatch.delenv("EVOLUTION_SIGNAL_TRIGGER", raising=False)
    monkeypatch.delenv("SKILL_CREATE", raising=False)
    assert (
        JiuWenSwarmDeepAdapter._resolve_enable_task_loop(
            {"enable_task_loop": False},
            {
                "evolution": {
                    "enabled": True,
                    "signal_trigger": True,
                    "review_trigger": False,
                    "skill_create": False,
                }
            },
        )
        is False
    )


def test_resolve_enable_task_loop_preserves_false_without_enforcers(monkeypatch):
    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    monkeypatch.delenv("SKILL_CREATE", raising=False)
    assert (
        JiuWenSwarmDeepAdapter._resolve_enable_task_loop(
            {"enable_task_loop": False},
            {"evolution": {"enabled": False, "skill_create": False}},
        )
        is False
    )


# DeepAdapter only builds research_agent + browser_agent (agent mode).
# code_agent belongs to CodeAdapter.

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

    assert subagents == ["research_spec", "browser_spec"]
    mock_research.assert_called_once_with(
        model,
        workspace="/tmp/jiuwenswarm-workspace",
        language="cn",
        max_iterations=9,
    )
    mock_browser.assert_called_once_with(
        model,
        workspace="/tmp/jiuwenswarm-workspace",
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

    # DeepAdapter: no research_agent configured, browser enabled
    assert subagents == ["browser_spec"]
    mock_research.assert_not_called()
    mock_browser.assert_called_once_with(
        model,
        workspace="/tmp/jiuwenswarm-workspace",
        language="cn",
        max_iterations=DEFAULT_BROWSER_AGENT_MAX_ITERATIONS,
    )
