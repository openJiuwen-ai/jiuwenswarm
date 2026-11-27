# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""PermissionInterruptRail - tool permission guardrail using ConfirmInterruptRail.

Implements permission checks via PermissionEngine and triggers HITL interrupts
for ASK decisions using the built-in interrupt rail flow.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.interrupt.state import INTERRUPT_AUTO_CONFIRM_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.interrupt.confirm_rail import (
    ConfirmInterruptRail,
    ConfirmPayload,
)

from jiuwenclaw.agentserver.permissions.core import PermissionEngine, get_permission_engine
from jiuwenclaw.agentserver.permissions.file_guard import persist_file_operations_allow
from jiuwenclaw.agentserver.permissions.models import FileOperation
from jiuwenclaw.agentserver.permissions.patterns import persist_permission_allow_rule
from jiuwenclaw.agentserver.permissions.shell_ast import parse_shell_for_permission
from jiuwenclaw.agentserver.permissions.suggestions import build_permission_suggestions
from jiuwenclaw.config import get_config
from jiuwenclaw.agentserver.permissions.checker import (
    TOOL_PERMISSION_CHANNEL_ID,
    ToolPermissionLog,
    assess_command_risk_static,
    assess_shell_targets_risk_static,
)
from jiuwenclaw.agentserver.permissions.shell_tools import (
    SHELL_PERMISSION_TOOLS,
    extract_shell_command,
    is_shell_permission_tool,
)
from jiuwenclaw.agentserver.permissions import PermissionLevel, PermissionResult
from jiuwenclaw.e2a.acp_tool_updates import build_acp_tool_descriptor
from jiuwenclaw.utils import logger


TOOL_NAME_ALIASES = {
    "free_search": "mcp_free_search",
    "paid_search": "mcp_paid_search",
    "fetch_webpage": "mcp_fetch_webpage",
    "exec_command": "mcp_exec_command",
}

INTERRUPT_PENDING_PERMISSION_CONTEXT_KEY = "jiuwenclaw_pending_permission_contexts"
_SHELL_PERMISSION_TOOLS = SHELL_PERMISSION_TOOLS


@dataclass(frozen=True)
class PermissionConfirmResponse:
    approved: bool
    feedback: str = ""
    auto_confirm: bool = False
    persist_allow: bool = False


@dataclass(frozen=True)
class PersistPreview:
    targets: list[str]
    whole_tool: bool = False
    disabled_reason: str = ""
    ask_subcommands: list[str] | None = None
    file_operations: list[FileOperation] | None = None


