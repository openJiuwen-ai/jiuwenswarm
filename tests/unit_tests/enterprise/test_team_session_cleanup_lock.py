# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The startup sweep lock remains exclusive across actual processes."""

import subprocess
import sys
from pathlib import Path

import pytest

from jiuwenswarm.enterprise.team.session_cleanup import _SWEEP_LOCK_NAME, _sweep_lock


def _probe_lock(volume: Path) -> str:
    script = """
import sys
import portalocker
try:
    with portalocker.Lock(
        sys.argv[1], mode="a+", timeout=0,
        flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
    ):
        print("acquired")
except portalocker.exceptions.LockException:
    print("blocked")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(volume / _SWEEP_LOCK_NAME)],
        check=True, capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip()


def test_startup_sweep_lock_serializes_other_processes(tmp_path):
    with _sweep_lock(tmp_path):
        assert _probe_lock(tmp_path) == "blocked"
    assert _probe_lock(tmp_path) == "acquired"


def test_failed_startup_sweep_releases_the_process_lock(tmp_path):
    with pytest.raises(RuntimeError, match="sweep failed"):
        with _sweep_lock(tmp_path):
            raise RuntimeError("sweep failed")
    assert _probe_lock(tmp_path) == "acquired"
