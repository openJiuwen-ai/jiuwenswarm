# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CodeAgentModeRail — plan-mode write enforcement for code mode.

Plan approval is handled by ``PlanApprovalInterruptRail`` with an
immediate dialog (aligned with Claude Code).  This rail handles:
- Blocking ``switch_mode`` from exiting plan mode
- Blocking non-git write operations via bash in plan mode
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from openjiuwen.core.common.logging import logger
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.agent_mode_rail import AgentModeRail

from jiuwenswarm.agents.harness.code.prompt.plan_approval import PLAN_EXECUTE_CTX_KEY

if TYPE_CHECKING:
    from openjiuwen.harness.deep_agent import DeepAgent

_NON_GIT_WRITE_RE = re.compile(
    r"\b(mkdir|touch|mv|cp|chmod|chown|dd|tee|wget|curl\s+.*\s*-[a-zA-Z]*O)\b"
    r"|\brm\s+(-[a-zA-Z]*[rf]|/|[~.])"
    r"|\b(7z|tar|zip|unzip|gzip|gunzip)\s+"
    r"|>\s*\S"
    r"|>>"
)

_EXIT_PLAN_NOTIFICATION_OPENING = "<system-reminder>"

# openjiuwen 改了 exit_plan_mode 的返回文案时的兜底值。只在下面那两个常量导不
# 进来时才用得上，所以允许它们过时——真正的判据始终优先取上游当前的文案。
_FALLBACK_EXIT_PLAN_RESULT_OPENINGS = ("Plan mode ended", "规划模式已结束")
_FALLBACK_EXIT_PLAN_BODY_HEADINGS = ("## Plan:", "## 计划：")


def _leading_literal(template: str) -> str:
    """取出模板里第一个占位符/换行之前的固定前缀。"""
    return template.split("{", 1)[0].split("\n", 1)[0].strip()


def _trailing_line(template: str) -> str:
    """取出模板最后一个非空行（``ExitPlanModeTool`` 的计划正文标题）。"""
    return template.rstrip().rsplit("\n", 1)[-1].strip()


def _derive_exit_plan_markers() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """从 openjiuwen 的 exit_plan_mode 文案模板推导两组判据。

    这两组字符串用来区分"工具真的跑完了"和"``before_tool_call`` 把它拦下来等审批"
    ——后者工具体还没执行，什么都不能拆。以前是把上游文案抄一份在这里，上游改措辞
    我们就会**静默**失效（task_tool 不注销、空计划不恢复模式、Web「执行」不结束本轮，
    最后一条还会导致本轮与前端补发的那一轮并发）。改成从上游常量推导：措辞变了自动
    跟随，常量被改名或删除则记一条 warning 并退回兜底值。

    Returns:
        ``(结果开头, 计划正文标题)`` 两个元组，各含中英两种。
    """
    try:
        from openjiuwen.harness.tools import agent_mode_tools
    except ImportError as exc:  # pragma: no cover - 上游包结构变动
        logger.warning(
            "[CodeAgentModeRail] cannot import openjiuwen agent_mode_tools (%s); "
            "falling back to bundled exit_plan_mode markers",
            exc,
        )
        return _FALLBACK_EXIT_PLAN_RESULT_OPENINGS, _FALLBACK_EXIT_PLAN_BODY_HEADINGS

    empty_msgs = getattr(agent_mode_tools, "_EXIT_PLAN_EMPTY_MSG", None)
    with_content_msgs = getattr(agent_mode_tools, "_EXIT_PLAN_WITH_CONTENT_PREFIX", None)
    if not isinstance(empty_msgs, dict) or not isinstance(with_content_msgs, dict):
        logger.warning(
            "[CodeAgentModeRail] openjiuwen exit_plan_mode message templates are "
            "missing; falling back to bundled markers"
        )
        return _FALLBACK_EXIT_PLAN_RESULT_OPENINGS, _FALLBACK_EXIT_PLAN_BODY_HEADINGS

    openings: list[str] = []
    for template in (*empty_msgs.values(), *with_content_msgs.values()):
        opening = _leading_literal(template) if isinstance(template, str) else ""
        if opening and opening not in openings:
            openings.append(opening)
    headings: list[str] = []
    for template in with_content_msgs.values():
        heading = _trailing_line(template) if isinstance(template, str) else ""
        if heading and heading not in headings:
            headings.append(heading)
    if not openings or not headings:
        logger.warning(
            "[CodeAgentModeRail] could not derive exit_plan_mode markers from "
            "openjiuwen templates; falling back to bundled markers"
        )
        return _FALLBACK_EXIT_PLAN_RESULT_OPENINGS, _FALLBACK_EXIT_PLAN_BODY_HEADINGS
    return tuple(openings), tuple(headings)


