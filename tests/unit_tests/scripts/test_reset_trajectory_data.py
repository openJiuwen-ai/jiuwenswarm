# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trace directories the trajectory reset script cleans."""

from __future__ import annotations

from pathlib import Path

from scripts import reset_trajectory_data


def test_trace_dirs_cover_unified_section(tmp_path: Path) -> None:
    config = {"observability": {"traces_dir": "custom"}}

    assert reset_trajectory_data._trace_dirs(config, tmp_path) == [
        tmp_path / ".trace",
        tmp_path / "custom",
    ]


def test_trace_dirs_keep_both_legacy_directories(tmp_path: Path) -> None:
    """Two legacy sections may have written traces to two directories."""
    config = {
        "team_observability": {"enabled": True, "traces_dir": "team-traces"},
        "agent_observability": {"enabled": True, "traces_dir": "agent-traces"},
    }

    directories = reset_trajectory_data._trace_dirs(config, tmp_path)

    assert tmp_path / "team-traces" in directories
    assert tmp_path / "agent-traces" in directories
    assert len(directories) == len(set(directories))
