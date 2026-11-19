# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retired sink cleanup must coexist with the real logging configuration APIs."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("configuration", ["dict", "file", "basic", "shutdown"])
def test_retired_file_sink_does_not_deadlock_logging_configuration(tmp_path, configuration):
    source = Path(__file__).resolve().parents[3]
    fixture = Path(__file__).parent / "fixtures" / "logging_reconfigure_process.py"
    paths = [str(source)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    result = subprocess.run(
        [sys.executable, str(fixture), str(tmp_path), configuration],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(paths)},
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "reconfiguration completed" in result.stdout
    assert (tmp_path / "before.log").read_text(encoding="utf-8") == "before configuration\n"
    assert (tmp_path / "after.log").read_text(encoding="utf-8") == "after configuration\n"
