# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PlanApprovalInterruptRail — instant plan approval dialog for exit_plan_mode.

Aligns with Claude Code: ``exit_plan_mode`` triggers an **immediate** user
interaction showing the plan content, and mode restoration happens inside
the tool call itself (not deferred to the next user turn).

How it works:
1. LLM calls ``exit_plan_mode``.
2. This rail intercepts BEFORE the tool executes.
3. Plan file is read and presented in the interrupt message.
4. User sees the plan and chooses [Approve] or [Reject].
5. **Approve**: tool executes → ``ExitPlanModeTool.invoke()`` calls
   ``restore_mode_after_plan_exit()`` immediately → mode restored.
6. **Reject**: tool skipped → LLM gets error → stays in plan mode → can revise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.interrupt.confirm_rail import (
    ConfirmInterruptRail,
)
from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult

from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    PLAN_EXECUTE_CTX_KEY,
    PLAN_EXECUTE_PAYLOAD_KEY,
    PLAN_REVISE_PAYLOAD_KEY,
    PLAN_SKIP_PAYLOAD_KEY,
    PLAN_SKIP_TURN_OUTPUT,
    wrap_plan_revision_feedback,
)

if TYPE_CHECKING:
    from openjiuwen.harness.deep_agent import DeepAgent

# ── Plan approval interrupt message templates ──────────────────────────

_PLAN_APPROVAL_MESSAGE_CN = """\
**计划审批**

Agent 已完成计划制定，等待你审批：

{plan_preview}

---
请选择：
- **批准**：退出 plan 模式，开始执行计划
- **拒绝**：留在 plan 模式，Agent 将根据你的反馈修改计划"""

_PLAN_APPROVAL_MESSAGE_EN = """\
**Plan Approval**

The agent has completed a plan and is requesting your approval:

{plan_preview}

---
Choose:
- **Approve**: Exit plan mode and begin implementation
- **Reject**: Stay in plan mode, the agent will revise the plan based on your feedback"""

_PLAN_EMPTY_MESSAGE_CN = """\
**计划审批**

Agent 请求退出 plan 模式，但计划文件为空。

---
请选择：
- **批准**：退出 plan 模式（无计划内容）
- **拒绝**：留在 plan 模式，让 Agent 先写好计划"""

_PLAN_EMPTY_MESSAGE_EN = """\
**Plan Approval**

The agent is requesting to exit plan mode, but the plan file is empty.

---
Choose:
- **Approve**: Exit plan mode (no plan content)
- **Reject**: Stay in plan mode, let the agent write a plan first"""

# Truncate plan preview to this many characters to keep the dialog readable
_MAX_PLAN_PREVIEW_CHARS = 3000

_PLAN_APPROVAL_TITLE_CN = "**计划审批**"
_PLAN_APPROVAL_TITLE_EN = "**Plan Approval**"
_PLAN_APPROVAL_INTRO_CN = "Agent 已完成计划制定，等待你审批："
_PLAN_APPROVAL_INTRO_EN = "The agent has completed a plan and is requesting your approval:"
_PLAN_EMPTY_INTRO_CN = "Agent 请求退出 plan 模式，但计划文件为空。"
_PLAN_EMPTY_INTRO_EN = "The agent is requesting to exit plan mode, but the plan file is empty."
_PLAN_CHOICE_MARKERS = ("\n---\n请选择：", "\n---\nChoose:")


def is_plan_approval_message(message: str) -> bool:
    """Return whether ``message`` is a plan-approval interrupt copy."""
    normalized = message.strip()
    return _PLAN_APPROVAL_TITLE_CN in normalized or _PLAN_APPROVAL_TITLE_EN in normalized


def strip_inline_plan_approval_choices(message: str) -> str:
    """Remove the text-only approve/reject list from a plan-approval message."""
    for marker in _PLAN_CHOICE_MARKERS:
        if marker in message:
            return message.split(marker, 1)[0].rstrip()
    return message


def extract_plan_approval_content(message: str) -> tuple[str, str]:
    """Extract ``(plan_content, language)`` from a plan-approval message."""
    stripped = strip_inline_plan_approval_choices(message).strip()
    if stripped.startswith(_PLAN_APPROVAL_TITLE_EN):
        body = stripped[len(_PLAN_APPROVAL_TITLE_EN):].lstrip()
        if body.startswith(_PLAN_EMPTY_INTRO_EN):
            return "", "en"
        if body.startswith(_PLAN_APPROVAL_INTRO_EN):
            body = body[len(_PLAN_APPROVAL_INTRO_EN):].lstrip()
        return body.strip(), "en"

    if stripped.startswith(_PLAN_APPROVAL_TITLE_CN):
        body = stripped[len(_PLAN_APPROVAL_TITLE_CN):].lstrip()
        if body.startswith(_PLAN_EMPTY_INTRO_CN):
            return "", "cn"
        if body.startswith(_PLAN_APPROVAL_INTRO_CN):
            body = body[len(_PLAN_APPROVAL_INTRO_CN):].lstrip()
        return body.strip(), "cn"

    return stripped, "cn"


