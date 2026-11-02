# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression: DeepAdapter must wire RequestSummaryRail request boundaries."""

from __future__ import annotations

import ast
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_DEEP = _ROOT / "jiuwenswarm" / "server" / "runtime" / "agent_adapter" / "interface_deep.py"
_ADAPTER_STATICS = (
    _ROOT
    / "jiuwenswarm"
    / "server"
    / "runtime"
    / "agent_adapter"
    / "deep"
    / "adapter_statics.py"
)
_CODE = _ROOT / "jiuwenswarm" / "server" / "runtime" / "agent_adapter" / "interface_code.py"
_CONFIG_YAML = _ROOT / "jiuwenswarm" / "resources" / "config.yaml"


def test_interface_deep_wires_request_summary_rail() -> None:
    deep_src = _DEEP.read_text(encoding="utf-8")
    statics_src = _ADAPTER_STATICS.read_text(encoding="utf-8")
    combined = deep_src + "\n" + statics_src
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
        "RequestSummaryRail(record_only=True)",
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
    missing = [item for item in required if item not in combined]
    assert not missing, f"interface_deep/adapter_statics missing perf wiring: {missing}"


def test_rail_builders_keep_staticmethod() -> None:
    """类上 builder 以 ``_foo = staticmethod(_foo)`` 挂回；定义在 deep 子模块。"""
    tree = ast.parse(_DEEP.read_text(encoding="utf-8"))
    remounted = False
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == "_build_request_summary_rail"
                for t in item.targets
            ):
                continue
            value = item.value
            remounted = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "staticmethod"
            )
            break
        if remounted:
            break
    assert remounted, "expected class remount: _build_request_summary_rail = staticmethod(...)"

    moved = {
        "_build_request_summary_rail": _ADAPTER_STATICS,
        "_build_multimodal_image_rail": _ROOT
        / "jiuwenswarm/server/runtime/agent_adapter/deep/model_factory.py",
        "_build_task_execution_rail": _ROOT
        / "jiuwenswarm/server/runtime/agent_adapter/deep/rail_factory.py",
    }
    for name, path in moved.items():
        moved_tree = ast.parse(path.read_text(encoding="utf-8"))
        assert any(
            isinstance(node, ast.FunctionDef) and node.name == name
            for node in moved_tree.body
        )


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
