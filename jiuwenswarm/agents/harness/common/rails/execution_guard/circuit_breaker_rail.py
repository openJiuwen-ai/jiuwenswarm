# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""CircuitBreakerRail - Agent 循环检测断路器.

检测类型:
  - generic_repeat:      相同工具+参数重复 (WARNING≥10)
  - unknown_tool_repeat: 错误工具连续调用 (CRITICAL≥10)
  - global_breaker:      工具无进展兜底中断 (CRITICAL≥30)
  - ping_pong:           两工具交替循环，尾部无进展轮次 (WARNING≥10, CRITICAL≥20)
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import logger


@dataclass
class CircuitBreakerConfig:
    warning_threshold: int = 10
    critical_threshold: int = 20
    global_breaker_threshold: int = 30
    unknown_tool_threshold: int = 10

    @property
    def history_size(self) -> int:
        return max(
            4 * self.critical_threshold,
            2 * self.global_breaker_threshold,
            2 * self.unknown_tool_threshold,
        )

_invoke_sid: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "circuit_breaker_invoke_sid",
    default=None,
)

# 报错文案按语言 × 检测器分表，占位符 {tool_name} / {count} 在渲染时填充。
# 语言键与 openjiuwen SUPPORTED_LANGUAGES 对齐：("cn", "en")。
_MESSAGES: dict[str, dict[str, str]] = {
    "cn": {
        "global_circuit_breaker": "全局断路器: {tool_name} 连续 {count} 次无进展",
        "unknown_tool_repeat": "未知工具 {tool_name} 连续调用 {count} 次，停止重试",
        "ping_pong_critical": "Ping-Pong 循环: {count} 轮交替无进展，阻断",
        "ping_pong_warning": "Ping-Pong 警告: {count} 轮交替无进展",
        "generic_repeat": "工具 {tool_name} 已重复调用 {count} 次，请检查是否有效",
    },
    "en": {
        "global_circuit_breaker": (
            "Circuit breaker: {tool_name} made no progress for {count} consecutive calls"
        ),
        "unknown_tool_repeat": (
            "Unknown tool {tool_name} called {count} times in a row, stopping retries"
        ),
        "ping_pong_critical": (
            "Ping-pong loop: {count} alternating calls with no progress, blocked"
        ),
        "ping_pong_warning": "Ping-pong warning: {count} alternating calls with no progress",
        "generic_repeat": (
            "Tool {tool_name} has been repeated {count} times, please verify it is effective"
        ),
    },
}

_DEFAULT_LANGUAGE = "cn"

# 按工具排除不影响执行语义的顶层 metadata 键（不参与 args_hash）。
_METADATA_ONLY_TOP_LEVEL: dict[str, frozenset[str]] = {
    "bash": frozenset({"description"}),
    "powershell": frozenset({"description"}),
    "Agent": frozenset({"description"}),
}

__all__ = [
    "CircuitBreakerConfig",
    "CircuitBreakerRail",
    "ToolResultErrorDetector",
    "ToolCallRecord",
    "DetectionResult",
    "PingPongResult",
]


def _normalize_language(language: str | None) -> str:
    """归一语言键：config 用 zh，rail 内部用 cn；非法值回落默认。"""
    lang = (language or "").strip().lower()
    if lang == "zh":
        lang = "cn"
    return lang if lang in _MESSAGES else _DEFAULT_LANGUAGE


