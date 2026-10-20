"""Build one desktop subagent without coupling its two backends."""

from __future__ import annotations

from openjiuwen.core.context_engine import ToolResultWindowProcessorConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.rails.context_engineer import ContextProcessorRail
from openjiuwen.harness.rails.multimodal_context_summarizer_rail import MultimodalContextSummarizerRail
from openjiuwen.harness.schema.config import SubAgentConfig

from .prompt import build_system_prompt
from .rail import ComputerUseRail


class CaptureContextRail(ContextProcessorRail):
    """Give each materialized child its own capture-result window."""

    def __init__(self, keep: int):
        self.keep = keep
        super().__init__(processors=[("ToolResultWindowProcessor", ToolResultWindowProcessorConfig(
            tool_names=["get_window_state", "zoom", "cua_repl"],
            keep_last_k=keep,
            trim_size=200,
        ))], preset=False)

    def fork_for_agent(self):
        return CaptureContextRail(self.keep)


class CaptureImageRail(MultimodalContextSummarizerRail):
    """Keep screenshot retention local to each child conversation."""

    def __init__(self, keep: int):
        self.keep = keep
        super().__init__(keep)

    def fork_for_agent(self):
        return CaptureImageRail(self.keep)


def build_cua_agent_config(
    model,
    *,
    card: AgentCard,
    workspace=None,
    sys_operation=None,
    language="cn",
    max_iterations=100,
    cua_snapshot_keep_last_k=3,
    cua_pause_on_user_input=False,
    cua_repl_enabled=True,
    cua_repl_python=None,
):
    """Compose public core tools/rails, adding a task-owned Python REPL.

    Use the generic DeepAgent factory: the core CUA factory unconditionally
    preflights its screenshot backend, even when only the separate engine is
    needed. The original tools and their execution implementations are reused.
    """
    keep = cua_snapshot_keep_last_k
    if type(keep) is not int or keep < 1:
        raise ValueError("snapshot_keep_last_k must be a positive integer")
    return SubAgentConfig(
        agent_card=card,
        system_prompt=build_system_prompt(language, repl_enabled=cua_repl_enabled),
        model=model,
        workspace=workspace,
        sys_operation=sys_operation,
        language=language,
        max_iterations=max_iterations,
        parallel_tool_calls=False,
        rails=[
            ComputerUseRail(repl_enabled=cua_repl_enabled, python=cua_repl_python,
                            pause_on_user_input=cua_pause_on_user_input),
            CaptureContextRail(keep),
            CaptureImageRail(keep),
        ],
    )
