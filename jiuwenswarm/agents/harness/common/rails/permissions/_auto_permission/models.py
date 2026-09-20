"""Migrated Auto Permission models slice."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.single_agent.interrupt.response import InterruptRequest


logger = logging.getLogger(__name__)


_REVIEWER_ACTION_TARGET_MAX_LENGTH = 160




_REVIEWER_UI_TEXT_MAX_LENGTH = 1024


_REVIEWER_UI_SHORT_TEXT_MAX_LENGTH = 240


_REVIEWER_DENIAL_GUIDANCE = (
    "Do not retry, rephrase, split, or switch tools to bypass this denial. "
    "A materially changed user intent requires a new tool call and full review."
)


_SHELL_DISPLAY_PATH_PATTERN = re.compile(
    r"(?:"
    r"(?:/Users/|/private/|/tmp/)[^\s\"']+"
    r"|/home/[^/\s\"']+/[^\s\"']+"
    r"|~(?:[/\\][^\s\"']+)+"
    r"|[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]"
    r"[^'\";|&<>`,，。；、\r\n]+"
    r")"
)


_SHELL_DISPLAY_PRE_SPLIT_PATH_PATTERN = re.compile(
    r"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]"
    r"[^'\";|&<>`,，。；、\r\n]+"
)


_SENSITIVE_DISPLAY_PATH_PATTERN = re.compile(
    r"(?i)(?:"
    r"(^|[/\\])(?:\.env(?:\.[^/\\]+)?|credentials?|id_rsa|id_dsa|id_ecdsa|"
    r"id_ed25519|known_hosts)$"
    r"|(^|[/\\])(?:\.ssh|\.aws|\.gnupg|\.jiuwenswarm[/\\]config)(?:[/\\]|$)"
    r"|(?:secret|token|api[_-]?key|private[_-]?key|credential|password)"
    r")"
)


PROHIBITED_FILE_DELIVERY_REASON = "file_delivery_prohibited_by_user"


DETERMINISTIC_BOUNDED_SCOPE_DECISION_SOURCE = "deterministic_bounded_scope"


DETERMINISTIC_READONLY_PUBLIC_WEB_REASON = "deterministic_readonly_public_web"


DETERMINISTIC_READONLY_PUBLIC_WEB_FETCH_SOURCE_KINDS = frozenset(
    {"recent_search_result"}
)


@dataclass(frozen=True)
class ToolInvocation:
    """Normalized tool invocation extracted from rail callback inputs."""

    ctx: Any | None
    tool_call: Any | None
    tool_name: str
    tool_args: Any


@dataclass(frozen=True)
class PermissionHandlingResult:
    """Handled result plus the trusted host source of that decision."""

    handled: bool
    result: Any | None
    decision_source: str

    def __post_init__(self) -> None:
        if self.handled and not self.decision_source:
            raise ValueError("handled permission result requires a decision source")


class PermissionInterruptRequest(InterruptRequest):
    """Interrupt request that preserves permission metadata for UI rendering."""

    model_config = {"extra": "allow"}
