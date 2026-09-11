# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Inject the dynamic Tool Usage Rules section without workspace context files."""

from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
import openjiuwen.harness.prompts.sections.context as context_sections
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.context_engineer.context_assemble_rail import (
    ContextAssembleRail,
)

_TOOL_USAGE_SECTION_PRIORITY = 14

# Optional platform capabilities should not consume the default Xiaoyi Work
# model-tool budget. Their implementations remain available for explicit
# integrations; this list controls only model visibility.
XIAOYI_HIDDEN_DEFAULT_TOOL_NAMES = frozenset(
    {
        # LLM Wiki
        "wiki_ingest",
        "wiki_query",
        "wiki_lint",
        # Audio processing
        "audio_metadata",
        "audio_transcribe",
        "audio_understanding",
        # Third-party channel administration
        "configure_channel",
        "get_wechat_login_status",
        # Persistent Goal protocol
        "submit",
        "submit_goal_report",
        "get_current_goal",
        # Skill self-evolution protocol. Keep the whole workflow hidden so
        # the model is never left with only an unusable intermediate step.
        "prepare_skill_evolution",
        "evolve_review_task",
        "list_skill_experiences",
        "read_skill_experiences",
        "evolve_skill_experiences",
        "simplify_skill_experiences",
        # Code execution (openjiuwen CodeTool). The code/design/deep profiles
        # all expose bash/powershell; a separate python/js interpreter tool is
        # redundant for the default Xiaoyi Work budget.
        "code",
    }
)


def _model_tool_name(tool) -> str:  # type: ignore[no-untyped-def]
    """Return a name from an OpenAI-schema dict or an OpenJiuwen ToolCard."""
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            return str(function.get("name", "") or "")
        return str(tool.get("name", "") or "")
    return str(getattr(tool, "name", "") or "")


def filter_xiaoyi_default_model_tools(tools):  # type: ignore[no-untyped-def]
    """Return a model-tool list without product-disabled default tools."""
    if not isinstance(tools, list):
        return tools
    return [tool for tool in tools if _model_tool_name(tool) not in XIAOYI_HIDDEN_DEFAULT_TOOL_NAMES]


class XiaoyiDefaultToolVisibilityRail(DeepAgentRail):
    """Remove optional platform tools at the final model-call boundary.

    Goal and Skill Evolution tools are added by upstream rails after adapters
    assemble their initial cards. Filtering here therefore covers office, code,
    and design without deleting the underlying optional implementations.
    """

    priority = -1000

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        if inputs is None:
            return
        tools = getattr(inputs, "tools", None)
        filtered = filter_xiaoyi_default_model_tools(tools)
        if isinstance(tools, list) and len(filtered) != len(tools):
            inputs.tools = filtered


class ToolUsagePromptRail(DeepAgentRail):
    """Render rules for the tools that are actually registered on this agent.

    Code and Design intentionally do not register ``ContextAssembleRail``:
    that rail also injects workspace context files.  This narrow rail reuses
    its tool-section builder while keeping those mode prompts free of unrelated
    workspace content.  The product patch gives the section priority 14,
    immediately after the shared Safety section (13), regardless of rail
    execution order.
    """

    priority = 6

    def __init__(self) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._ability_manager = None

    def init(self, agent) -> None:  # type: ignore[no-untyped-def]
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._ability_manager = getattr(agent, "ability_manager", None)

    def uninit(self, agent) -> None:  # type: ignore[no-untyped-def]
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section("tools")
        self.system_prompt_builder = None
        self._ability_manager = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if self.system_prompt_builder is None:
            return
        language = getattr(self.system_prompt_builder, "language", "en") or "en"
        # Resolve from the module at call time so the shared prompt override's
        # placement patch also applies to Code and Design.
        section = context_sections.build_tools_section(self._ability_manager, language)
        if section is None:
            self.system_prompt_builder.remove_section("tools")
            return
        # This rail is the last writer for Code/Design.  Set the final object
        # priority here rather than relying on an earlier builder monkey patch.
        section.priority = _TOOL_USAGE_SECTION_PRIORITY
        self.system_prompt_builder.add_section(section)


class _MemoryFilteredWorkspace:
    """Hide legacy memory paths from context reads without changing the workspace."""

    def __init__(self, workspace):
        self._workspace = workspace

    def get_node_path(self, node):
        if getattr(node, "value", node) in {"USER.md", "MEMORY.md", "memory"}:
            return None
        return self._workspace.get_node_path(node)

    def __getattr__(self, name):
        return getattr(self._workspace, name)


class OrderedContextAssembleRail(ContextAssembleRail):
    """Product-scoped Context rail with deterministic Tool Usage placement.

    ``ContextAssembleRail`` imports its tool-section builder at module load,
    before the product's tool-text override can replace that helper. Rebuild
    the final Tools section here after the upstream rail runs. Keeping this
    behavior on the product-owned subclass avoids changing unrelated agents
    that happen to share the Python process.
    """

    @property
    def workspace(self):
        from jiuwenswarm.common.config import get_config
        from jiuwenswarm.agents.harness.common.memory.external_memory_config import (
            is_legacy_workspace_memory_enabled,
        )

        workspace = getattr(self, "_context_workspace", None)
        if workspace is None:
            return None
        config = get_config()
        return workspace if is_legacy_workspace_memory_enabled(config) else _MemoryFilteredWorkspace(workspace)

    @workspace.setter
    def workspace(self, value):
        self._context_workspace = value

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        await super().before_model_call(ctx)
        if self.system_prompt_builder is None or self.workspace is None:
            return
        language = getattr(self.system_prompt_builder, "language", "en") or "en"
        section = context_sections.build_tools_section(self._ability_manager, language)
        if section is None:
            self.system_prompt_builder.remove_section("tools")
            return
        section.priority = _TOOL_USAGE_SECTION_PRIORITY
        self.system_prompt_builder.add_section(section)
