# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Circuit breaker for repeated access-denied / credential-probing loops.

The default ``agent_ras`` loop detectors key on identical tool arguments.
Agents that retry the same *goal* with a different script on every turn
(``auth_probe.py``, ``token_hunt.py``, ``leveldb_scan.py`` ...) never match
those detectors.  This rail counts two strategy-level signals instead:

- repeated ``401 / AUTH_SESSION_TERMINATED`` results within a time window;
- repeated attempts to read or copy session credentials
  (``session.cookie`` / Chromium ``Cookies`` / LevelDB / an explicit DPAPI
  helper or Win32 DPAPI API invocation).

Mentioning ``DPAPI`` while reading logs or source code is normal diagnosis,
not credential probing.  The DPAPI signal therefore requires an actual
operation (helper ``--mode dpapi-protect|dpapi-unprotect`` in space, equals,
or argv form, or ``CryptProtectData`` / ``CryptUnprotectData``), not the bare
word.

Credential probes are blocked at the tool-result level first, so the model can
switch to a supported path (web search / BrowserSkill / user export).  Only if
it keeps probing does the rail finish the turn with a normal answer.  The
threshold-hit tool result is rewritten too, so the last real read cannot leak
credentials into the model context.  This avoids turning a policy refusal into
a fatal ``chat.error`` (which the host classifies as an unknown failure such as
OA.05000090).