class ToolResultErrorDetector:
    """从工具返回结果的结构化字段推断是否发生错误。

    支持 ToolOutput / dict / JSON 字符串 / repr 字符串归一化后判定。
    当前与 CircuitBreakerRail 同文件维护；其他模块可直接 import 复用::

        from jiuwenswarm.agents.harness.common.rails.execution_guard.circuit_breaker_rail import (
            ToolResultErrorDetector,
        )
    """

    _ERROR_STATUSES = frozenset({"error", "failed", "failure"})
    _EXIT_KEYS = ("exit_code", "exitCode", "returncode", "return_code")
    _REPR_SUCCESS_FALSE = re.compile(r"^success\s*=\s*False\b", re.IGNORECASE)
    _REPR_SUCCESS_TRUE = re.compile(r"^success\s*=\s*True\b", re.IGNORECASE)

    @staticmethod
    def has_error(value: Any) -> bool:
        """结构化字段明确指示错误时返回 True，否则返回 False。"""
        payload = ToolResultErrorDetector._normalize(value)
        if payload is None:
            return False
        return ToolResultErrorDetector._dict_has_error(payload)

    @staticmethod
    def has_explicit_success(value: Any) -> bool:
        """结构化字段明确指示成功时返回 True，否则返回 False。"""
        payload = ToolResultErrorDetector._normalize(value)
        if payload is None or "success" not in payload:
            return False
        if ToolResultErrorDetector._boolish_true(payload.get("success")):
            return True
        return False

    @staticmethod
    def ctx_has_error(ctx: AgentCallbackContext) -> bool:
        """从回调上下文推断工具调用是否发生错误。"""
        if ctx.exception is not None:
            return True
        tool_msg = getattr(ctx.inputs, "tool_msg", None)
        if tool_msg is not None:
            return ToolResultErrorDetector.has_error(getattr(tool_msg, "content", None))
        return False

    @staticmethod
    def infer_record_has_error(
        tool_result: Any,
        ctx: AgentCallbackContext,
    ) -> bool:
        """推断单次工具调用是否计入 unknown_tool 连续错误 streak。

        tool_result 能明确判定成功/失败时以其为准；仅在结果不可解析时
        才回退到 ctx.exception / tool_msg，避免成功结果被 ctx 误判为错误。
        """
        if ToolResultErrorDetector.has_error(tool_result):
            return True
        if ToolResultErrorDetector.has_explicit_success(tool_result):
            return False
        return ToolResultErrorDetector.ctx_has_error(ctx)

    @staticmethod
    def _normalize(value: Any) -> dict | None:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        if hasattr(value, "success"):
            return {
                "success": getattr(value, "success", None),
                "data": getattr(value, "data", None),
                "error": getattr(value, "error", None),
            }
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.startswith("{"):
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict):
                    return parsed
            if ToolResultErrorDetector._REPR_SUCCESS_FALSE.match(text):
                return {"success": False}
            if ToolResultErrorDetector._REPR_SUCCESS_TRUE.match(text):
                return {"success": True}
        return None

    @staticmethod
    def _dict_has_error(payload: dict, *, check_data: bool = True) -> bool:
        if "success" in payload:
            if ToolResultErrorDetector._boolish_false(payload.get("success")):
                return True
            if ToolResultErrorDetector._boolish_true(payload.get("success")):
                return False
        if (
            ToolResultErrorDetector._boolish_true(payload.get("is_error"))
            or ToolResultErrorDetector._boolish_true(payload.get("isError"))
        ):
            return True
        status = payload.get("status")
        if isinstance(status, str) and status.strip().lower() in ToolResultErrorDetector._ERROR_STATUSES:
            return True
        result_type = payload.get("result_type")
        if isinstance(result_type, str) and result_type.strip().lower() == "error":
            return True
        err = payload.get("error")
        if err is not None and str(err).strip() and str(err).strip().lower() != "none":
            return True
        for key in ToolResultErrorDetector._EXIT_KEYS:
            code = payload.get(key)
            if isinstance(code, int) and code != 0:
                return True
        if check_data:
            data = payload.get("data")
            if isinstance(data, dict) and ToolResultErrorDetector._dict_has_error(data, check_data=False):
                return True
        return False

    @staticmethod
    def _boolish_false(value: Any) -> bool:
        if value is False:
            return True
        return isinstance(value, str) and value.strip().lower() in {"false", "0", "no"}

    @staticmethod
    def _boolish_true(value: Any) -> bool:
        if value is True:
            return True
        return isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}


