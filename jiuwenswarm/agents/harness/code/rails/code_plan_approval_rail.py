# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PlanApprovalRail — pending-approval lifecycle for plan mode exit.

``exit_plan_mode`` presents the plan for review
but the agent **stays in plan mode** until the user approves via chat.
Mode restoration (``restore_mode_after_plan_exit``) happens only after
approval, when the server injects the approved notification on the next
user turn and ``_ensure_code_mode_state`` runs.

The rail:

1. Detects successful ``exit_plan_mode`` calls in ``after_tool_call``.
2. Re-enters plan mode if the tool already restored it prematurely.
3. Rewrites tool_result copy from "plan ended" to "awaiting your review".
4. Stores ``_plan_approval_state`` for the server pending gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openjiuwen.core.common.logging import logger
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.code.prompt.plan_approval import PENDING_APPROVAL_MARKER

if TYPE_CHECKING:
    from openjiuwen.harness.deep_agent import DeepAgent


@dataclass
class PlanApprovalState:
    """Pending-approval state for one session.

    Stored on the agent instance after exit_plan_mode and consumed by the
    server layer on the next user request.
    """

    pending: bool = False
    plan_slug: str = ""
    plan_content: str = ""
    plan_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict."""
        return {
            "pending": self.pending,
            "plan_slug": self.plan_slug,
            "plan_content": self.plan_content,
            "plan_path": self.plan_path,
        }


PENDING_EXIT_RESULT_PREFIX = {
    "cn": "计划已提交，等待用户确认。\n计划文件：{plan_path}\n\n## 计划：\n",
    "en": "Plan submitted for your review.\nPlan file: {plan_path}\n\n## Plan:\n",
}


def _detect_language(agent: DeepAgent | None) -> str:
    """Return ``"cn"`` or ``"en"`` based on the agent's prompt builder."""
    if agent is None:
        return "cn"
    builder = getattr(agent, "system_prompt_builder", None)
    if builder is None:
        return "cn"
    return getattr(builder, "language", "cn")


class PlanApprovalRail(DeepAgentRail):
    """Rail that manages the plan-approval lifecycle.

    Priority ``76`` — runs after AgentModeRail (85) and ConfirmInterruptRail
    so that the tool has already executed and its result is available.
    """

    priority = 76

    def init(self, agent: DeepAgent) -> None:
        """Capture the agent reference.

        Args:
            agent: The parent DeepAgent.
        """
        self._agent = agent

    def uninit(self, agent: DeepAgent) -> None:
        """Clean up the stored agent reference."""
        self._agent = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """No-op — this rail only observes tool calls."""
        return

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """No-op — this rail only observes tool calls."""
        return

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Detect exit_plan_mode and store pending-approval state.

        Also appends a pending-approval text marker as a **fallback** so
        text-only channels (e.g. IM bridges) still see guidance on how to
        approve/reject the plan.

        Args:
            ctx: Callback context containing tool-name, result, and session.
        """
        if ctx.inputs.tool_name != "exit_plan_mode":
            return
        if ctx.extra.get("_skip_tool"):
            return
        # ConfirmInterrupt 等 hook 中断时工具未真正执行，不应进入待审批状态。
        if ctx.inputs.tool_result is None:
            return

        agent = self._agent
        if agent is None:
            logger.warning("[PlanApprovalRail] agent is None, skipping")
            return

        language = _detect_language(agent)

        # ── 1. Extract plan content from tool result ──
        tool_msg = ctx.inputs.tool_msg
        result_text = ""
        if tool_msg and hasattr(tool_msg, "content") and tool_msg.content:
            raw = tool_msg.content
            if isinstance(raw, str):
                result_text = raw
            elif isinstance(raw, list):
                # Extract text from structured content blocks
                for block in raw:
                    if isinstance(block, dict) and block.get("type") == "text":
                        result_text = block.get("text", "")
                        if result_text:
                            break
        if not result_text:
            result_text = str(ctx.inputs.tool_result or "")

        # ── 2. Extract plan body (strip prefix metadata) ──
        plan_content = self._extract_plan_body(result_text)

        # ── 3. Load plan metadata via agent ──
        plan_path = agent.get_plan_file_path(ctx.session)
        plan_path_str = str(plan_path) if plan_path else ""
        state = agent.load_state(ctx.session)

        # Safety net: patched exit_plan_mode must stay in plan until approval.
        if state.plan_mode.mode != "plan":
            agent.switch_mode(session=ctx.session, mode="plan")
            state = agent.load_state(ctx.session)
            logger.warning(
                "[PlanApprovalRail] Re-entered plan mode after premature exit restore"
            )

        if not plan_content and plan_path_str:
            try:
                plan_file = Path(plan_path_str)
                if plan_file.is_file():
                    plan_content = plan_file.read_text(encoding="utf-8").strip()
            except OSError:
                plan_content = ""
        if not plan_content:
            logger.warning("[PlanApprovalRail] no plan content extracted from exit_plan_mode result")
            plan_content = "(plan content unavailable)"

        # ── 4. Build and store approval state ──
        approval_state = PlanApprovalState(
            pending=True,
            plan_slug=state.plan_mode.plan_slug or "",
            plan_content=plan_content,
            plan_path=plan_path_str,
        )
        agent._plan_approval_state = approval_state  # pylint: disable=protected-access
        logger.info(
            "[PlanApprovalRail] exit_plan_mode detected, pending approval set "
            "for plan %s",
            approval_state.plan_slug,
        )

        # ── 5. Rewrite tool_result + append chat guidance marker ──
        marker = PENDING_APPROVAL_MARKER.get(language, PENDING_APPROVAL_MARKER["cn"])
        if tool_msg and hasattr(tool_msg, "content"):
            tool_msg.content = str(tool_msg.content) + marker

    @staticmethod
    def _extract_plan_body(result_text: str) -> str:
        """Strip the prefix metadata from the ExitPlanModeTool result.

        ExitPlanModeTool returns::

            规划模式已结束。
            计划文件：xxx

            ## 计划：
            <plan_body>

        We only keep the part after the ``## 计划：`` / ``## Plan:`` marker.

        Args:
            result_text: Full tool result text.
            language: ``"cn"`` or ``"en"``.

        Returns:
            The plan body text, or the full text if no marker is found.
        """
        for marker in ("## 计划：\n", "## Plan:\n"):
            if marker in result_text:
                return result_text.split(marker, 1)[1].strip()
        # Fallback: try to strip common prefixes
        lines = result_text.strip().split("\n", 10)
        if len(lines) > 5:
            return "\n".join(lines[5:]).strip()
        return result_text.strip()


__all__ = [
    "PENDING_EXIT_RESULT_PREFIX",
    "PlanApprovalRail",
    "PlanApprovalState",
]