Enforcement is driven by tool *arguments* (what the agent asks the tool to do).
Credential-like text in a tool *result* (reading the rail's own source, a DPAPI
error log, or a grep hit) is counted for observability only and never rewrites
the result, otherwise normal diagnosis would be interrupted.
"""

from __future__ import annotations

import re
import reprlib
import time
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import logger

_FATAL_ACCESS = re.compile(r"AUTH_SESSION_TERMINATED", re.IGNORECASE)
# 裸词 "DPAPI" 不能作为指纹：正常排障会在工具参数/结果里大量出现该词
# （例如 grep "native DPAPI helper failed"、读取 dpapi.ts 源码），会被误判成
# 凭证探测并提前结束排障任务。只匹配真实调用 DPAPI helper 或 Win32 API 的形态。
_CREDENTIAL_PROBE = re.compile(
    r"session\.cookie|Network[\\/]Cookies|Local Storage[\\/]leveldb|"
    r"Login Data|CryptUnprotectData|CryptProtectData|"
    r"--mode[=\s]+[\"']?dpapi-(?:protect|unprotect)[\"']?\b",
    re.IGNORECASE,
)

# Only tools that can reach local app data or credentials participate.
# Generic web tools may legitimately return 401 from third-party sites.
_RELEVANT_TOOLS = frozenset(
    {
        "bash",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "invoke_tool",
        "mcp_exec_command",
        "exec_command",
        "run_command",
        "execute_command",
        "shell",
        "execute_code",
        "code_interpreter",
        "office_claw_list_wishes",
    }
)

_SCAN_LIMIT = 64 * 1024
_TEXT_KEYS = ("content", "output", "error", "message", "stdout", "stderr", "result", "text")
_REPR = reprlib.Repr()
_REPR.maxstring = _SCAN_LIMIT
_REPR.maxother = _SCAN_LIMIT


def _head_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return f"{text[:half]}\n...\n{text[-half:]}"


def _bounded_text(value: Any, *, limit: int = _SCAN_LIMIT) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _head_tail(value, limit)
    if isinstance(value, (bytes, bytearray)):
        return _bounded_text(bytes(value).decode("utf-8", errors="replace"), limit=limit)
    if isinstance(value, dict):
        parts = [_bounded_text(value[key], limit=limit) for key in _TEXT_KEYS if key in value]
        if parts:
            return _head_tail("\n".join(parts), limit)
    object_parts: list[str] = []
    for key in ("data",) + _TEXT_KEYS:
        attr = _safe_getattr(value, key)
        if attr is not None:
            object_parts.append(_bounded_text(attr, limit=limit))
    if object_parts:
        return _head_tail("\n".join(object_parts), limit)
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        per_item = max(1, limit // min(len(value), 8))
        return _head_tail(
            "\n".join(_bounded_text(item, limit=per_item) for item in value[:8]),
            limit,
        )
    return _head_tail(_REPR.repr(value), limit)


def _safe_getattr(value: Any, key: str) -> Any:
    try:
        return getattr(value, key, None)
    except Exception:  # noqa: BLE001
        return None


def _iter_leaf_text(value: Any, *, _depth: int = 0) -> Any:
    """Yield text leaves from nested tool args without repr escaping.

    argv-style args (``["--mode", "dpapi-unprotect"]``) must stay separated by
    whitespace, not by ``', '`` from ``repr``, otherwise the credential
    fingerprint cannot see the ``--mode`` / value pair.
    """
    if value is None or _depth > 6:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, (bytes, bytearray)):
        yield _bounded_text(value)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_leaf_text(item, _depth=_depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_leaf_text(item, _depth=_depth + 1)
        return
    for key in ("data",) + _TEXT_KEYS:
        attr = _safe_getattr(value, key)
        if attr is not None:
            yield from _iter_leaf_text(attr, _depth=_depth + 1)


def _flatten_text(value: Any, *, limit: int = _SCAN_LIMIT) -> str:
    parts: list[str] = []
    size = 0
    for part in _iter_leaf_text(value):
        if not part:
            continue
        parts.append(part)
        size += len(part) + 1
        if size >= limit:
            break
    return _head_tail("\n".join(parts), limit)


def _tool_args_text(inputs: Any) -> str:
    tool_args = getattr(inputs, "tool_args", None)
    if not tool_args:
        tool_call = getattr(inputs, "tool_call", None)
        tool_args = getattr(tool_call, "arguments", None)
        if not tool_args:
            function = getattr(tool_call, "function", None)
            tool_args = getattr(function, "arguments", None)
    if not tool_args:
        return ""
    flattened = _flatten_text(tool_args)
    return flattened or _bounded_text(tool_args)


class StalledAccessCircuitRail(DeepAgentRail):
    """Stop strategy-level auth/credential retry loops."""

    priority: int = 97

    def __init__(
        self,
        *,
        auth_threshold: int = 5,
        probe_threshold: int = 3,
        window_seconds: float = 1800.0,
        language: str = "cn",
    ) -> None:
        super().__init__()
        self._auth_threshold = max(1, int(auth_threshold))
        self._probe_threshold = max(1, int(probe_threshold))
        self._window = max(1.0, float(window_seconds))
        self._language = "en" if str(language).lower().startswith("en") else "cn"
        self._denied_at: list[float] = []
        self._probe_at: list[float] = []
        self._tripped = False
        self._soft_refused = False
        self._result_probe_hits = 0

    def set_language(self, language: str | None) -> None:
        self._language = "en" if str(language or "").lower().startswith("en") else "cn"

    async def before_invoke(self, ctx: Any) -> None:
        self._denied_at = []
        self._probe_at = []
        self._tripped = False
        self._soft_refused = False
        self._result_probe_hits = 0

    async def after_tool_call(self, ctx: Any) -> None:
        if self._tripped:
            return
        inputs = getattr(ctx, "inputs", None)
        if inputs is None:
            return
        tool_name = str(getattr(inputs, "tool_name", "") or "")
        if tool_name not in _RELEVANT_TOOLS:
            return

        result_text = _bounded_text(getattr(inputs, "tool_result", None))
        args_text = _tool_args_text(inputs)
        now = time.monotonic()

        # 只有“工具要做什么”能作为强制执行依据；工具返回的纯文本里出现
        # session.cookie / CryptUnprotectData 等字样，可能只是读源码、读错误
        # 日志或 grep 命中，不能据此吞掉工具结果。结果侧命中只做观测计数。
        args_probe = bool(_CREDENTIAL_PROBE.search(args_text))
        result_probe = bool(_CREDENTIAL_PROBE.search(result_text))
        if args_probe:
            self._probe_at.append(now)
        elif result_probe:
            self._result_probe_hits += 1

        if _FATAL_ACCESS.search(result_text):
            self._denied_at.append(now)

        cutoff = now - self._window
        self._denied_at = [stamp for stamp in self._denied_at if stamp >= cutoff]
        self._probe_at = [stamp for stamp in self._probe_at if stamp >= cutoff]

        if len(self._denied_at) >= self._auth_threshold:
            self._tripped = True
            logger.warning(
                "[StalledAccessCircuitRail] tripped session_id=%s tool=%s "
                "probes_in_window=%s denied_in_window=%s result_probe_hits=%s",
                self._session_id(ctx),
                tool_name,
                len(self._probe_at),
                len(self._denied_at),
                self._result_probe_hits,
            )
            ctx.request_force_finish(
                {"output": self._auth_output(), "result_type": "error"}
            )
            return

        if not args_probe:
            if result_probe:
                logger.debug(
                    "[StalledAccessCircuitRail] credential-like text in tool result "
                    "is observability-only session_id=%s tool=%s result_probe_hits=%s",
                    self._session_id(ctx),
                    tool_name,
                    self._result_probe_hits,
                )
            return

        # 凭证探测属于策略层拒绝，不是任务本身失败：第一次命中先以工具结果
        # 形式回绝，让模型改走联网搜索 / BrowserSkill / 用户导出等合规路径，
        # 避免像旧实现那样直接 force_finish 成 chat.error（上层会归为
        # OA.05000090）。模型若继续探测，达到阈值后再用普通回答收尾。
        if len(self._probe_at) < max(2, self._probe_threshold):
            if self._inject_probe_refusal(ctx):
                if not self._soft_refused:
                    self._soft_refused = True
                    logger.warning(
                        "[StalledAccessCircuitRail] soft-refused credential probe "
                        "session_id=%s tool=%s probes_in_window=%s denied_in_window=%s "
                        "result_probe_hits=%s",
                        self._session_id(ctx),
                        tool_name,
                        len(self._probe_at),
                        len(self._denied_at),
                        self._result_probe_hits,
                    )
                return

            # 回调输入不是 ToolCallInputs 时无法改写工具结果；退化为普通回答，
            # 同样避免产生 chat.error。
            self._tripped = True
            logger.warning(
                "[StalledAccessCircuitRail] stopped credential probe without "
                "tool-result rewrite session_id=%s tool=%s probes_in_window=%s",
                self._session_id(ctx),
                tool_name,
                len(self._probe_at),
            )
            ctx.request_force_finish(
                {"output": self._probe_guidance(), "result_type": "answer"}
            )
            return

        self._tripped = True
        # 达到阈值的这一次探测也必须先改写结果，否则最后一次真实读取的
        # 凭证内容会进入模型上下文 / history，force_finish 只覆盖最终回答
        # 并不能回收已经泄漏的工具结果。
        rewritten = self._inject_probe_refusal(ctx)
        logger.warning(
            "[StalledAccessCircuitRail] stopped after repeated credential probes "
            "session_id=%s tool=%s probes_in_window=%s denied_in_window=%s "
            "result_probe_hits=%s tool_result_rewritten=%s",
            self._session_id(ctx),
            tool_name,
            len(self._probe_at),
            len(self._denied_at),
            self._result_probe_hits,
            rewritten,
        )
        ctx.request_force_finish(
            {"output": self._probe_guidance(), "result_type": "answer"}
        )

    def _auth_output(self) -> str:
        if self._language == "en":
            return (
                "This data source requires a fresh sign-in, and the agent is not "
                "allowed to read session credentials (session.cookie, browser "
                "Cookies, LevelDB, or DPAPI).\n"
                "Please sign in again or export the data and retry."
            )
        return (
            "当前数据源需要重新登录，且系统不允许读取会话凭证"
            "（session.cookie / 浏览器 Cookies / LevelDB / DPAPI）。\n"
            "请重新登录或导出数据后重试。"
        )

    def _probe_guidance(self) -> str:
        if self._language == "en":
            return (
                "Reading browser session credentials (Cookies / LevelDB / DPAPI / "
                "session.cookie) is blocked by security policy.\n"
                "Continue with: (1) web search for public content; (2) BrowserSkill "
                "on an already-open page; (3) ask the user to export the data. "
                "Do not retry credential access."
            )
        return (
            "安全策略禁止读取浏览器会话凭证（Cookies / LevelDB / DPAPI / session.cookie）。\n"
            "请改用：①联网搜索获取公开内容；②BrowserSkill 操作已打开的页面；"
            "③请用户导出数据后再处理。不要再次尝试读取凭证。"
        )

    def _inject_probe_refusal(self, ctx: Any) -> bool:
        from openjiuwen.core.foundation.llm import ToolMessage
        from openjiuwen.core.single_agent.rail.base import ToolCallInputs

        inputs = getattr(ctx, "inputs", None)
        if not isinstance(inputs, ToolCallInputs):
            return False

        tool_call = getattr(inputs, "tool_call", None)
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        text = f"[ERROR]: {self._probe_guidance()}"
        inputs.tool_result = text
        tool_msg = getattr(inputs, "tool_msg", None)
        if tool_msg is not None:
            tool_msg.content = text
            if tool_call_id and getattr(tool_msg, "tool_call_id", None) in (None, ""):
                tool_msg.tool_call_id = tool_call_id
            metadata = getattr(tool_msg, "metadata", None)
            if isinstance(metadata, dict):
                metadata["is_error"] = True
        else:
            inputs.tool_msg = ToolMessage(
                content=text,
                tool_call_id=tool_call_id,
                metadata={"is_error": True},
            )
        return True

    @staticmethod
    def _session_id(ctx: Any) -> str:
        session = getattr(ctx, "session", None)
        try:
            return str(session.get_session_id() or "") if session is not None else ""
        except Exception:  # noqa: BLE001
            return ""


__all__ = ["StalledAccessCircuitRail"]
