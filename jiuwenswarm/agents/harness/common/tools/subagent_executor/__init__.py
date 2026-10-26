"""Subagent executor package.

Provides context variables for subagent execution state passing.
"""

from __future__ import annotations

from .context_vars import (
    set_effective_request_workspace_dir,
    get_effective_request_workspace_dir,
    reset_effective_request_workspace_dir,
    set_effective_request_output_dir,
    get_effective_request_output_dir,
    reset_effective_request_output_dir,
    set_send_file_request_context,
    get_send_file_request_context,
    reset_send_file_request_context,
    set_subagent_parent_session,
    get_subagent_parent_session,
    reset_subagent_parent_session,
    set_interactive_ask,
    get_interactive_ask,
    reset_interactive_ask,
)

__all__ = [
    "set_effective_request_workspace_dir",
    "get_effective_request_workspace_dir",
    "reset_effective_request_workspace_dir",
    "set_effective_request_output_dir",
    "get_effective_request_output_dir",
    "reset_effective_request_output_dir",
    "set_send_file_request_context",
    "get_send_file_request_context",
    "reset_send_file_request_context",
    "set_subagent_parent_session",
    "get_subagent_parent_session",
    "reset_subagent_parent_session",
    "set_interactive_ask",
    "get_interactive_ask",
    "reset_interactive_ask",
]