def build_plan_approval_options_from_message(message: str) -> list[dict[str, str]]:
    """Build structured approve/reject options from a plan-approval message."""
    stripped = strip_inline_plan_approval_choices(message).strip()
    _, language = extract_plan_approval_content(message)

    if language == "en":
        if _PLAN_EMPTY_INTRO_EN in stripped:
            return [
                {"label": "Approve", "value": "approve", "description": "Exit plan mode (no plan content)"},
                {"label": "Reject", "value": "reject", "description":
                    "Stay in plan mode, let the agent write a plan first"},
            ]
        return [
            {"label": "Approve", "value": "approve", "description": "Exit plan mode and begin implementation"},
            {"label": "Reject", "value": "reject", "description":
                "Stay in plan mode, the agent will revise the plan based on your feedback"},
        ]

    if _PLAN_EMPTY_INTRO_CN in stripped:
        return [
            {"label": "批准", "value": "approve", "description": "退出 plan 模式（无计划内容）"},
            {"label": "拒绝", "value": "reject", "description": "留在 plan 模式，让 Agent 先写好计划"},
        ]
    return [
        {"label": "批准", "value": "approve", "description": "退出 plan 模式，开始执行计划"},
        {"label": "拒绝", "value": "reject", "description": "留在 plan 模式，Agent 将根据你的反馈修改计划"},
    ]


