"""Context variables for subagent execution.

Provides context isolation and state passing between parent agent and subagents.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

# Per-request workspace dir (same resolution as RuntimePromptRail: metadata
# effective_project_dir or adapter default). Used by fork/spawn so subagent
# prompts and Workspace match the main agent for the current request.
_effective_request_workspace_dir: ContextVar[Optional[str]] = ContextVar(
    "effective_request_workspace_dir", default=None
)

# Per-request output dir for agent file isolation (from metadata.output_dir).
# Used by send_file_to_user tool and agent to know where to save output files.
_effective_request_output_dir: ContextVar[Optional[str]] = ContextVar(
    "effective_request_output_dir", default=None
)

# Per-request send_file routing context (session_id / request_id / channel_id / metadata).
_send_file_request_context: ContextVar[Optional[dict]] = ContextVar(
    "send_file_request_context", default=None
)

# Context variable to pass parent session from tool execution to executor
_subagent_parent_session: ContextVar[Optional[object]] = ContextVar(
    "subagent_parent_session", default=None
)

# Whether interactive/guided mode is enabled for the current request.
# When False, ask_user calls with preview (e.g. outline review) are skipped
# instead of interrupting. Bound from request.params["interactive_ask"].
_interactive_ask: ContextVar[bool] = ContextVar(
    "interactive_ask", default=False
)


def set_effective_request_workspace_dir(workspace_dir: Optional[str]) -> Token:
    """Store workspace for the current request (aligned with RuntimePromptRail).

    Returns Token for caller to reset via :func:`reset_effective_request_workspace_dir`.
    """
    return _effective_request_workspace_dir.set(workspace_dir)


def get_effective_request_workspace_dir() -> Optional[str]:
    """Workspace dir for the current request, or None if not set."""
    return _effective_request_workspace_dir.get()


def reset_effective_request_workspace_dir(token: Token) -> None:
    """Restore the previous workspace dir binding."""
    _effective_request_workspace_dir.reset(token)


def set_effective_request_output_dir(output_dir: Optional[str]) -> Token:
    """Store output_dir for the current request (from metadata.output_dir).

    Returns Token for caller to reset via :func:`reset_effective_request_output_dir`.
    """
    return _effective_request_output_dir.set(output_dir)


def get_effective_request_output_dir() -> Optional[str]:
    """Output dir for the current request, or None if not set."""
    return _effective_request_output_dir.get()


def reset_effective_request_output_dir(token: Token) -> None:
    """Restore the previous output_dir binding."""
    _effective_request_output_dir.reset(token)


def set_send_file_request_context(
    *,
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Token:
    """Bind send_file routing context for the current async request.

    Returns Token for caller to reset via :func:`reset_send_file_request_context`.
    """
    ctx: dict = {}
    if request_id is not None:
        ctx["request_id"] = request_id
    if session_id is not None:
        ctx["session_id"] = session_id
    if channel_id is not None:
        ctx["channel_id"] = channel_id
    if metadata is not None:
        ctx["metadata"] = dict(metadata)
    return _send_file_request_context.set(ctx)


def get_send_file_request_context() -> Optional[dict]:
    """Return send_file routing context for the current request, or None if unset."""
    return _send_file_request_context.get()


def reset_send_file_request_context(token: Token) -> None:
    """Restore the previous send_file routing context binding."""
    _send_file_request_context.reset(token)


def set_subagent_parent_session(session: Optional[object]) -> Token:
    """Set the parent session context for subagent execution.

    Returns Token for caller to reset via :func:`reset_subagent_parent_session`.
    """
    return _subagent_parent_session.set(session)


def get_subagent_parent_session() -> Optional[object]:
    """Get the parent session from context."""
    return _subagent_parent_session.get()


def reset_subagent_parent_session(token: Token) -> None:
    """Restore the previous parent session binding."""
    _subagent_parent_session.reset(token)


def set_interactive_ask(enabled: bool) -> Token:
    """Set interactive/guided mode for the current request.

    Returns Token for caller to reset via :func:`reset_interactive_ask`.
    """
    return _interactive_ask.set(bool(enabled))


def get_interactive_ask() -> bool:
    """Whether interactive/guided mode is enabled (default False)."""
    return _interactive_ask.get()


def reset_interactive_ask(token: Token) -> None:
    """Restore the previous interactive_ask binding."""
    _interactive_ask.reset(token)
