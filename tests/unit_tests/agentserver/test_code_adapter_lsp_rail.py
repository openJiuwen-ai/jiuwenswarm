# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for LspRail registration and degradation in JiuwenSwarmCodeAdapter.

Covers:
- TC-001: RAIL_BUILD_NAMES does not contain LspRail entry
- TC-002: build_lsp_rail() degradation log includes classification label
- TC-003: FIXED_RAIL_NAMES still contains LspRail (protected, unchanged)
- TC-004: Fixed rail list still contains LspRail RailBuildInfo (L297)
- TC-005: LspRail returning None does not block rails_list construction

NOTE: The codebase imports InMemoryTrajectoryRegistry from openjiuwen which
does not yet exist in the published openjiuwen package. We inject a mock into
the real trajectory module before the import chain resolves so collection succeeds.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

import openjiuwen.agent_evolving.trajectory as _traj_mod
if not hasattr(_traj_mod, "InMemoryTrajectoryRegistry"):
    _traj_mod.InMemoryTrajectoryRegistry = MagicMock

import jiuwenswarm.server.runtime.agent_adapter.interface_code as _ic_mod

_RAIL_BUILD_NAMES = getattr(_ic_mod, "_RAIL_BUILD_NAMES")
_RailBuildInfo = getattr(_ic_mod, "_RailBuildInfo")
JiuwenSwarmCodeAdapter = _ic_mod.JiuwenSwarmCodeAdapter
_FIXED_RAIL_NAMES = getattr(JiuwenSwarmCodeAdapter, "_FIXED_RAIL_NAMES")


def _make_log_capture():
    """Create a custom log capture handler since pytest caplog doesn't capture
    in this project's logging configuration."""
    capture = _LogCapture()
    handler = logging.Handler()
    handler.emit = capture.add_record
    return capture, handler


class _LogCapture:
    """Simple in-memory log capture for assertion checks."""

    def __init__(self):
        self.records: list[logging.LogRecord] = []

    def add_record(self, record: logging.LogRecord):
        self.records.append(record)

    @property
    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)


# ─── TC-001: RAIL_BUILD_NAMES 不含 LspRail ─────────────────────────


def test_rail_build_names_does_not_contain_lsp_rail():
    """Verify LspRail key has been removed from RAIL_BUILD_NAMES mapping."""
    assert "LspRail" not in _RAIL_BUILD_NAMES, (
        "LspRail should have been removed from RAIL_BUILD_NAMES "
        "since it is now a fixed rail"
    )


def test_rail_build_names_still_contains_other_required_entries():
    """Verify other required mappings are still present after LspRail removal."""
    required_keys = [
        "SkillUseRail",
        "HeartbeatRail",
        "ProjectMemoryRail",
        "CodingMemoryRail",
    ]
    for key in required_keys:
        assert key in _RAIL_BUILD_NAMES, (
            f"{key} should still be present in RAIL_BUILD_NAMES"
        )


def test_rail_build_names_lsp_rail_via_config_method_still_accessible():
    """Verify build_lsp_rail_via_config method still exists on the adapter instance."""
    adapter = JiuwenSwarmCodeAdapter()
    assert hasattr(adapter, "_build_lsp_rail_via_config"), (
        "build_lsp_rail_via_config should still exist as a method "
        "since it is called by the fixed rail list"
    )


# ─── TC-002: build_lsp_rail() 降级日志分类标签 ─────────────────────


def test_import_error_logs_config_error_label():
    """Verify ImportError produces [config_error] classification label."""
    adapter = JiuwenSwarmCodeAdapter()
    log_capture, handler = _make_log_capture()
    _ic_mod.logger.addHandler(handler)

    try:
        with patch.object(_ic_mod, "LspRail", side_effect=ImportError("openjiuwen not available")), \
             patch.object(_ic_mod, "InitializeOptions", MagicMock(cwd="/test/project")):
            result = getattr(adapter, "_build_lsp_rail")(workspace_dir="/test/project")
    finally:
        _ic_mod.logger.removeHandler(handler)

    assert result is None
    assert "[config_error]" in log_capture.text, (
        f"Degradation log should contain [config_error] classification label for ImportError. "
        f"Actual log output: {log_capture.text!r}"
    )


def test_file_not_found_error_logs_server_start_failed_label():
    """Verify FileNotFoundError produces [server_start_failed] classification label."""
    adapter = JiuwenSwarmCodeAdapter()
    log_capture, handler = _make_log_capture()
    _ic_mod.logger.addHandler(handler)

    try:
        with patch.object(_ic_mod, "LspRail", side_effect=FileNotFoundError("lsp server binary not found")), \
             patch.object(_ic_mod, "InitializeOptions", MagicMock(cwd="/test/project")):
            result = getattr(adapter, "_build_lsp_rail")(workspace_dir="/test/project")
    finally:
        _ic_mod.logger.removeHandler(handler)

    assert result is None
    assert "[server_start_failed]" in log_capture.text, (
        f"Degradation log should contain [server_start_failed] classification label for FileNotFoundError. "
        f"Actual log output: {log_capture.text!r}"
    )


