"""运行时工具调用的 ContextVar 与 cron 工具上下文代理。

职责：
- 绑定/读取 session、channel、metadata、resource、A2A policy ContextVar
- ``_RuntimeCronToolContext``：任务级 cron 工具上下文（含 fallback）
- ``_CRON_TOOL_NAMES``：cron 相关工具名集合

进程级 ContextVar；adapter 负责 set/reset，本模块只定义与读取。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.gateway.cron import CronTargetChannel

_CRON_TOOL_CHANNEL_ID: ContextVar[str] = ContextVar(
    "cron_tool_channel_id",
    default=CronTargetChannel.WEB.value,
)
_CRON_TOOL_SESSION_ID: ContextVar[str | None] = ContextVar(
    "cron_tool_session_id",
    default=None,
)
_CRON_TOOL_METADATA: ContextVar[dict[str, Any] | None] = ContextVar(
    "cron_tool_metadata",
    default=None,
)
_CRON_TOOL_MODE: ContextVar[str | None] = ContextVar(
    "cron_tool_mode",
    default=None,
)
_CRON_TOOL_BOUND: ContextVar[bool] = ContextVar(
    "cron_tool_bound",
    default=False,
)
_RUNTIME_TOOL_RESOURCE_ID: ContextVar[str] = ContextVar(
    "runtime_tool_resource_id", default=""
)
_RUNTIME_TOOL_A2A_POLICY_ID: ContextVar[str] = ContextVar(
    "runtime_tool_a2a_policy_id", default=""
)


@dataclass
class _DeepResearchRouteContextToken:
    token: Token[dict[str, object] | None] | None


@dataclass
class _RuntimeCronContextTokens:
    channel: Token[str] | None
    session: Token[str | None] | None
    metadata: Token[dict[str, Any] | None] | None
    mode: Token[str | None] | None
    bound: Token[bool] | None
    shell: Token[str | None] | None
    deepresearch: _DeepResearchRouteContextToken | None
    send_file: Token | None = None
    resource_id: Token[str] | None = None
    a2a_policy_id: Token[str] | None = None


def get_runtime_tool_session_id() -> str | None:
    """Session id bound for the current agent tool invocation (ContextVar)."""
    return _CRON_TOOL_SESSION_ID.get()


def get_runtime_tool_channel_id() -> str:
    """Channel id bound for the current agent tool invocation (ContextVar)."""
    return _CRON_TOOL_CHANNEL_ID.get()


def get_runtime_tool_metadata() -> dict[str, Any] | None:
    """Request metadata bound for the current agent tool invocation (ContextVar)."""
    return _CRON_TOOL_METADATA.get()


def get_runtime_tool_resource_id() -> str:
    """Manager-owned resource id for the current tool invocation."""
    return _RUNTIME_TOOL_RESOURCE_ID.get()


def get_runtime_tool_a2a_policy_id() -> str:
    """A2A policy selected by the current enterprise Agent template."""
    return _RUNTIME_TOOL_A2A_POLICY_ID.get()

_CRON_TOOL_NAMES = frozenset(
    {
        "cron",
        "cron_list_jobs",
        "cron_get_job",
        "cron_create_job",
        "cron_update_job",
        "cron_delete_job",
        "cron_toggle_job",
        "cron_preview_job",
    }
)


class _RuntimeCronToolContext:
    """Stable cron tool context proxy backed by per-task contextvars."""

    def __init__(self, tool_scope: str) -> None:
        self._tool_scope = tool_scope
        self._fallback_channel_id = CronTargetChannel.WEB.value
        self._fallback_session_id: str | None = None
        self._fallback_metadata: dict[str, Any] | None = None
        self._fallback_mode: str | None = None

    def remember_current_binding(self) -> None:
        self._fallback_channel_id = _CRON_TOOL_CHANNEL_ID.get()
        self._fallback_session_id = _CRON_TOOL_SESSION_ID.get()
        metadata = _CRON_TOOL_METADATA.get()
        self._fallback_metadata = dict(metadata) if isinstance(metadata, dict) else None
        self._fallback_mode = _CRON_TOOL_MODE.get()

    @property
    def channel_id(self) -> str:
        if _CRON_TOOL_BOUND.get():
            return _CRON_TOOL_CHANNEL_ID.get()
        return self._fallback_channel_id

    @property
    def session_id(self) -> str | None:
        if _CRON_TOOL_BOUND.get():
            return _CRON_TOOL_SESSION_ID.get()
        return self._fallback_session_id

    @property
    def metadata(self) -> dict[str, Any] | None:
        if _CRON_TOOL_BOUND.get():
            metadata = _CRON_TOOL_METADATA.get()
            if isinstance(metadata, dict):
                return metadata
        return self._fallback_metadata

    @property
    def mode(self) -> str | None:
        if _CRON_TOOL_BOUND.get():
            return _CRON_TOOL_MODE.get()
        return self._fallback_mode

    @property
    def tool_scope(self) -> str:
        return self._tool_scope
