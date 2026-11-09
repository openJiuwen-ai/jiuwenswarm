# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Circuit breaker for repeated access-denied / credential-probing loops.

The default ``agent_ras`` loop detectors key on identical tool arguments.
Agents that retry the same *goal* with a different script on every turn
(``auth_probe.py``, ``token_hunt.py``, ``leveldb_scan.py`` ...) never match
those detectors.  This rail counts two strategy-level signals instead:

- repeated ``401 / AUTH_SESSION_TERMINATED`` results within a time window;
- repeated attempts to read or copy session credentials
  (``session.cookie`` / Chromium ``Cookies`` / LevelDB / DPAPI).

When either threshold trips, the rail finishes the turn with an actionable
message instead of letting the agent burn the whole ReAct budget.
"""

from __future__ import annotations

import re
import reprlib
import time
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import logger

_FATAL_ACCESS = re.compile(r"AUTH_SESSION_TERMINATED", re.IGNORECASE)
_CREDENTIAL_PROBE = re.compile(
    r"session\.cookie|Network[\\/]Cookies|Local Storage[\\/]leveldb|"
    r"Login Data|CryptUnprotectData|CryptProtectData|DPAPI",
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


def _tool_args_text(inputs: Any) -> Any:
    tool_args = getattr(inputs, "tool_args", None)
    if tool_args:
        return tool_args
    tool_call = getattr(inputs, "tool_call", None)
    tool_args = getattr(tool_call, "arguments", None)
    if tool_args:
        return tool_args
    function = getattr(tool_call, "function", None)
    return getattr(function, "arguments", None)


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

    def set_language(self, language: str | None) -> None:
        self._language = "en" if str(language or "").lower().startswith("en") else "cn"

    async def before_invoke(self, ctx: Any) -> None:
        self._denied_at = []
        self._probe_at = []
        self._tripped = False

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
        args_text = _bounded_text(_tool_args_text(inputs))
        now = time.monotonic()

        if _CREDENTIAL_PROBE.search(result_text) or _CREDENTIAL_PROBE.search(args_text):
            self._probe_at.append(now)

        if _FATAL_ACCESS.search(result_text):
            self._denied_at.append(now)

        cutoff = now - self._window
        self._denied_at = [stamp for stamp in self._denied_at if stamp >= cutoff]
        self._probe_at = [stamp for stamp in self._probe_at if stamp >= cutoff]

        if (
            len(self._probe_at) < self._probe_threshold
            and len(self._denied_at) < self._auth_threshold
        ):
            return

        self._tripped = True
        if self._language == "en":
            output = (
                "This data source requires a fresh sign-in, and the agent is not "
                "allowed to read session credentials (session.cookie, browser "
                "Cookies, LevelDB, or DPAPI).\n"
                "Please sign in again or export the data and retry."
            )
        else:
            output = (
                "当前数据源需要重新登录，且系统不允许读取会话凭证"
                "（session.cookie / 浏览器 Cookies / LevelDB / DPAPI）。\n"
                "请重新登录或导出数据后重试。"
            )
        logger.warning(
            "[StalledAccessCircuitRail] tripped session_id=%s tool=%s "
            "probes_in_window=%s denied_in_window=%s",
            self._session_id(ctx),
            tool_name,
            len(self._probe_at),
            len(self._denied_at),
        )
        ctx.request_force_finish({"output": output, "result_type": "error"})

    @staticmethod
    def _session_id(ctx: Any) -> str:
        session = getattr(ctx, "session", None)
        try:
            return str(session.get_session_id() or "") if session is not None else ""
        except Exception:  # noqa: BLE001
            return ""


__all__ = ["StalledAccessCircuitRail"]
