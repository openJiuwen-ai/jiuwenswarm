"""Installation-time setup for Xiaoyi memory state and workspace files."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _touch_missing(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def initialize_celia_workspace(agent_workspace: Path) -> None:
    """Prepare memory UI files without overwriting existing user contents."""
    runtime_root = Path.home() / ".openclaw"
    _touch_missing(agent_workspace / "USER.md")
    _touch_missing(agent_workspace / "MEMORY.md")
    _touch_missing(runtime_root / ".memory.log")

    from jiuwenswarm.agents.harness.common.memory.celia.runtime_state import ensure_runtime_state

    ensure_runtime_state()
    logger.info("Xiaoyi memory state prepared at %s", runtime_root)