@dataclass
class ToolCallRecord:
    tool_name: str
    args_hash: str
    result_hash: str | None
    timestamp: float
    has_error: bool = False


@dataclass
class DetectionResult:
    stuck: bool = False
    level: str = ""
    detector: str = ""
    count: int = 0
    msg_key: str = ""
    tool_name: str = ""


@dataclass
class PingPongResult:
    no_progress_rounds: int = 0
    paired_tool: str | None = None
    no_progress: bool = False


class CircuitBreakerRail(DeepAgentRail):
    priority: int = 95

    _SID_KEY = "__jiuwenswarm_cb_session_id__"
    _STREAM_SID_KEY = "__jiuwenswarm_session_id__"

    def __init__(
        self,
        config: CircuitBreakerConfig | None = None,
        language: str = _DEFAULT_LANGUAGE,
    ):
        super().__init__()
        self._config = config or CircuitBreakerConfig()
        self._histories: dict[str, list[ToolCallRecord]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._language = _normalize_language(language)

    def set_language(self, language: str) -> None:
        """per-request 更新报错文案语言。"""
        self._language = _normalize_language(language)

    def cleanup_session(self, session_id: str = "") -> None:
        """Remove per-session history and lock for *session_id*."""
        sid = session_id or "default"
        self._histories.pop(sid, None)
        self._locks.pop(sid, None)

    def _resolve_sid(self, ctx: AgentCallbackContext) -> str:
        invoke_sid = _invoke_sid.get()
        if isinstance(invoke_sid, str) and invoke_sid:
            return invoke_sid

        sid = ctx.extra.get(self._SID_KEY)
        if isinstance(sid, str) and sid:
            return sid

        stream_sid = ctx.extra.get(self._STREAM_SID_KEY)
        if isinstance(stream_sid, str) and stream_sid:
            # Only reuse stream sid when it matches the active invoke bucket.
            if invoke_sid is None or stream_sid == invoke_sid:
                return stream_sid

        return "default"

    def _get_history(self, sid: str) -> list[ToolCallRecord]:
        history = self._histories.get(sid)
        if history is None:
            history = []
            self._histories[sid] = history
        return history

    def _get_lock(self, sid: str) -> asyncio.Lock:
        lock = self._locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[sid] = lock
        return lock

    def _format_message(self, result: DetectionResult) -> str:
        """按当前语言渲染检测结果文案。"""
        table = _MESSAGES.get(self._language, _MESSAGES[_DEFAULT_LANGUAGE])
        template = table.get(result.msg_key) or _MESSAGES[_DEFAULT_LANGUAGE].get(
            result.msg_key, ""
        )
        return template.format(tool_name=result.tool_name, count=result.count)

    # ------------------------------------------------------------------
    # before_invoke: 每条新消息清空当前 session 历史
    # before_tool_call: 绑定 invoke 级 sid
    # after_tool_call: 记录调用 + 检测 + 告警/中断
    # after_invoke: 清理 subagent / invoke 级状态
    # ------------------------------------------------------------------

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        if not isinstance(ctx.inputs, InvokeInputs):
            return
        raw_conv_id = ctx.inputs.conversation_id or ""
        sid = raw_conv_id or "default"
        ctx.extra[self._SID_KEY] = sid
        _invoke_sid.set(sid)
        self._histories[sid] = []

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        sid = _invoke_sid.get()
        if isinstance(sid, str) and sid:
            self.cleanup_session(sid)
        _invoke_sid.set(None)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        sid = self._resolve_sid(ctx)
        ctx.extra[self._SID_KEY] = sid

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        tool_call = ctx.inputs.tool_call
        if tool_call is None:
            return

        tool_name = getattr(tool_call, "name", "")
        tool_args = getattr(tool_call, "arguments", {})
        tool_result = ctx.inputs.tool_result

        if not tool_name:
            return

        sid = self._resolve_sid(ctx)
        args_hash = self._hash_args(tool_name, tool_args)
        result_hash = self._hash_outcome(tool_result)
        has_error = ToolResultErrorDetector.infer_record_has_error(
            tool_result, ctx,
        )

        async with self._get_lock(sid):
            history = self._get_history(sid)
            history.append(ToolCallRecord(
                tool_name=tool_name,
                args_hash=args_hash,
                result_hash=result_hash,
                timestamp=time.time(),
                has_error=has_error,
            ))
            if len(history) > self._config.history_size:
                self._histories[sid] = history[-self._config.history_size:]
                history = self._histories[sid]

            result = self._detect(history, tool_name, args_hash)

            if result.stuck and result.level == "critical":
                message = self._format_message(result)
                logger.error("[CircuitBreaker] %s", message)
                ctx.request_force_finish({
                    "output": message,
                    "result_type": "answer",
                })
            elif result.stuck and result.level == "warning":
                logger.warning("[CircuitBreaker] %s", self._format_message(result))

    # ------------------------------------------------------------------
    # _detect: 四种检测器按优先级依次检查
    # ------------------------------------------------------------------

    def _detect(
        self,
        history: list[ToolCallRecord],
        tool_name: str,
        args_hash: str,
    ) -> DetectionResult:
        cfg = self._config

        no_progress = self._get_no_progress_streak(history, tool_name, args_hash)
        if no_progress >= cfg.global_breaker_threshold:
            return DetectionResult(stuck=True, level="critical",
                detector="global_circuit_breaker", count=no_progress,
                msg_key="global_circuit_breaker", tool_name=tool_name)

        unknown_streak = self._get_unknown_tool_streak(history, tool_name)
        if unknown_streak >= cfg.unknown_tool_threshold:
            return DetectionResult(stuck=True, level="critical",
                detector="unknown_tool_repeat", count=unknown_streak,
                msg_key="unknown_tool_repeat", tool_name=tool_name)

        ping_pong = self._get_ping_pong_streak(history, args_hash)
        if (
            ping_pong.no_progress
            and ping_pong.no_progress_rounds >= cfg.critical_threshold
        ):
            return DetectionResult(stuck=True, level="critical",
                detector="ping_pong", count=ping_pong.no_progress_rounds,
                msg_key="ping_pong_critical", tool_name=tool_name)
        if (
            ping_pong.no_progress
            and ping_pong.no_progress_rounds >= cfg.warning_threshold
        ):
            return DetectionResult(stuck=True, level="warning",
                detector="ping_pong", count=ping_pong.no_progress_rounds,
                msg_key="ping_pong_warning", tool_name=tool_name)

        recent = self._count_recent_same(history, tool_name, args_hash)
        if recent >= cfg.warning_threshold:
            return DetectionResult(stuck=True, level="warning",
                detector="generic_repeat", count=recent,
                msg_key="generic_repeat", tool_name=tool_name)

        return DetectionResult(stuck=False)

    @staticmethod
    def _canonicalize_args_for_hash(tool_name: str, params: Any) -> Any:
        """Strip tool-specific metadata keys before args hashing."""
        if not isinstance(params, dict):
            return params
        excluded = _METADATA_ONLY_TOP_LEVEL.get(tool_name, frozenset())
        if not excluded:
            return params
        return {key: value for key, value in params.items() if key not in excluded}

    @staticmethod
    def _hash_args(tool_name: str, params: dict) -> str:
        canonical = json.dumps(
            CircuitBreakerRail._canonicalize_args_for_hash(tool_name, params),
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()

    def _hash_outcome(self, result: Any) -> str | None:
        if result is None:
            return None
        normalized = self._normalize_result(result)
        return hashlib.sha256(
            json.dumps(normalized, sort_keys=True, default=str).encode()
        ).hexdigest()

    @staticmethod
    def _normalize_result(result: Any) -> dict:
        if isinstance(result, dict):
            return {
                "content": str(result.get("content", "")).strip(),
                "output": str(result.get("output", "")).strip(),
                "error": str(result.get("error", "")).strip(),
                "status": str(result.get("status", "")),
            }
        return {"raw": str(result)}

    # ------------------------------------------------------------------
    # 计数方法
    # ------------------------------------------------------------------

    def _get_no_progress_streak(
        self,
        history: list[ToolCallRecord],
        tool_name: str,
        args_hash: str,
    ) -> int:
        streak = 0
        latest_hash = None
        for record in reversed(history):
            if record.tool_name != tool_name or record.args_hash != args_hash:
                continue
            if record.result_hash is None:
                continue
            if latest_hash is None:
                latest_hash = record.result_hash
                streak = 1
            elif record.result_hash == latest_hash:
                streak += 1
            else:
                break
        return streak

    @staticmethod
    def _get_side_tail_streak(records: list[ToolCallRecord]) -> int:
        """从该侧最后一次调用向前，统计连续相同 result_hash 次数."""
        streak = 0
        latest_hash = None
        for record in reversed(records):
            if record.result_hash is None:
                break
            if latest_hash is None:
                latest_hash = record.result_hash
                streak = 1
            elif record.result_hash == latest_hash:
                streak += 1
            else:
                break
        return streak

    @staticmethod
    def _compute_ping_pong_no_progress_rounds(
        side_a: list[ToolCallRecord],
        side_b: list[ToolCallRecord],
    ) -> int:
        return min(
            CircuitBreakerRail._get_side_tail_streak(side_a),
            CircuitBreakerRail._get_side_tail_streak(side_b),
        )

    def _collect_alternating_streak(
        self,
        history: list[ToolCallRecord],
    ) -> tuple[list[ToolCallRecord], str | None]:
        """从末尾向前收集完整交替序列（含 last），时间正序返回."""
        if len(history) < 2:
            return [], None
        last_hash = history[-1].args_hash
        other_hash = other_name = None
        for record in reversed(history[:-1]):
            if record.args_hash != last_hash:
                other_hash = record.args_hash
                other_name = record.tool_name
                break
        if other_hash is None:
            return [], None

        streak_rev = [history[-1]]
        expect = other_hash
        for record in reversed(history[:-1]):
            if record.args_hash != expect:
                break
            streak_rev.append(record)
            expect = last_hash if expect == other_hash else other_hash
        streak_rev.reverse()
        return streak_rev, other_name

    def _get_ping_pong_streak(
        self,
        history: list[ToolCallRecord],
        _current_args_hash: str,
    ) -> PingPongResult:
        streak, paired_tool = self._collect_alternating_streak(history)
        if len(streak) < 2:
            return PingPongResult()

        hash_last = streak[-1].args_hash
        hash_other = next(r.args_hash for r in streak if r.args_hash != hash_last)

        side_last = [r for r in streak if r.args_hash == hash_last]
        side_other = [r for r in streak if r.args_hash == hash_other]

        no_progress_rounds = self._compute_ping_pong_no_progress_rounds(
            side_last, side_other,
        )
        no_progress = no_progress_rounds >= 2

        return PingPongResult(
            no_progress_rounds=no_progress_rounds,
            paired_tool=paired_tool,
            no_progress=no_progress,
        )

    def _count_recent_same(
        self,
        history: list[ToolCallRecord],
        tool_name: str,
        args_hash: str,
    ) -> int:
        return sum(
            1 for r in history
            if r.tool_name == tool_name and r.args_hash == args_hash
        )

    def _get_unknown_tool_streak(
        self,
        history: list[ToolCallRecord],
        tool_name: str,
    ) -> int:
        streak = 0
        for record in reversed(history):
            if record.tool_name != tool_name:
                break
            if not record.has_error:
                break
            streak += 1
        return streak