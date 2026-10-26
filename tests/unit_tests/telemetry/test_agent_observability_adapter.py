"""JiuwenSwarm integration contracts for agent-core observability.

The SDK tests own root-span lifecycle, fallback, and provider demand behavior.
These tests cover the call sites that remain in JiuwenSwarm.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _adapter_tree() -> ast.AST:
    source_path = (
        Path(__file__).parents[3]
        / "jiuwenswarm/server/runtime/agent_adapter/interface_deep.py"
    )
    return ast.parse(source_path.read_text(encoding="utf-8"))


def test_real_adapter_call_sites_match_core_run_span_signature() -> None:
    calls = [
        node
        for node in ast.walk(_adapter_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open_agent_run_span"
    ]

    assert len(calls) == 2
    for node_call in calls:
        keyword_names = {keyword.arg for keyword in node_call.keywords}
        assert keyword_names >= {"session_id", "request_id", "mode"}
        assert "channel_id" not in keyword_names
        mode_keyword = next(
            keyword for keyword in node_call.keywords if keyword.arg == "mode"
        )
        assert isinstance(mode_keyword.value, ast.Call)
        assert isinstance(mode_keyword.value.func, ast.Name)
        assert mode_keyword.value.func.id == "_resolve_observability_mode"


def test_adapter_imports_run_span_from_sdk() -> None:
    imports = [
        node
        for node in ast.walk(_adapter_tree())
        if isinstance(node, ast.ImportFrom)
        and node.module == "openjiuwen.harness.observability"
    ]
    imported_names = {alias.name for node in imports for alias in node.names}
    assert {"open_agent_run_span", "close_agent_run_span"} <= imported_names