_EXIT_PLAN_RESULT_OPENINGS, _EXIT_PLAN_BODY_HEADINGS = _derive_exit_plan_markers()


class CodeAgentModeRail(AgentModeRail):
    """AgentModeRail variant for jiuwenswarm code mode.

    Plan approval is handled by ``PlanApprovalInterruptRail`` which intercepts
    ``exit_plan_mode`` with an immediate approval dialog (aligned with Claude Code).
    Mode restoration happens inside ``ExitPlanModeTool.invoke()`` on approval.
    """

    def init(self, agent: "DeepAgent") -> None:
        """Register tools. No exit_plan_mode patching needed —
        ``PlanApprovalInterruptRail`` handles the approval gate.
        """
        super().init(agent)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Enforce plan-mode write blocks beyond the parent git-only guard."""
        agent = self._agent
        session = ctx.session
        plan_state = agent.load_state(session).plan_mode
        tool_name = ctx.inputs.tool_name

        if plan_state.mode == "plan" and tool_name == "switch_mode":
            target = self._parse_switch_mode_target(ctx)
            if target in {"normal", "auto"}:
                if self._language_is_cn():
                    msg = (
                        "[AgentModeRail] plan 模式下不能用 switch_mode 退出。"
                        "请先调用 exit_plan_mode 提交计划等待审批。"
                    )
                else:
                    msg = (
                        "[AgentModeRail] switch_mode cannot exit plan mode. "
                        "Call exit_plan_mode to submit your plan for approval."
                    )
                self._reject_tool(ctx, msg)
                return

        await super().before_tool_call(ctx)
        if ctx.extra.get("_skip_tool"):
            return

        if plan_state.mode != "plan":
            return
        if tool_name == "bash":
            command = self._extract_bash_command(ctx)
            if _NON_GIT_WRITE_RE.search(command):
                if self._language_is_cn():
                    msg = f"[AgentModeRail] plan 模式下禁止写操作（{command!r}）。"
                else:
                    msg = (
                        f"[AgentModeRail] Write operations are blocked in plan mode "
                        f"({command!r})."
                    )
                self._reject_tool(ctx, msg)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Override parent to fix mode restoration on user rejection.

        The parent ``AgentModeRail.after_tool_call()`` has a supplement
        mode-restoration block that calls ``restore_mode_after_plan_exit()``
        when ``tool_result is not None``.  However, when
        ``PlanApprovalInterruptRail`` rejects the call (user clicks Reject),
        ``_skip_tool()`` sets ``tool_result`` to the feedback string — so the
        ``is not None`` check passes and the mode is erroneously restored.

        **Important**: we check ``_plan_rejected`` instead of ``_skip_tool``
        because ``ability_manager._railed_execute_single_tool_call`` **pops**
        ``_skip_tool`` from ``ctx.extra`` before ``after_tool_call`` runs.
        ``PlanApprovalInterruptRail`` sets ``_plan_rejected`` which persists
        through the pop.
        """
        tool_name = ctx.inputs.tool_name
        agent = self._agent
        rejected = ctx.extra.get("_plan_rejected", False)
        # This hook also fires on the pass where ``PlanApprovalInterruptRail``
        # suspended the call for approval — the tool body has not run yet, so
        # tearing plan mode down here would drop the user out of plan mode
        # before they even answered (and "skip" is supposed to keep them in).
        executed = self._exit_plan_tool_executed(ctx)

        # Segment 1: register / unregister task_tool (same as parent)
        if tool_name == "enter_plan_mode" and not rejected:
            self._register_task_tool(agent)
        elif tool_name == "exit_plan_mode" and not rejected and executed:
            self._unregister_task_tool(agent)

        if tool_name != "exit_plan_mode" or rejected or not executed:
            return

        # Segment 2: supplement mode restoration (PARENT BUG FIXED)
        # Only restore when the tool was NOT rejected — i.e. it actually
        # executed but the plan was empty (ExitPlanModeTool.invoke() returns
        # early without calling restore_mode_after_plan_exit).
        session = ctx.session
        state = agent.load_state(session)
        if state.plan_mode.mode == "plan":
            try:
                agent.restore_mode_after_plan_exit(session)
                logger.info(
                    "[CodeAgentModeRail] Restored mode after plan exit "
                    "(plan was empty, tool did not restore)"
                )
            except Exception as exc:
                logger.warning(
                    "[CodeAgentModeRail] Failed to restore mode: %s", exc
                )

        # Segment 3: drop the plan body the tool echoes back.
        self._compact_exit_plan_result(ctx)

        # Segment 4: Web splits execution off into its own turn. Plan mode is
        # now torn down, so stop here — the frontend follows up with a plain
        # non-plan message that runs the plan as a fresh user turn. TUI never
        # sets this marker and keeps implementing within the same turn.
        if ctx.extra.get(PLAN_EXECUTE_CTX_KEY):
            ctx.request_force_finish({"output": "", "result_type": "answer"})
            logger.info(
                "[CodeAgentModeRail] Plan approved for deferred execution; "
                "exited plan mode and finished this turn"
            )

    @staticmethod
    def _exit_plan_tool_executed(ctx: AgentCallbackContext) -> bool:
        """True when ``ExitPlanModeTool.invoke()`` actually produced a result."""
        result = ctx.inputs.tool_result
        if not isinstance(result, str):
            return False
        head = result.lstrip()
        return any(head.startswith(opening) for opening in _EXIT_PLAN_RESULT_OPENINGS)

    @staticmethod
    def _compact_exit_plan_result(ctx: AgentCallbackContext) -> None:
        """Strip the echoed plan body out of the ``exit_plan_mode`` result.

        ``ExitPlanModeTool`` returns the whole plan file (easily 10k+ chars)
        followed by the short "start executing now" reminder. The model already
        has the plan in context from writing it, and the user has just approved
        it in the UI, so re-reading it here only buries the reminder — models
        respond by summarising the plan again and asking whether to start
        instead of executing. Keep the header and the reminder, drop the body.
        """
        result = ctx.inputs.tool_result
        if not isinstance(result, str):
            return
        body_at = -1
        for heading in _EXIT_PLAN_BODY_HEADINGS:
            found = result.find(heading)
            if found == -1:
                continue
            if body_at == -1 or found < body_at:
                body_at = found
        if body_at == -1:
            return
        notification_at = result.rfind(_EXIT_PLAN_NOTIFICATION_OPENING)
        head = result[:body_at].rstrip()
        tail = result[notification_at:] if notification_at > body_at else ""
        compacted = f"{head}\n\n{tail}" if tail else head

        ctx.inputs.tool_result = compacted
        tool_msg = ctx.inputs.tool_msg
        if tool_msg is not None and hasattr(tool_msg, "content"):
            tool_msg.content = compacted
        logger.info(
            "[CodeAgentModeRail] Compacted exit_plan_mode result: %d -> %d chars",
            len(result),
            len(compacted),
        )

    @staticmethod
    def _parse_switch_mode_target(ctx: AgentCallbackContext) -> str:
        """Parse the target mode from a switch_mode tool-call context."""
        raw: Any = None
        tool_call = getattr(ctx.inputs, "tool_call", None)
        if tool_call is not None:
            raw = getattr(tool_call, "arguments", None)
        if raw is None:
            raw = getattr(ctx.inputs, "tool_args", None)
        args: dict[str, Any] = {}
        if isinstance(raw, dict):
            args = raw
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    args = parsed
            except (TypeError, ValueError):
                pass
        return str(args.get("mode") or args.get("target_mode") or "").strip()