class PermissionInterruptRail(ConfirmInterruptRail):
    """Permission interrupt rail.

    - ALLOW: continue
    - DENY: reject
    - ASK: interrupt with ConfirmPayload schema

    Auto-confirm is stored in session state (INTERRUPT_AUTO_CONFIRM_KEY).
    Supports fine-grained auto-confirm keys for bash commands (e.g., bash_dir, bash_rm).
    """

    priority: int = 90

    def __init__(
        self,
        config: Optional[dict] = None,
        engine: Optional[PermissionEngine] = None,
        tool_names: Optional[Iterable[str]] = None,
        llm: Any = None,
        model_name: str | None = None,
    ) -> None:
        # This rail overrides before_tool_call and intentionally evaluates every
        # tool call. Parent tool_names are therefore diagnostic only.
        super().__init__(tool_names=tool_names or [])
        self._static_config = config or {}
        if engine is not None:
            self._engine = engine
            # 复用外部引擎时也要把 LLM 句柄打进去，否则 L3-Cmd 永远拿不到 llm
            # （根因：``init_permission_engine(...)`` 首启时通常没有 LLM，
            # 而 ``build_permission_rail(... llm=...)`` 又把 engine 直接传了进来。）
            if llm is not None or model_name is not None:
                effective_llm = llm if llm is not None else self._engine.llm
                effective_model = (
                    model_name if model_name is not None else self._engine.model_name
                )
                self._engine.update_llm(effective_llm, effective_model)
        else:
            self._engine = PermissionEngine(
                config=self._static_config,
                llm=llm,
                model_name=model_name,
            )
        logger.info(
            "[PermissionEngine] permission.rail.init tool_count=%d configured_tool_count=%d llm_enabled=%s model_name=%s",
            len(self._tool_names),
            len(self._static_config.get("tools") or {}),
            self._engine.llm is not None,
            self._engine.model_name,
        )

    @property
    def engine(self) -> PermissionEngine:
        """只读暴露底层 ``PermissionEngine``（单测 / 诊断用，避免直接读 ``_engine``）。"""
        return self._engine

    def init(self, agent: Any) -> None:
        super().init(agent)
        callbacks = self.get_callbacks()
        before_cb = callbacks.get("before_tool_call")
        if before_cb is None:
            from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
            before_cb = callbacks.get(AgentCallbackEvent.BEFORE_TOOL_CALL)
        logger.info(
            "[PermissionEngine] permission.rail.init.callbacks rail_class=%s "
            "bound_before=%s bound_before_qualname=%s get_callbacks_before=%s get_callbacks_before_qualname=%s",
            self.__class__.__name__,
            type(self.before_tool_call).__name__,
            getattr(self.before_tool_call, "__qualname__", None),
            type(before_cb).__name__ if before_cb is not None else None,
            getattr(before_cb, "__qualname__", None) if before_cb is not None else None,
        )

    def _normalize_tool_name(self, tool_name: str) -> str:
        """Normalize tool name using aliases.

        Maps tool names from openjiuwen.harness.tools to mcp_* names used in config.
        """
        return TOOL_NAME_ALIASES.get(tool_name, tool_name)

    def _get_auto_confirm_key(self, tool_call: ToolCall) -> str:
        """Generate a conservative session auto-confirm key for the tool call."""
        if tool_call is None:
            return ""

        tool_name = tool_call.name or ""
        tool_args = self._parse_tool_args(tool_call)

        if is_shell_permission_tool(tool_name):
            return self._build_shell_auto_confirm_key(tool_name, extract_shell_command(tool_args))

        return tool_name

    @staticmethod
    def _build_shell_auto_confirm_key(tool_name: str, command: str) -> str:
        text = (command or "").strip()
        if not text:
            return ""

        shell_ast_result = parse_shell_for_permission(text)
        if shell_ast_result.kind != "simple":
            return ""
        if shell_ast_result.flags.has_risky_structure():
            return ""
        if len(shell_ast_result.subcommands) != 1:
            return ""

        subcommand = (shell_ast_result.subcommands[0].text or "").strip()
        if not subcommand:
            return ""
        return f"{tool_name}:{subcommand}"

    @staticmethod
    def _should_store_auto_confirm(
        *,
        auto_confirm: bool,
        session: Any,
        auto_confirm_key: str,
        persisted: bool,
        persist_requested: bool = False,
    ) -> bool:
        if not auto_confirm or session is None or not auto_confirm_key:
            return False
        if persisted:
            logger.info(
                "[PermissionEngine] permission.auto_confirm.skip key=%s reason=persisted_rule",
                auto_confirm_key,
            )
            return False
        if persist_requested:
            logger.info(
                "[PermissionEngine] permission.auto_confirm.skip key=%s reason=persist_failed",
                auto_confirm_key,
            )
            return False
        return True

    @staticmethod
    def _read_session_state(session: Any, key: str) -> Any:
        if session is None:
            return None
        try:
            return session.get_state(key)
        except Exception:
            logger.debug(
                "[PermissionEngine] permission.rail.session_state_read_failed key=%s",
                key,
                exc_info=True,
            )
            return None

    @classmethod
    def _get_pending_permission_contexts(cls, session: Any) -> dict[str, dict[str, Any]]:
        data = cls._read_session_state(session, INTERRUPT_PENDING_PERMISSION_CONTEXT_KEY)
        return data if isinstance(data, dict) else {}

    @classmethod
    def _store_pending_permission_context(
        cls,
        ctx: AgentCallbackContext,
        tool_call_id: str,
        context: dict[str, Any],
    ) -> None:
        session = getattr(ctx, "session", None)
        if session is None or not tool_call_id:
            return
        pending = dict(cls._get_pending_permission_contexts(session))
        pending[tool_call_id] = context
        session.update_state({INTERRUPT_PENDING_PERMISSION_CONTEXT_KEY: pending})

    @classmethod
    def _pop_pending_permission_context(
        cls,
        ctx: AgentCallbackContext,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        session = getattr(ctx, "session", None)
        if session is None or not tool_call_id:
            return None
        pending = dict(cls._get_pending_permission_contexts(session))
        payload = pending.pop(tool_call_id, None)
        session.update_state({INTERRUPT_PENDING_PERMISSION_CONTEXT_KEY: pending})
        if not isinstance(payload, dict):
            return None
        if payload.get("tool_name") != tool_name:
            return None
        stored_args = payload.get("tool_args")
        if isinstance(stored_args, dict) and stored_args != tool_args:
            return None
        return payload

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        tool_name = ctx.inputs.tool_name
        tool_call = ctx.inputs.tool_call
        normalized_name = self._normalize_tool_name(tool_name)
        logger.info(
            "[PermissionEngine] permission.rail.before_tool_call tool=%s normalized=%s tracked_tools=%s",
            tool_name, normalized_name, list(self._tool_names)
        )

        tool_call_id = self._resolve_tool_call_id(tool_call)
        user_input = self._get_user_input(ctx, tool_call_id)
        auto_confirm_config = None
        if ctx.session:
            auto_confirm_config = ctx.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY)
            if not isinstance(auto_confirm_config, dict):
                auto_confirm_config = {}

        decision = await self.resolve_interrupt(
            ctx=ctx,
            tool_call=tool_call,
            user_input=user_input,
            auto_confirm_config=auto_confirm_config,
        )
        ctx.extra["_interrupt_decision"] = decision
        self._apply_decision(ctx, tool_call, tool_name, decision)

    def update_config(
        self,
        config: dict,
        tool_names: Optional[Iterable[str]] = None,
        *,
        llm: Any = None,
        model_name: str | None = None,
    ) -> None:
        """Hot-update static permission config.

        Config updates must not shrink the rail interception surface. This rail
        checks every tool call regardless of whether the tool appears in config.

        ``llm`` / ``model_name`` 可选；若任一非 ``None``，则同步刷新底层引擎的
        LLM 句柄，避免 L3-Cmd 拿不到模型而静默退化为空。
        """
        self._static_config = config
        self._engine.update_config(config)
        if llm is not None or model_name is not None:
            effective_llm = llm if llm is not None else self._engine.llm
            effective_model = (
                model_name if model_name is not None else self._engine.model_name
            )
            self._engine.update_llm(effective_llm, effective_model)
        if tool_names is not None:
            self._tool_names.update(str(x).strip() for x in tool_names if str(x).strip())
        logger.info(
            "[PermissionEngine] permission.rail.config_updated intercept=all diagnostic_tool_count=%d "
            "llm_enabled=%s model_name=%s",
            len(self._tool_names),
            self._engine.llm is not None,
            self._engine.model_name,
        )

    def _build_pending_permission_context(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        result: PermissionResult,
    ) -> dict[str, Any]:
        preview = self._build_persist_preview(tool_name, tool_args, result)
        context: dict[str, Any] = {
            "tool_name": tool_name,
            "tool_args": dict(tool_args),
            "permission": result.permission.value,
            "matched_rule": result.matched_rule,
            "reason": result.reason,
            "would_persist_patterns": list(preview.targets) if not preview.whole_tool else [],
            "would_persist_whole_tool": preview.whole_tool,
        }
        if preview.ask_subcommands:
            context["ask_subcommands"] = preview.ask_subcommands
        if preview.disabled_reason:
            context["persist_disabled_reason"] = preview.disabled_reason
        if result.file_operations:
            context["file_operations"] = [
                {
                    "action": op.action,
                    "path": op.path,
                    "source": op.source,
                    "prompt": op.prompt,
                }
                for op in result.file_operations
            ]
        return context

    def _build_persist_preview(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        result: PermissionResult | None = None,
    ) -> PersistPreview:
        file_operations = list(result.file_operations or []) if result is not None else []
        if is_shell_permission_tool(tool_name):
            ask_subcommands = self._extract_ask_subcommands(result) if result is not None else []
            targets = self._build_would_persist_patterns(
                tool_name,
                tool_args,
                ask_subcommands=ask_subcommands,
            )
            return PersistPreview(
                targets=targets,
                whole_tool=False,
                disabled_reason=(
                    "" if file_operations else self._build_no_persist_target_reason(tool_name, tool_args, targets)
                ),
                ask_subcommands=ask_subcommands,
                file_operations=file_operations,
            )
        targets = [tool_name] if tool_name else []
        return PersistPreview(
            targets=targets,
            whole_tool=bool(tool_name),
            disabled_reason="" if targets else "工具名为空，无法生成持久化规则",
            file_operations=file_operations,
        )

    @staticmethod
    def _restore_file_operations(pending_context: dict[str, Any] | None) -> list[FileOperation]:
        if not isinstance(pending_context, dict):
            return []
        raw = pending_context.get("file_operations")
        if not isinstance(raw, list):
            return []
        ops: list[FileOperation] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip().lower()
            path = str(item.get("path") or "").strip()
            if action not in ("read", "write", "exec") or not path:
                continue
            source = str(item.get("source") or "tool_arg").strip().lower()
            if source not in ("tool_arg", "shlex", "script_scan", "llm"):
                source = "tool_arg"
            prompt = str(item.get("prompt") or "")
            ops.append(FileOperation(
                action=action,  # type: ignore[arg-type]
                path=path,
                source=source,  # type: ignore[arg-type]
                prompt=prompt,
            ))
        return ops

    def _build_would_persist_patterns(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        ask_subcommands: list[str] | None = None,
    ) -> list[str]:
        command = extract_shell_command(tool_args)
        if not command:
            return []
        suggestions = build_permission_suggestions(
            tool_name,
            tool_args,
            shell_ast_result=parse_shell_for_permission(command),
            ask_subcommands=ask_subcommands,
            existing_patterns=self._existing_allow_patterns(),
        )
        return [item.pattern for item in suggestions]

    def _build_no_persist_target_reason(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        persist_targets: list[str],
    ) -> str:
        if not is_shell_permission_tool(tool_name) or persist_targets:
            return ""
        command = extract_shell_command(tool_args)
        if not command:
            return "命令为空，无法生成持久化规则"
        return "没有可写入的新持久化规则"

    def _build_persist_allow_targets(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        result: PermissionResult | None = None,
    ) -> list[str]:
        return self._build_persist_preview(tool_name, tool_args, result).targets

    @staticmethod
    def _format_inline_code_items(items: list[str]) -> str:
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        return " ".join(f"`{item}`" for item in cleaned)

    @staticmethod
    def extract_file_guard_tail(raw: str) -> str | None:
        """从 ``core.py`` 拼出来的 raw matched_rule 里抠出 ``|file_guard:xxx`` 段。

        ``core.py`` 用 ``f"{baseline}|{fg_result.matched_rule}"`` 拼接，
        ``fg_result.matched_rule`` 形如 ``file_guard:ask`` / ``file_guard:deny``，
        本身不含 ``|``。**但 baseline 段可能含 ``|``**——典型情况是 shell tool
        的 ``tiered_policy:shell_subcommands:<long command>...``，命令字符串里
        合法的 PowerShell / bash 管道（``ls | grep x``）会让 ``split("|")``
        错切第一个 ``|``，导致 ``head`` / ``tail`` 全错位。这里改用 ``rfind``
        从右侧定位最后一个 ``|file_guard:`` 边界，避开 baseline 内部的伪 ``|``。
        """
        if not raw:
            return None
        sep_idx = raw.rfind("|file_guard:")
        if sep_idx < 0:
            return None
        return raw[sep_idx + 1:].strip() or None

    def _tools_cfg_allows_tool(self, tool_name: str) -> bool:
        """``permissions.tools.<name>`` 是否为字面 ``allow``（与 ``tiered_policy`` 命中段一致）。"""
        tools = self._static_config.get("tools")
        if not isinstance(tools, dict):
            return False
        key = self._normalize_tool_name(tool_name)
        raw = tools.get(key)
        return isinstance(raw, str) and raw.strip().lower() == "allow"

    def _non_shell_allow_plus_file_guard_ask_display(
        self, tool_name: str, result: PermissionResult, raw: str,
    ) -> bool:
        """非 shell、子线 A 为 allow（``tools.<name>``）、仅子线 B 抬到 ASK 的合成 matched_rule。"""
        if tool_name in _SHELL_PERMISSION_TOOLS or result.permission != PermissionLevel.ASK:
            return False
        if not raw or "|file_guard:" not in raw:
            return False
        if not self._tools_cfg_allows_tool(tool_name):
            return False
        key = self._normalize_tool_name(tool_name)
        first = raw.split("|", 1)[0].strip()
        return first == f"tools.{key}"

    def display_matched_rule(self, tool_name: str, result: PermissionResult) -> str:
        raw = str(result.matched_rule or "").strip()
        if result.permission != PermissionLevel.ASK:
            return raw or "N/A"
        if tool_name in _SHELL_PERMISSION_TOOLS:
            # shell tool 的 baseline matched_rule（``tiered_policy:shell_subcommands:<完整命令串>=>...``）
            # 又长又含管道符，前端展示价值不高，所以折叠成 ``{tool}.shell_command.ask``。
            # 但 file_guard 子线 B 的判定结果（``file_guard:ask`` / ``file_guard:deny``）
            # 仍然必须保留，否则审批卡上看不出本次决策是不是被 file_guard 抬高的。
            head = f"{tool_name}.shell_command.ask"
            fg_tail = PermissionInterruptRail.extract_file_guard_tail(raw)
            if fg_tail == "file_guard:ask" and not PermissionInterruptRail._has_shell_ask(result, raw):
                return fg_tail
            return f"{head}|{fg_tail}" if fg_tail else head
        # 非 shell：``read_file`` 配 ``allow`` 但路径越界时 core 拼 ``tools.read_file|file_guard:ask``。
        # 此时子线 A 并未要求 ASK，展示 ``read_file.ask|…`` 会误导用户——与 shell 侧「无子线 A ASK
        # 则只展示 file_guard」对称，这里整段折叠为 ``file_guard:ask``。
        fg_tail = PermissionInterruptRail.extract_file_guard_tail(raw)
        if fg_tail and self._non_shell_allow_plus_file_guard_ask_display(tool_name, result, raw):
            return fg_tail
        # 非 shell tool：baseline head（``tools.{tool}`` / ``defaults.ask`` / ``defaults.guard``）
        # 不含 ``|``，``split("|")`` 切第一段就是干净的 head。这里只对 head 做美化，
        # 后续段（``file_guard:ask`` 等）保持原样，最后再用 ``|`` 拼回，避免把 ``.ask`` 后缀
        # 错误地追加到整串末尾导致 ``write_file|file_guard:ask.ask`` 这种怪异显示。
        segments = [s.strip() for s in raw.split("|")] if raw else []
        head = segments[0] if segments else ""
        tail = [s for s in segments[1:] if s]

        if head in ("defaults.ask", "defaults.guard"):
            display_head = head
        elif head == f"tools.{tool_name}":
            display_head = f"{tool_name}.ask"
        elif head.startswith("tools."):
            configured_tool = head.removeprefix("tools.").strip()
            display_head = f"{configured_tool}.ask" if configured_tool else f"{tool_name}.ask"
        elif head:
            display_head = head
        else:
            display_head = f"{tool_name}.ask"

        return "|".join([display_head, *tail])

    @staticmethod
    def _has_shell_ask(result: PermissionResult, raw_matched_rule: str) -> bool:
        subcommand_results = result.subcommand_results or []
        for item in subcommand_results:
            if item.permission == PermissionLevel.ASK:
                return True
        if subcommand_results:
            return False
        baseline = raw_matched_rule
        fg_sep_idx = raw_matched_rule.rfind("|file_guard:")
        if fg_sep_idx >= 0:
            baseline = raw_matched_rule[:fg_sep_idx]
        return "fallback(no_allow_match)" in baseline or "shell_empty_command" in baseline

    @staticmethod
    def _risk_rank(risk: dict[str, Any]) -> int:
        return {"低": 0, "中": 1, "高": 2}.get(str(risk.get("level") or ""), 1)

    @staticmethod
    def _pending_file_guard_paths(result: PermissionResult) -> list[str]:
        """``file_guard`` 抬到 ASK 时附带的待确认路径（与 ``core`` 的 ``external_paths`` 同源）。"""
        ext = getattr(result, "external_paths", None) or []
        if ext:
            return [str(p).strip() for p in ext if str(p).strip()]
        ops = getattr(result, "file_operations", None) or []
        return [str(op.path).strip() for op in ops if str(getattr(op, "path", "") or "").strip()]

    @staticmethod
    def _risk_high_unauthorized_file_access() -> dict[str, str]:
        return {
            "level": "高",
            "icon": "🔴",
            "explanation": (
                "该操作涉及策略范围外或未授权的文件路径访问，安全风险较高，需用户确认后方可继续"
            ),
        }

    def build_risk_for_message(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        result: PermissionResult,
        preview: PersistPreview,
    ) -> dict[str, str]:
        """合成审批卡用的风险等级与说明（供消息体与结构化上下文复用）。"""
        if result.risk:
            return self._append_persistence_risk_note(result.risk, preview.disabled_reason)

        pending_paths = PermissionInterruptRail._pending_file_guard_paths(result)

        if not is_shell_permission_tool(tool_name):
            if pending_paths:
                risk = PermissionInterruptRail._risk_high_unauthorized_file_access()
            else:
                risk = {
                    "level": "低",
                    "icon": "🟢",
                    "explanation": (
                        "该工具调用不涉及需额外审批的文件路径访问，"
                        "当前仅因工具权限配置需要用户确认"
                    ),
                }
            return self._append_persistence_risk_note(risk, preview.disabled_reason)

        targets = preview.targets
        if targets:
            risk = assess_shell_targets_risk_static(targets)
        else:
            risk = assess_command_risk_static(tool_name, tool_args)

        if pending_paths:
            risk = PermissionInterruptRail._risk_high_unauthorized_file_access()
        elif preview.disabled_reason and self._risk_rank(risk) < 1:
            risk = {
                "level": "中",
                "icon": "🟡",
                "explanation": "当前命令结构复杂，需要用户确认后执行",
            }
        return self._append_persistence_risk_note(risk, preview.disabled_reason)

    @staticmethod
    def _append_persistence_risk_note(risk: dict[str, Any], persist_disabled_reason: str) -> dict[str, str]:
        if not persist_disabled_reason:
            return risk
        explanation = str(risk.get("explanation") or "").strip()
        note = f"当前命令{persist_disabled_reason}，无法为“总是允许”写入持久化规则"
        if note in explanation:
            return risk
        combined = f"{explanation}；{note}" if explanation else note
        return {
            "level": str(risk.get("level") or "中"),
            "icon": str(risk.get("icon") or "🟡"),
            "explanation": combined,
        }

    def _existing_allow_patterns(self) -> set[str]:
        patterns: set[str] = set()
        for section_name in ("rules", "approval_overrides"):
            raw = self._static_config.get(section_name)
            if not isinstance(raw, list):
                continue
            for item in raw:
                if not isinstance(item, dict):
                    continue
                if str(item.get("action") or "").strip().lower() != "allow":
                    continue
                pattern = item.get("pattern")
                if isinstance(pattern, str) and pattern:
                    patterns.add(pattern)
        return patterns

    @property
    def diagnostic_tool_names(self) -> set[str]:
        return set(self._tool_names)

    def build_pending_permission_context(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        result: PermissionResult,
    ) -> dict[str, Any]:
        return self._build_pending_permission_context(tool_name, tool_args, result)

    def build_permission_message(
        self,
        tool_call: Optional[ToolCall],
        result: PermissionResult,
    ) -> str:
        return self._build_message(tool_call, result)

    def build_acp_permission_request(
        self,
        tool_call: Optional[ToolCall],
        result: PermissionResult,
    ) -> dict[str, Any]:
        return self._build_acp_permission_request(tool_call, result)

    @staticmethod
    def _extract_ask_subcommands(result: PermissionResult) -> list[str]:
        """复用第一次 ``check_permission`` 已经算好的子命令权限结果。

        仅在 simple shell 多/单子命令场景下 ``result.subcommand_results`` 非空。
        这里只挑出 ``ASK`` 子命令；本身已被 rules 判为 ``allow`` 的子命令会被
        过滤掉，从而避免在用户「始终允许」时为它们再生成新的规则。
        """
        if not result or not result.subcommand_results:
            return []
        return [
            item.text
            for item in result.subcommand_results
            if item.permission == PermissionLevel.ASK and item.text
        ]

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[ToolCall],
        user_input: Optional[Any],
        auto_confirm_config: Optional[dict] = None,
    ):
        tool_name = tool_call.name if tool_call is not None else ""
        normalized_name = self._normalize_tool_name(tool_name)
        tool_args = self._parse_tool_args(tool_call)
        auto_confirm_key = self._get_auto_confirm_key(tool_call)
        tool_call_id = self._resolve_tool_call_id(tool_call)

        logger.info(
            "[PermissionEngine] permission.rail.resolve tool=%s normalized=%s "
            "tool_args=%s auto_confirm_key=%s user_input_type=%s",
            tool_name, normalized_name, tool_args, auto_confirm_key,
            type(user_input).__name__ if user_input else None
        )

        from jiuwenclaw.agentserver.deep_agent.permissions.owner_scopes import (
            TOOL_PERMISSION_CONTEXT,
            check_avatar_permission,
            _resolve_owner_scope_level,
        )
        perm_ctx = TOOL_PERMISSION_CONTEXT.get()

        if perm_ctx is not None:
            logger.info(
                "[PermissionEngine] permission.rail.context scene=%s channel_id=%s principal_user_id=%s",
                perm_ctx.scene, perm_ctx.channel_id, perm_ctx.principal_user_id
            )
            if perm_ctx.scene == "group_digital_avatar":
                if user_input is None:
                    level = await check_avatar_permission(
                        normalized_name, tool_args,
                        channel_id=self._resolve_channel_id(),
                        session_id=None,
                    )
                    if level == "allow":
                        return self.approve()
                    return self.reject(
                        tool_result="[PERMISSION_DENIED] 该工具未被授权在数字分身场景下使用"
                    )
                return self.reject(tool_result="[PERMISSION_DENIED] 数字分身场景不支持交互审批")

            if perm_ctx.principal_user_id:
                owner_scopes = self._static_config.get("owner_scopes", {})
                logger.info(
                    "[PermissionEngine] permission.rail.owner_scope_lookup "
                    "channel_id=%s user_id=%s owner_scope_channel_count=%d",
                    perm_ctx.channel_id,
                    perm_ctx.principal_user_id,
                    len(owner_scopes) if isinstance(owner_scopes, dict) else 0,
                )
                if isinstance(owner_scopes, dict) and owner_scopes:
                    cid = perm_ctx.channel_id.strip()
                    uid = perm_ctx.principal_user_id.strip()
                    scope_cfg = (owner_scopes.get(cid) or {}).get(uid)
                    owner_level = _resolve_owner_scope_level(scope_cfg, normalized_name, tool_args)
                    if owner_level is not None:
                        logger.info(
                            "[PermissionEngine] permission.rail.owner_scope_match tool=%s normalized=%s level=%s",
                            tool_name, normalized_name, owner_level
                        )
                        if owner_level == "allow":
                            return self.approve()
                        return self.reject(
                            tool_result=f"[PERMISSION_DENIED] 该工具未被授权 (owner_scopes: {owner_level})"
                        )

        if user_input is None:
            logger.info(
                "[PermissionEngine] permission.rail.first_check tool=%s normalized=%s",
                tool_name, normalized_name
            )
            # 与磁盘上的 permissions 对齐：persist_cli_trusted_directory 等只更新了全局
            # PermissionEngine；若此处仍用旧的 _static_config 覆盖引擎，会抹掉刚写入的
            # approval_overrides / external_directory。
            perm = get_config().get("permissions")
            if isinstance(perm, dict):
                self.update_config(perm)
            elif self._engine is get_permission_engine():
                self._static_config = dict(self._engine.config)
            else:
                self._engine.update_config(self._static_config)
            result = await self._engine.check_permission(
                tool_name=normalized_name,
                tool_args=tool_args,
                channel_id=self._resolve_channel_id(),
            )

            if result.permission == PermissionLevel.ALLOW:
                logger.info(ToolPermissionLog(
                    tool=tool_name,
                    decision="ALLOW",
                    source="system",
                    rule=result.matched_rule or "N/A",
                    channel=self._resolve_channel_id() or "empty",
                    session_id=self._resolve_session_id(ctx) or "N/A",
                ).to_json(), extra={'component': 'permissions'})
                return self.approve()

            if result.permission == PermissionLevel.DENY:
                logger.warning(ToolPermissionLog(
                    tool=tool_name,
                    decision="DENY",
                    source="system",
                    rule=result.matched_rule or "N/A",
                    channel=self._resolve_channel_id() or "empty",
                    session_id=self._resolve_session_id(ctx) or "N/A",
                ).to_json(), extra={'component': 'permissions'})
                return self.reject(tool_result=f"[PERMISSION_DENIED] {result.reason or 'Operation not allowed'}")

            if self._is_auto_confirmed(auto_confirm_config, auto_confirm_key):
                logger.info(ToolPermissionLog(
                    tool=tool_name,
                    decision="ALLOW",
                    source="system",
                    rule=f"auto_confirm:{auto_confirm_key}",
                    channel=self._resolve_channel_id() or "empty",
                    session_id=self._resolve_session_id(ctx) or "N/A",
                ).to_json(), extra={'component': 'permissions'})
                return self.approve()

            resolved_channel = self._resolve_channel_id()
            if resolved_channel == "acp":
                confirm_payload = await self._request_acp_permission(
                    ctx=ctx,
                    tool_call=tool_call,
                    result=result,
                    auto_confirm_key=auto_confirm_key,
                )
                if confirm_payload is None:
                    return self.reject(
                        tool_result=(
                            f"[PERMISSION_DENIED] {result.reason or 'Operation requires approval'} "
                            "(ACP permission request failed)"
                        )
                    )
                should_persist = confirm_payload.persist_allow
                persisted = False
                if should_persist:
                    persisted = persist_permission_allow_rule(
                        normalized_name,
                        tool_args,
                        permission_context=self._build_pending_permission_context(
                            normalized_name,
                            tool_args,
                            result,
                        ),
                    )
                    logger.info(
                        "[PermissionEngine] permission.persist.result tool=%s channel=acp persisted=%s",
                        tool_name,
                        persisted,
                    )
                    if result.file_operations:
                        try:
                            persist_file_operations_allow(list(result.file_operations))
                            persisted = True
                            logger.info(
                                "[PermissionEngine] permission.persist.file_operations tool=%s channel=acp count=%s",
                                tool_name,
                                len(result.file_operations),
                            )
                        except Exception:
                            persisted = False
                            logger.exception(
                                "[PermissionEngine] permission.persist.file_operations.failed tool=%s",
                                tool_name,
                            )
                if self._should_store_auto_confirm(
                    auto_confirm=confirm_payload.auto_confirm,
                    session=ctx.session,
                    auto_confirm_key=auto_confirm_key,
                    persisted=persisted,
                    persist_requested=confirm_payload.persist_allow,
                ):
                    self._store_auto_confirm(ctx, auto_confirm_key)
                if confirm_payload.approved:
                    decision = "allow_always" if confirm_payload.persist_allow else "allow_once"
                    logger.info(ToolPermissionLog(
                        tool=tool_name,
                        decision="ALLOW",
                        source="user",
                        rule=result.matched_rule or "N/A",
                        user_decision=decision,
                        channel="acp",
                        session_id=self._resolve_session_id(ctx) or "N/A",
                    ).to_json(), extra={'component': 'permissions'})
                    return self.approve()
                logger.info(ToolPermissionLog(
                    tool=tool_name,
                    decision="DENY",
                    source="user",
                    rule=result.matched_rule or "N/A",
                    user_decision="deny",
                    channel="acp",
                    session_id=self._resolve_session_id(ctx) or "N/A",
                ).to_json(), extra={'component': 'permissions'})
                return self.reject(
                    tool_result=confirm_payload.feedback or "[PERMISSION_REJECTED] User rejected the request."
                )

            logger.info(ToolPermissionLog(
                tool=tool_name,
                decision="ASK",
                source="system",
                rule=result.matched_rule or "N/A",
                channel=self._resolve_channel_id() or "empty",
                session_id=self._resolve_session_id(ctx) or "N/A",
            ).to_json(), extra={'component': 'permissions'})
            self._store_pending_permission_context(
                ctx,
                tool_call_id,
                self._build_pending_permission_context(normalized_name, tool_args, result),
            )
            message = self._build_message(tool_call, result)
            return self.interrupt(InterruptRequest(
                message=message,
                payload_schema=ConfirmPayload.to_schema(),
            ))

        logger.info("[PermissionEngine] permission.rail.user_response tool=%s", tool_name)
        payload = self._parse_confirm_payload(user_input)
        if payload is None:
            logger.warning(
                "[PermissionEngine] permission.rail.invalid_response tool=%s "
                "tool_call_id=%s user_input_type=%s",
                tool_name,
                tool_call_id,
                type(user_input).__name__,
            )
            return self.reject(
                tool_result="[PERMISSION_REJECTED] Invalid permission confirmation payload."
            )

        persisted = False
        pending_context = self._pop_pending_permission_context(
            ctx,
            tool_call_id,
            normalized_name,
            tool_args,
        )
        if payload.persist_allow:
            persisted = persist_permission_allow_rule(
                normalized_name,
                tool_args,
                permission_context=pending_context,
            )
            logger.info(
                "[PermissionEngine] permission.persist.result tool=%s channel=%s persisted=%s",
                tool_name,
                self._resolve_channel_id(),
                persisted,
            )
            file_ops = self._restore_file_operations(pending_context)
            if file_ops:
                try:
                    persist_file_operations_allow(file_ops)
                    persisted = True
                    logger.info(
                        "[PermissionEngine] permission.persist.file_operations tool=%s channel=%s count=%s",
                        tool_name,
                        self._resolve_channel_id(),
                        len(file_ops),
                    )
                except Exception:
                    persisted = False
                    logger.exception(
                        "[PermissionEngine] permission.persist.file_operations.failed tool=%s",
                        tool_name,
                    )

        if self._should_store_auto_confirm(
            auto_confirm=payload.auto_confirm,
            session=ctx.session,
            auto_confirm_key=auto_confirm_key,
            persisted=persisted,
            persist_requested=payload.persist_allow,
        ):
            self._store_auto_confirm(ctx, auto_confirm_key)

        if payload.approved:
            decision = "allow_always" if payload.persist_allow else "allow_once"
            logger.info(ToolPermissionLog(
                tool=tool_name,
                decision="ALLOW",
                source="user",
                rule=pending_context.get("matched_rule") if pending_context else "N/A",
                user_decision=decision,
                channel=self._resolve_channel_id() or "empty",
                session_id=self._resolve_session_id(ctx) or "N/A",
            ).to_json(), extra={'component': 'permissions'})
            return self.approve()

        logger.info(ToolPermissionLog(
            tool=tool_name,
            decision="DENY",
            source="user",
            rule=pending_context.get("matched_rule") if pending_context else "N/A",
            user_decision="deny",
            channel=self._resolve_channel_id() or "empty",
            session_id=self._resolve_session_id(ctx) or "N/A",
        ).to_json(), extra={'component': 'permissions'})
        return self.reject(tool_result=payload.feedback or "[PERMISSION_REJECTED] User rejected the request.")

    @staticmethod
    def _parse_tool_args(tool_call: Optional[ToolCall]) -> dict:
        if tool_call is None:
            return {}
        args = tool_call.arguments
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        if isinstance(args, dict):
            return args
        return {}

    @staticmethod
    def _parse_confirm_payload(user_input: Any) -> Optional[PermissionConfirmResponse]:
        if isinstance(user_input, PermissionConfirmResponse):
            return user_input
        if isinstance(user_input, ConfirmPayload):
            return PermissionConfirmResponse(
                approved=user_input.approved,
                feedback=user_input.feedback,
                auto_confirm=user_input.auto_confirm,
            )
        if isinstance(user_input, dict):
            try:
                payload = ConfirmPayload.model_validate(user_input)
            except Exception:
                return None
            return PermissionConfirmResponse(
                approved=payload.approved,
                feedback=payload.feedback,
                auto_confirm=payload.auto_confirm,
                persist_allow=bool(user_input.get("persist_allow", False)),
            )
        if isinstance(user_input, str):
            try:
                raw_payload = json.loads(user_input)
            except Exception:
                return None
            if not isinstance(raw_payload, dict):
                return None
            return PermissionInterruptRail._parse_confirm_payload(raw_payload)
        return None

    @staticmethod
    def _resolve_channel_id() -> str:
        return TOOL_PERMISSION_CHANNEL_ID.get() or "web"

    @staticmethod
    def _is_auto_confirmed(auto_confirm_config: Optional[dict], tool_name: str) -> bool:
        if auto_confirm_config is None:
            return False
        return auto_confirm_config.get(tool_name, False)

    @staticmethod
    def _store_auto_confirm(ctx: AgentCallbackContext, auto_confirm_key: str) -> None:
        config = ctx.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) or {}
        if not isinstance(config, dict):
            config = {}
        config[auto_confirm_key] = True
        ctx.session.update_state({INTERRUPT_AUTO_CONFIRM_KEY: config})
        logger.info("[PermissionEngine] permission.auto_confirm.store key=%s", auto_confirm_key)

    @staticmethod
    def _read_session_attr_value(session: Any, attr_name: str) -> Any:
        attr = getattr(session, attr_name, None)
        if not callable(attr):
            return attr
        try:
            return attr()
        except Exception:
            logger.debug(
                "[PermissionEngine] permission.rail.session_attr_read_failed attr=%s",
                attr_name,
                exc_info=True,
            )
            return None

    @staticmethod
    def _resolve_session_id(ctx: AgentCallbackContext) -> str | None:
        session = getattr(ctx, "session", None)
        if session is None:
            return None

        for attr_name in ("get_session_id", "session_id"):
            value = PermissionInterruptRail._read_session_attr_value(session, attr_name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _tool_kind_for_permission(tool_name: str) -> str:
        if tool_name in {"bash", "mcp_exec_command", "create_terminal", "exec_command"}:
            return "execute"
        if tool_name in {"read_file", "read_text_file", "memory_get"}:
            return "read"
        if tool_name in {"write_file", "write_text_file", "edit_file", "write"}:
            return "edit"
        if tool_name in {"grep", "glob_file_search", "mcp_free_search", "mcp_paid_search", "mcp_petal_search"}:
            return "search"
        if tool_name in {"fetch_webpage", "mcp_fetch_webpage"}:
            return "fetch"
        return "other"

    def _build_acp_permission_request(
        self,
        tool_call: Optional[ToolCall],
        result: PermissionResult,
    ) -> dict[str, Any]:
        tool_name = tool_call.name if tool_call else ""
        tool_args = self._parse_tool_args(tool_call)
        tool_call_id = str(getattr(tool_call, "id", "") or f"permission_{tool_name or 'tool'}").strip()
        descriptor = build_acp_tool_descriptor(
            tool_name,
            tool_args,
            tool_call_id=tool_call_id,
            status="pending",
            kind=self._tool_kind_for_permission(tool_name),
        )
        title = str(descriptor.get("title") or f"Approve `{tool_name}`")
        if result.reason:
            title = f"{title}: {result.reason}"

        request: dict[str, Any] = {
            "toolCall": {
                **descriptor,
                "title": title,
            },
            "options": [
                {
                    "optionId": "allow-once",
                    "name": "Allow once",
                    "kind": "allow_once",
                },
                {
                    "optionId": "allow-always",
                    "name": "Always allow",
                    "kind": "allow_always",
                },
                {
                    "optionId": "reject-once",
                    "name": "Reject",
                    "kind": "reject_once",
                },
            ],
        }
        permission_context = self._build_pending_permission_context(tool_name, tool_args, result)
        file_ops_payload = permission_context.get("file_operations") or []

        if file_ops_payload:
            existing_locations = descriptor.get("locations") if isinstance(descriptor.get("locations"), list) else []
            seen_paths = {
                str(loc.get("path") or "").strip()
                for loc in existing_locations if isinstance(loc, dict)
            }
            extra_locations: list[dict[str, Any]] = []
            for op in file_ops_payload:
                path = str(op.get("path") or "").strip()
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                extra_locations.append({"path": path})
            if extra_locations:
                request["toolCall"]["locations"] = list(existing_locations) + extra_locations

            raw_input = request["toolCall"].get("rawInput")
            if not isinstance(raw_input, dict):
                raw_input = {}
            raw_input["__file_operations"] = list(file_ops_payload)
            request["toolCall"]["rawInput"] = raw_input

        request["permissionContext"] = {
            "askSubcommands": permission_context.get("ask_subcommands", []),
            "wouldPersistPatterns": permission_context.get("would_persist_patterns", []),
            "wouldPersistWholeTool": bool(permission_context.get("would_persist_whole_tool", False)),
            "displayMatchedRule": self.display_matched_rule(tool_name, result),
            "persistDisabledReason": permission_context.get("persist_disabled_reason", ""),
            "persistAllowTargets": permission_context.get("would_persist_patterns", []) or (
                [tool_name] if permission_context.get("would_persist_whole_tool") else []
            ),
            "toolName": tool_name,
            "fileOperations": list(file_ops_payload),
            "toolDescription": self._extract_tool_description(tool_args),
        }
        return request

    async def _request_acp_permission(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[ToolCall],
        result: PermissionResult,
        auto_confirm_key: str,
    ) -> PermissionConfirmResponse | None:
        session_id = self._resolve_session_id(ctx)
        if not session_id:
            logger.warning("[PermissionEngine] permission.acp.request_skipped reason=missing_session_id")
            return None

        from jiuwenclaw.agentserver.tools.acp_output_tools import get_acp_output_manager

        request_params = self._build_acp_permission_request(tool_call, result)
        logger.info(
            "[PermissionEngine] permission.acp.request_start session_id=%s tool=%s auto_confirm_key=%s",
            session_id,
            tool_call.name if tool_call else "",
            auto_confirm_key,
        )
        try:
            response = await get_acp_output_manager().send_jsonrpc_request(
                "session/request_permission",
                request_params,
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning("[PermissionEngine] permission.acp.request_failed error_type=%s", type(exc).__name__)
            return None

        if not isinstance(response, dict):
            logger.warning("[PermissionEngine] permission.acp.invalid_response response_type=%s", type(response).__name__)
            return None

        if isinstance(response.get("error"), dict):
            err = response["error"]
            message = str(err.get("message") or "Permission request failed")
            logger.warning("[PermissionEngine] permission.acp.error_response code=%s", err.get("code"))
            return PermissionConfirmResponse(
                approved=False,
                auto_confirm=False,
                feedback=f"[PERMISSION_DENIED] {message}",
            )

        result_payload = response.get("result") if isinstance(response.get("result"), dict) else {}
        outcome = result_payload.get("outcome") if isinstance(result_payload.get("outcome"), dict) else {}
        outcome_kind = str(outcome.get("outcome") or "").strip().lower()
        option_id = str(outcome.get("optionId") or "").strip().lower()

        if outcome_kind == "selected":
            if option_id == "allow-once":
                return PermissionConfirmResponse(approved=True, auto_confirm=False, feedback="")
            if option_id == "allow-always":
                return PermissionConfirmResponse(
                    approved=True,
                    auto_confirm=True,
                    persist_allow=True,
                    feedback="",
                )
            if option_id in {"reject-once", "reject-always"}:
                return PermissionConfirmResponse(
                    approved=False,
                    auto_confirm=False,
                    feedback="[PERMISSION_REJECTED] User rejected the request.",
                )
            logger.warning(
                "[PermissionEngine] permission.acp.unknown_option option_id=%s",
                option_id,
            )
            return PermissionConfirmResponse(
                approved=False,
                auto_confirm=False,
                feedback=f"[PERMISSION_DENIED] Unknown permission option: {option_id or 'empty'}",
            )

        if outcome_kind == "cancelled":
            return PermissionConfirmResponse(
                approved=False,
                auto_confirm=False,
                feedback="[PERMISSION_REJECTED] Permission request was cancelled.",
            )

        logger.warning(
            "[PermissionEngine] permission.acp.unknown_outcome outcome=%s has_payload=%s",
            outcome_kind,
            bool(result_payload),
        )
        return PermissionConfirmResponse(
            approved=False,
            auto_confirm=False,
            feedback="[PERMISSION_DENIED] Invalid ACP permission response.",
        )

    @staticmethod
    def _format_args_preview(tool_args: dict) -> str:
        try:
            return json.dumps(tool_args, ensure_ascii=False, indent=2)[:1000]
        except Exception:
            return str(tool_args)[:1000]

    @staticmethod
    def _extract_tool_description(tool_args: dict) -> str:
        """读取工具自带的 ``description`` 字段（如 ``mcp_exec_command`` 的语义描述）。

        - 仅接受字符串；非字符串或空白一律视为缺省。
        - 截断长度，避免审批卡被超长描述撑爆。
        """
        if not isinstance(tool_args, dict):
            return ""
        raw = tool_args.get("description")
        if not isinstance(raw, str):
            return ""
        text = raw.strip()
        if not text:
            return ""
        max_len = 500
        if len(text) > max_len:
            text = text[:max_len].rstrip() + "…"
        return text

    def _build_message(
        self,
        tool_call: Optional[ToolCall],
        result: PermissionResult,
    ) -> str:
        tool_name = tool_call.name if tool_call else ""
        tool_args = self._parse_tool_args(tool_call)
        preview = self._build_persist_preview(tool_name, tool_args, result)
        risk = self.build_risk_for_message(tool_name, tool_args, result, preview)
        persist_targets_text = self._format_inline_code_items(preview.targets)
        target_suffix = ""
        if is_shell_permission_tool(tool_name) and persist_targets_text:
            target_suffix = f" {persist_targets_text}"
        elif preview.file_operations:
            target_suffix = "文件操作"
        elif is_shell_permission_tool(tool_name) and preview.disabled_reason:
            target_suffix = ""

        parts = [
            f"**工具 `{tool_name}` 需要授权才能执行{target_suffix}**\n\n",
        ]
        description = self._extract_tool_description(tool_args)
        if description:
            parts.append(f"> 行为意图：{description}\n\n")

        file_ops = list(result.file_operations or [])
        if file_ops:
            parts.append(f"> {self._render_file_operations_section(file_ops)}\n\n")

        parts.extend([
            f"**风险等级：{risk.get('level', '')}风险**\n\n",
            #f"> {risk.get('explanation', '')}\n\n",
        ])

        # args_preview = self._format_args_preview(tool_args)
        # if args_preview and args_preview != "{}":
        #     parts.append(f"参数：\n```json\n{args_preview}\n```\n")

        #parts.append(f"\n匹配规则：`{self.display_matched_rule(tool_name, result)}`")

        # external_paths = getattr(result, "external_paths", None) or []
        # if external_paths:
        #     parts.append(f"\n\n**外部路径：** `{', '.join(external_paths)}`")

        # parts.append(self._build_always_allow_hint(tool_name, preview))

        return "".join(parts)

    @staticmethod
    def _render_file_operations_section(file_ops: list[FileOperation]) -> str:
        if not file_ops:
            return ""
        verb_cn = {"read": "读取", "write": "写入", "exec": "执行"}
        groups: dict[str, list[FileOperation]] = {}
        for op in file_ops:
            groups.setdefault(op.source, []).append(op)

        order = ["tool_arg", "shlex", "llm", "script_scan"]
        chunks: list[str] = ["\n\n**需要授权的文件操作：**"]
        for src in order:
            ops = groups.get(src)
            if not ops:
                continue
            for op in ops:
                verb = verb_cn.get(op.action, op.action)
                chunks.append(f"\n- {verb} `{op.path}`")
        for src, ops in groups.items():
            if src in order:
                continue
            for op in ops:
                verb = verb_cn.get(op.action, op.action)
                chunks.append(f"\n- {verb} `{op.path}`")
        return "".join(chunks)

    def _build_always_allow_hint(
        self,
        tool_name: str,
        preview: PersistPreview,
    ) -> str:
        targets_text = self._format_inline_code_items(preview.targets)
        file_targets_text = self._format_file_operation_targets(preview.file_operations or [])
        if targets_text and file_targets_text:
            if preview.whole_tool:
                return (
                    f'\n\n> 选择"总是允许"将允许整个工具：{targets_text}；'
                    f"并允许文件操作：{file_targets_text}"
                )
            return (
                f'\n\n> 选择"总是允许"将写入持久化允许规则：{targets_text}；'
                f"并允许文件操作：{file_targets_text}"
            )
        if targets_text:
            if preview.whole_tool:
                return f'\n\n> 选择"总是允许"将允许整个工具：{targets_text}'
            return f'\n\n> 选择"总是允许"将写入持久化允许规则：{targets_text}'
        if file_targets_text:
            return f'\n\n> 选择"总是允许"将允许文件操作：{file_targets_text}'

        if preview.disabled_reason:
            return (
                f'\n\n> 当前命令{preview.disabled_reason}；选择"总是允许"不会写入持久化允许规则，'
                "后续相同或类似命令仍可能再次审批"
            )
        return ""

    @classmethod
    def _format_file_operation_targets(cls, file_operations: list[FileOperation]) -> str:
        verb_cn = {"read": "读取", "write": "写入", "exec": "执行"}
        items: list[str] = []
        seen: set[tuple[str, str]] = set()
        for op in file_operations:
            action = str(op.action or "").strip()
            path = str(op.path or "").strip()
            if not action or not path:
                continue
            key = (action, path)
            if key in seen:
                continue
            seen.add(key)
            items.append(f"{verb_cn.get(action, action)} {path}")
        return cls._format_inline_code_items(items)


__all__ = [
    "PermissionInterruptRail",
]
