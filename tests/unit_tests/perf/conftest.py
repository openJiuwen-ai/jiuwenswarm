# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import replace

import pytest

from jiuwenswarm.perf.config import get_perf_summary_config, init_perf_summary_config


@pytest.fixture(autouse=True)
def _enable_perf_summary_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests assume perf summary collection is enabled unless they override config."""
    init_perf_summary_config()
    enabled_cfg = replace(get_perf_summary_config(), enabled=True)
    for target in (
        "jiuwenswarm.perf.config.get_perf_summary_config",
        "jiuwenswarm.perf.collector.get_perf_summary_config",
        "jiuwenswarm.perf.task_hooks.get_perf_summary_config",
        "jiuwenswarm.perf.todo_tracker.get_perf_summary_config",
        "jiuwenswarm.perf.interface_hooks.get_perf_summary_config",
        "jiuwenswarm.perf.subagent_hooks.get_perf_summary_config",
        "jiuwenswarm.perf.request_summary_rail.get_perf_summary_config",
    ):
        try:
            monkeypatch.setattr(target, lambda _cfg=enabled_cfg: _cfg)
        except Exception:
            # Optional modules may pull heavy agent-core deps unavailable in CI envs.
            continue