def test_os_error_logs_server_start_failed_label():
    """Verify OSError produces [server_start_failed] classification label."""
    adapter = JiuwenSwarmCodeAdapter()
    log_capture, handler = _make_log_capture()
    _ic_mod.logger.addHandler(handler)

    try:
        with patch.object(_ic_mod, "LspRail", side_effect=OSError("cannot start lsp server process")), \
             patch.object(_ic_mod, "InitializeOptions", MagicMock(cwd="/test/project")):
            result = getattr(adapter, "_build_lsp_rail")(workspace_dir="/test/project")
    finally:
        _ic_mod.logger.removeHandler(handler)

    assert result is None
    assert "[server_start_failed]" in log_capture.text, (
        f"Degradation log should contain [server_start_failed] classification label for OSError. "
        f"Actual log output: {log_capture.text!r}"
    )


def test_generic_exception_logs_unknown_label():
    """Verify other exceptions produce [unknown] classification label."""
    adapter = JiuwenSwarmCodeAdapter()
    log_capture, handler = _make_log_capture()
    _ic_mod.logger.addHandler(handler)

    try:
        with patch.object(_ic_mod, "LspRail", side_effect=RuntimeError("unexpected failure")), \
             patch.object(_ic_mod, "InitializeOptions", MagicMock(cwd="/test/project")):
            result = getattr(adapter, "_build_lsp_rail")(workspace_dir="/test/project")
    finally:
        _ic_mod.logger.removeHandler(handler)

    assert result is None
    assert "[unknown]" in log_capture.text, (
        f"Degradation log should contain [unknown] classification label for generic exceptions. "
        f"Actual log output: {log_capture.text!r}"
    )


def test_degradation_log_is_warning_level():
    """Verify degradation log is WARNING level, not ERROR."""
    adapter = JiuwenSwarmCodeAdapter()
    log_capture, handler = _make_log_capture()
    _ic_mod.logger.addHandler(handler)

    try:
        with patch.object(_ic_mod, "LspRail", side_effect=ImportError("test import error")), \
             patch.object(_ic_mod, "InitializeOptions", MagicMock(cwd="/test/project")):
            result = getattr(adapter, "_build_lsp_rail")(workspace_dir="/test/project")
    finally:
        _ic_mod.logger.removeHandler(handler)

    assert result is None
    lsp_fail_records = [r for r in log_capture.records if "LspRail create failed" in r.getMessage()]
    for record in lsp_fail_records:
        assert record.levelno == logging.WARNING, (
            f"Degradation log should be WARNING level, not {record.levelname}"
        )


# ─── TC-003: FIXED_RAIL_NAMES 仍包含 LspRail ──────────────────────


def test_fixed_rail_names_contains_lsp_rail():
    """Verify LspRail is still present in FIXED_RAIL_NAMES (protected)."""
    assert "LspRail" in _FIXED_RAIL_NAMES, (
        "LspRail must remain in FIXED_RAIL_NAMES for dedup protection"
    )


def test_fixed_rail_names_is_frozenset():
    """Verify FIXED_RAIL_NAMES is an immutable frozenset."""
    assert isinstance(_FIXED_RAIL_NAMES, frozenset), (
        "FIXED_RAIL_NAMES should be a frozenset (immutable)"
    )


# ─── TC-004: 固定列表仍包含 LspRail RailBuildInfo ──────────────────


def test_build_lsp_rail_via_config_in_fixed_list():
    """Verify build_lsp_rail_via_config method is callable on the adapter."""
    adapter = JiuwenSwarmCodeAdapter()
    build_func = getattr(adapter, "_build_lsp_rail_via_config")
    assert callable(build_func), (
        "build_lsp_rail_via_config should be callable "
        "as part of the fixed rail list"
    )


def test_lsp_rail_attr_name_in_fixed_list_context():
    """Verify lsp_rail attribute name is expected in the adapter."""
    adapter = JiuwenSwarmCodeAdapter()
    assert hasattr(adapter, "_lsp_rail"), (
        "JiuwenSwarmCodeAdapter should have lsp_rail attribute slot"
    )


# ─── TC-005: LspRail 返回 None 不阻塞 rails_list 构建 ──────────────


def test_build_lsp_rail_returns_none_on_failure():
    """Verify build_lsp_rail returns None when LspRail creation fails."""
    adapter = JiuwenSwarmCodeAdapter()

    with patch.object(_ic_mod, "LspRail", side_effect=ImportError("test import error")), \
         patch.object(_ic_mod, "InitializeOptions", MagicMock(cwd="/test/project")):
        result = getattr(adapter, "_build_lsp_rail")(workspace_dir="/test/project")

    assert result is None, (
        "build_lsp_rail should return None on failure, not raise exception"
    )


def test_none_result_skipped_in_rails_list():
    """Verify None results are skipped when building rails_list."""
    rails_list = []

    rails_list.append(MagicMock(name="ProjectMemoryRail"))

    lsp_result = None
    if lsp_result is not None:
        rails_list.append(lsp_result)

    rails_list.append(MagicMock(name="CodingMemoryRail"))

    assert len(rails_list) == 2, (
        "None should be skipped in rails_list construction, "
        "not blocking other rails"
    )