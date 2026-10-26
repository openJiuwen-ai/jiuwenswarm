# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression: DeepAdapter must wire RequestSummaryRail request boundaries."""

from __future__ import annotations

import ast
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_DEEP = _ROOT / "jiuwenswarm" / "server" / "runtime" / "agent_adapter" / "interface_deep.py"
_CODE = _ROOT / "jiuwenswarm" / "server" / "runtime" / "agent_adapter" / "interface_code.py"
_CONFIG_YAML = _ROOT / "jiuwenswarm" / "resources" / "config.yaml"


def test_interface_deep_wires_request_summary_rail() -> None:
    src = _DEEP.read_text(encoding="utf-8")
    required = (
        "from jiuwenswarm.perf.interface_hooks import",
        "set_perf_summary_context",
        "finalize_perf_summary_request",
        "clear_perf_summary_context",
        "mark_request_first_byte",
        "mark_request_first_answer",
        "maybe_mark_answer_first_byte",
        "snapshot_perf_summary_usage",
        "merge_perf_summary_usage_fallback",
        "record_deepresearch_sdk_token_usage",
        "_account_deepresearch_sdk_usage",
        "def _build_request_summary_rail",
        'RequestSummaryRail(record_only=True)',
        '_RailBuildInfo("_request_summary_rail"',
        "self._request_summary_rail",
        "def _mark_first_byte_once",
        "def _mark_first_answer_once",
        'perf_summary_status = "ok"',
        'perf_summary_status = "cancelled"',
        'perf_summary_status = "error"',
        'mode in ("team", "team.plan", "code.team")',
        'mode == "auto_harness"',
    )
    missing = [item for item in required if item not in src]
    assert not missing, f"interface_deep missing perf wiring: {missing}"


def test_rail_builders_keep_staticmethod() -> None:
    """Regression: inserting a builder must not steal @staticmethod from the next method."""
    tree = ast.parse(_DEEP.read_text(encoding="utf-8"))
    wanted = {
        "_build_request_summary_rail",
        "_build_multimodal_image_rail",
        "_build_task_execution_rail",
    }
    found: dict[str, bool] = {}

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node.name in wanted:
                found[node.name] = any(
                    isinstance(d, ast.Name) and d.id == "staticmethod"
                    for d in node.decorator_list
                )
            self.generic_visit(node)

    _Visitor().visit(tree)
    assert found == {name: True for name in wanted}


def test_interface_code_includes_request_summary_rail() -> None:
    src = _CODE.read_text(encoding="utf-8")
    assert '_RailBuildInfo("_request_summary_rail"' in src
    assert '"RequestSummaryRail"' in src


def test_perf_summary_enabled_by_default_in_resources_config() -> None:
    text = _CONFIG_YAML.read_text(encoding="utf-8")
    # Narrow to the perf.summary block rather than any other enabled: true.
    idx = text.find("perf:\n  summary:")
    assert idx >= 0
    block = text[idx : idx + 120]
    assert "enabled: true" in block
