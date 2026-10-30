# coding: utf-8
"""Distributed templates must not freeze ignored_tool_calls (inherit from main)."""
from __future__ import annotations

from pathlib import Path

import yaml

_RESOURCES = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "resources"
_DISTRIBUTED_TEMPLATES = (
    "config.team.distributed.leader.yaml",
    "config.team.distributed.teammate.yaml",
)


def _contains_ignored_tool_calls(node) -> bool:
    if isinstance(node, dict):
        if "ignored_tool_calls" in node:
            return True
        return any(_contains_ignored_tool_calls(v) for v in node.values())
    if isinstance(node, list):
        return any(_contains_ignored_tool_calls(v) for v in node)
    return False


def test_distributed_templates_do_not_define_ignored_tool_calls():
    for name in _DISTRIBUTED_TEMPLATES:
        path = _RESOURCES / name
        assert path.is_file(), f"missing template: {path}"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert not _contains_ignored_tool_calls(data), (
            f"{name} must not define ignored_tool_calls "
            f"(inherit from main config.yaml to avoid freeze/drift)"
        )