class PlanApprovalInterruptRail(ConfirmInterruptRail):
    """Interrupt ``exit_plan_mode`` to show the plan for user approval.

    Extends ``ConfirmInterruptRail`` with a plan-aware interrupt message
    that reads the plan file and presents it inline.  Mode restoration
    happens inside ``ExitPlanModeTool.invoke()`` when the user approves.

    Priority 78 — runs BEFORE the generic ``CodeConfirmInterruptRail`` (80)
    but AFTER ``AgentModeRail`` (85) so plan mode enforcement is active.
    """

    priority = 78

    def __init__(self) -> None:
        super().__init__(tool_names=["exit_plan_mode"])
        self._agent: "DeepAgent | None" = None

    # ── Lifecycle ───────────────────────────────────────────────────

    def init(self, agent: "DeepAgent") -> None:
        """Capture the agent reference for plan file reading.

        Args:
            agent: The parent DeepAgent.
        """
        self._agent = agent

    def uninit(self, agent: "DeepAgent") -> None:  # noqa: ARG002
        """Release the agent reference."""
        self._agent = None

    # ── Interrupt logic ──────────────────────────────────────────────

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[ToolCall],
        user_input: Optional[Any],
        auto_confirm_config: Optional[dict] = None,
    ):
        """Override to inject plan content into the interrupt message.

        On first call (user_input is None): reads the plan file and builds
        a rich approval message showing the plan content.

        On resume: delegates to ``ConfirmInterruptRail.resolve_interrupt()``
        which handles ``ConfirmPayload`` deserialization and approve/reject.

        Args:
            ctx: Agent callback context.
            tool_call: The tool call being intercepted.
            user_input: User response from resume (None on first call).
            auto_confirm_config: Current auto-confirm settings.
        """
        # First call — show plan content in the interrupt message
        if user_input is None and tool_call is not None:
            plan_content = self._read_plan_content(ctx)
            language = self._detect_language()
            message = self._build_approval_message(plan_content, language)
            return self.interrupt(
                InterruptRequest(
                    message=message,
                    payload_schema=self.request.payload_schema,
                    auto_confirm_key="exit_plan_mode",
                )
            )

        # Web 的"跳过"：走 reject 通道（不退出 plan），但必须结束本轮，
        # 否则模型收到普通 reject 后会继续思考并再次调用 exit_plan_mode。
        # 该标记只由 Web 的 plan_skip 选项产生，TUI 的 reject 不受影响。
        plan_skip = bool(
            isinstance(user_input, dict) and user_input.get(PLAN_SKIP_PAYLOAD_KEY)
        )
        # Web 的"执行"：批准后只让 exit_plan_mode 跑完退出 plan，本轮就结束；
        # 真正的执行由前端补发的普通消息开启新一轮。TUI 发的是纯 approve，没有
        # 这个标记，仍在同一轮里继续实现。
        plan_execute = bool(
            isinstance(user_input, dict) and user_input.get(PLAN_EXECUTE_PAYLOAD_KEY)
        )
        plan_revise = bool(
            isinstance(user_input, dict) and user_input.get(PLAN_REVISE_PAYLOAD_KEY)
        )
        # 只包 Web「下一步」。TUI reject 没有 plan_revise，假回执保持原文。
        if plan_revise:
            user_input = {
                **user_input,
                "feedback": wrap_plan_revision_feedback(
                    str(user_input.get("feedback") or ""),
                    self._detect_language(),
                ),
            }

        # Resume — delegate to parent (handles ConfirmPayload.approved)
        decision = await super().resolve_interrupt(
            ctx, tool_call, user_input, auto_confirm_config
        )
        # 标记设在拿到结果之后：父类可能把这次恢复判成拒绝（例如 payload 校验
        # 失败），那时 exit_plan_mode 不会执行，本轮也就不该被"执行"标记提前结束。
        if plan_execute and not isinstance(decision, RejectResult):
            ctx.extra[PLAN_EXECUTE_CTX_KEY] = True
        # When the user rejects, _skip_tool() sets ctx.extra["_skip_tool"]=True,
        # but _railed_execute_single_tool_call pops it before after_tool_call
        # runs.  Set a persistent marker so CodeAgentModeRail.after_tool_call()
        # can still detect the rejection and skip mode restoration.
        if isinstance(decision, RejectResult):
            ctx.extra["_plan_rejected"] = True
            if plan_skip:
                ctx.extra["_plan_skipped"] = True
                ctx.request_force_finish(
                    {
                        "output": PLAN_SKIP_TURN_OUTPUT.get(
                            self._detect_language(), PLAN_SKIP_TURN_OUTPUT["cn"]
                        ),
                        "result_type": "answer",
                    }
                )
                logger.info(
                    "[PlanApprovalInterruptRail] Plan approval skipped; "
                    "staying in plan mode and finishing this turn"
                )
        return decision

    # ── Helpers ──────────────────────────────────────────────────────

    def _detect_language(self) -> str:
        """Return ``"cn"`` or ``"en"`` based on the agent's configured language.

        Reads from ``system_prompt_builder.language`` on the agent. Falls back
        to ``"cn"`` if the agent reference is unavailable.
        """
        agent = self._agent
        if agent is not None:
            builder = getattr(agent, "system_prompt_builder", None)
            if builder is not None:
                lang = getattr(builder, "language", "cn")
                if lang in ("cn", "en"):
                    return lang
        return "cn"

    def _read_plan_content(self, ctx: AgentCallbackContext) -> str:
        """Read the plan file content for display in the approval dialog.

        Args:
            ctx: Agent callback context with session reference.

        Returns:
            Plan file text, or empty string if no plan file exists.
        """
        agent = self._agent
        if agent is None:
            return ""

        session = ctx.session
        if session is None:
            return ""
        plan_path = agent.get_plan_file_path(session)
        if not plan_path or not plan_path.exists():
            return ""

        try:
            return plan_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning(
                "[PlanApprovalInterruptRail] Failed to read plan file %s: %s",
                plan_path, exc,
            )
            return ""

    def _build_approval_message(self, plan_content: str, language: str) -> str:
        """Build the interrupt message with plan preview.

        Args:
            plan_content: The full plan file text (may be empty).
            language: ``"cn"`` or ``"en"``.

        Returns:
            Formatted markdown message for the approval dialog.
        """
        if language == "en":
            template = (
                _PLAN_APPROVAL_MESSAGE_EN
                if plan_content
                else _PLAN_EMPTY_MESSAGE_EN
            )
        else:
            template = (
                _PLAN_APPROVAL_MESSAGE_CN
                if plan_content
                else _PLAN_EMPTY_MESSAGE_CN
            )

        preview = plan_content
        if len(preview) > _MAX_PLAN_PREVIEW_CHARS:
            preview = preview[:_MAX_PLAN_PREVIEW_CHARS] + "\n\n… (truncated)"

        return template.format(plan_preview=preview)


__all__ = [
    "PlanApprovalInterruptRail",
    "build_plan_approval_options_from_message",
    "extract_plan_approval_content",
    "is_plan_approval_message",
    "strip_inline_plan_approval_choices",
]
