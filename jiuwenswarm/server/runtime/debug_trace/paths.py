# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Debug trace directory / file resolution.

All paths derive from ``jiuwenswarm.common.utils.get_user_workspace_dir``
(respects ``JIUWENSWARM_DATA_DIR``, falls back to ``~/.jiuwenswarm``).
Mode-prefixed dirs keep Agent and Code dumps separate:

    ~/.jiuwenswarm/.agent/traces/dump-agent-<session_id>.txt
    ~/.jiuwenswarm/.code/traces/dump-code-<session_id>.txt
"""

from __future__ import annotations

import re
from pathlib import Path

from jiuwenswarm.common.utils import get_user_workspace_dir


def _safe_segment(value: str, fallback: str = "_") -> str:
    """Sanitize an untrusted string into a single safe path segment.

    Mirrors ``openjiuwen.agent_teams.paths._safe_segment``: replaces every
    character outside ``[A-Za-z0-9_.-]`` with ``_`` and strips leading/
    trailing separators so the result can never escape its parent dir.
    """
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    normalized = normalized.strip("._-")
    return normalized[:96] or fallback


def debug_trace_dir(mode: str) -> Path:
    """Return the trace directory for *mode* (``.agent`` or ``.code``)."""
    root = get_user_workspace_dir()
    kind = ".code" if (mode or "").startswith("code") else ".agent"
    return root / kind / "traces"


def debug_trace_file(mode: str, session_id: str) -> Path:
    """Return the per-session dump file path for *mode*."""
    kind = "code" if (mode or "").startswith("code") else "agent"
    return debug_trace_dir(mode) / f"dump-{kind}-{_safe_segment(session_id)}.txt"


def resolve_debug_trace_mode(runtime_mode: str, original_mode: str | None = None) -> str:
    """Return the mode used to select a request's local dump path.

    Web code-profile Plan requests arrive as ``agent.plan`` and are later
    canonicalized to ``code.plan`` for Adapter selection. Preserve the explicit
    Agent Plan wire mode for storage classification without changing ordinary
    ``agent`` + ``work_mode=code`` requests, which remain Code dumps.
    """
    requested = str(original_mode or "").strip().lower()
    return "agent.plan" if requested == "agent.plan" else runtime_mode


__all__ = ["debug_trace_dir", "debug_trace_file", "resolve_debug_trace_mode"]
