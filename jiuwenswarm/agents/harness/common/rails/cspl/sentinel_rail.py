# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CsplSentinelRail - CSPL tool input/output security scanning.

Ported from xy_channel sentinel_hook.ts; output REJECT uses request_force_finish
instead of steer-context.ts injection.
"""

from __future__ import annotations

import uuid

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.cspl.client import CsplConfig, scan
from jiuwenswarm.agents.harness.common.rails.cspl.constants import (
    ABORT_FALLBACK_MESSAGE,
    ABORT_MESSAGE,
    TOOL_INPUT_FALLBACK_REJECT_TEMPLATE,
    TOOL_INPUT_REJECT_TEMPLATE,
    TOOL_INPUT_SCAN,
    TOOL_OUTPUT_SCAN,
)
from jiuwenswarm.agents.harness.common.rails.cspl.scanners import (
    build_tool_input_payload,
    build_tool_output_payload,
)
from jiuwenswarm.agents.harness.common.rails.security_lists import audit
from jiuwenswarm.agents.harness.common.rails.security_lists.fallback import (
    is_strict_profile,
    p2_fail_closed_active,
)
from jiuwenswarm.agents.harness.common.rails.security_lists.notify import (
    report_security_event,
)
from jiuwenswarm.agents.harness.common.rails.security_lists.risk_classify import (
    RISK_LEVEL_KEY,
    RISK_P2,
    classify_risk,
)
from jiuwenswarm.common.utils import logger

_SESSION_ID_KEY = "cspl_session_id"

_HOT_POLICIES = frozenset({"auto", "p2_only", "strict"})


def _hot_fallback_policy() -> str:
    """``security.cspl.fallback_policy`` 热读（新键优先于静态 cspl 段；非法值忽略）。"""
    try:
        from jiuwenswarm.common.config import get_config

        cfg = get_config() or {}
        raw = (cfg.get("security") or {}).get("cspl") or {}
        policy = str(raw.get("fallback_policy") or "").strip() if isinstance(raw, dict) else ""
        return policy if policy in _HOT_POLICIES else ""
    except Exception:  # noqa: BLE001
        return ""


class CsplSentinelRail(DeepAgentRail):
    """CSPL Sentinel — scan tool input before execution and tool output after."""

    priority: int = 78

    def __init__(self, config: CsplConfig | None = None) -> None:
        super().__init__()
        self._config = config or CsplConfig.load()

    @staticmethod
    def _resolve_session_id(ctx: AgentCallbackContext) -> str:
        existing = ctx.extra.get(_SESSION_ID_KEY)
        if isinstance(existing, str) and existing:
            return existing

        for attr in ("session_id", "conversation_id"):
            value = getattr(ctx, attr, None)
            if isinstance(value, str) and value:
                sid = value.replace("-", "")
                ctx.extra[_SESSION_ID_KEY] = sid
                return sid

        inputs = ctx.inputs
        for attr in ("conversation_id", "session_id"):
            value = getattr(inputs, attr, None)
            if isinstance(value, str) and value:
                sid = value.replace("-", "")
                ctx.extra[_SESSION_ID_KEY] = sid
                return sid

        generated = uuid.uuid4().hex
        ctx.extra[_SESSION_ID_KEY] = generated
        return generated

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not self._config.enabled or not self._config.scan_tool_input:
            return

        tool_name = ctx.inputs.tool_name or ""
        if not tool_name:
            return

        payload = build_tool_input_payload(tool_name, ctx.inputs.tool_args)
        if not payload:
            logger.debug(
                "[CsplSentinelRail] TOOL_INPUT skip tool=%s (no scannable payload, args=%r)",
                tool_name,
                ctx.inputs.tool_args,
            )
            return

        session_id = self._resolve_session_id(ctx)
        logger.info(
            "[CsplSentinelRail] TOOL_INPUT scan start tool=%s session=%s",
            tool_name,
            session_id,
        )
        try:
            result = await scan(
                payload, TOOL_INPUT_SCAN, session_id, self._config, raise_on_error=True
            )
        except Exception as exc:
            logger.warning(
                "[CsplSentinelRail] TOOL_INPUT scan error tool=%s: %s", tool_name, exc
            )
            self._handle_scan_unavailable(ctx, tool_name, exc, scan_kind="tool_input")
            return

        logger.info(
            "[CsplSentinelRail] TOOL_INPUT scan done tool=%s result=%s",
            tool_name,
            result,
        )
        if result == "REJECT":
            message = TOOL_INPUT_REJECT_TEMPLATE.format(tool_name=tool_name)
            logger.warning("[CsplSentinelRail] TOOL_INPUT REJECT, blocking tool=%s", tool_name)
            self._reject_tool(ctx, message)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not self._config.enabled or not self._config.scan_tool_output:
            return

        tool_name = ctx.inputs.tool_name or ""
        if not tool_name:
            return

        payload = build_tool_output_payload(tool_name, ctx.inputs.tool_result)
        if not payload:
            return

        session_id = self._resolve_session_id(ctx)
        try:
            result = await scan(
                payload, TOOL_OUTPUT_SCAN, session_id, self._config, raise_on_error=True
            )
        except Exception as exc:
            logger.warning(
                "[CsplSentinelRail] TOOL_OUTPUT scan error tool=%s: %s", tool_name, exc
            )
            self._handle_scan_unavailable(ctx, tool_name, exc, scan_kind="tool_output")
            return

        if result == "REJECT":
            logger.warning(
                "[CsplSentinelRail] TOOL_OUTPUT REJECT, force finishing tool=%s", tool_name
            )
            ctx.request_force_finish({"output": ABORT_MESSAGE, "result_type": "answer"})

    def _effective_policy(self, strict_mode: bool) -> str:
        """兜底策略解析：strict 档强制 strict（设计 5.4）；auto → p2_only。"""
        if strict_mode:
            return "strict"
        policy = _hot_fallback_policy() or self._config.fallback_policy
        return "p2_only" if policy == "auto" else policy

    def _handle_scan_unavailable(
        self,
        ctx: AgentCallbackContext,
        tool_name: str,
        exc: Exception,
        *,
        scan_kind: str,
    ) -> None:
        """CSPL 不可用分级兜底（spec 7.1/7.3，设计 5.4）。

        strict 档/策略 strict → 全拒；P2（疑似凭证外传）且总开关生效 → 拒绝；
        其余 → 放行 + 审计 + 桌面提示；因总开关关闭而放行的 P2 打
        ``p2_fail_open_disabled`` 标记。
        """
        level = ctx.extra.get(RISK_LEVEL_KEY)
        if not level:
            level = classify_risk(tool_name, ctx.inputs.tool_args)
            ctx.extra[RISK_LEVEL_KEY] = level
        strict_mode = is_strict_profile()
        policy = self._effective_policy(strict_mode)
        closed = policy == "strict" or (level == RISK_P2 and p2_fail_closed_active())
        p2_fail_open = level == RISK_P2 and not closed
        extra: dict = {"p2_fail_open_disabled": True} if p2_fail_open else {}
        audit.log_event(
            audit.AUDIT_FALLBACK,
            domain="cspl",
            scan=scan_kind,
            risk_level=level,
            action_taken="reject" if closed else "allow",
            tool=tool_name,
            policy=policy,
            reason=f"{type(exc).__name__}: {exc}",
            **extra,
        )
        if closed:
            logger.warning(
                "[CsplSentinelRail] CSPL 不可用，%s 兜底拒绝 tool=%s level=%s",
                policy,
                tool_name,
                level,
            )
            if scan_kind == "tool_input":
                message = TOOL_INPUT_FALLBACK_REJECT_TEMPLATE.format(tool_name=tool_name)
                self._reject_tool(ctx, message)
            else:
                ctx.request_force_finish(
                    {"output": ABORT_FALLBACK_MESSAGE, "result_type": "answer"}
                )
            return
        logger.warning(
            "[CsplSentinelRail] CSPL 不可用，%s 兜底放行 tool=%s level=%s",
            policy,
            tool_name,
            level,
        )
        report_security_event(
            ctx,
            stage="security.degraded",
            detail=f"云端安全检测不可用，{level} 风险操作已放行并记录审计: {tool_name}",
        )

    @staticmethod
    def _reject_tool(ctx: AgentCallbackContext, message: str) -> None:
        tool_call = ctx.inputs.tool_call
        tool_call_id = tool_call.id if tool_call else ""
        ctx.extra["_skip_tool"] = True
        ctx.inputs.tool_result = message
        ctx.inputs.tool_msg = ToolMessage(content=message, tool_call_id=tool_call_id)


__all__ = ["CsplSentinelRail"]
