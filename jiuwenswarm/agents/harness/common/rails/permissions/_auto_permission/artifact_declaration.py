"""Exact send-file argument helpers."""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    ToolDecisionFacts,
)


def _requested_file_paths(facts: ToolDecisionFacts) -> tuple[Path, ...]:
    return tuple(
        Path(path).expanduser().resolve(strict=False) for path in facts.read_paths
    )
