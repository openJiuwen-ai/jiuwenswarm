# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep the dependency-free Python host contracts covered by the normal UT CI."""

import subprocess
import sys
from pathlib import Path


def test_python_sdk_standalone_subprocess_contracts():
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-o",
            "asyncio_mode=auto",
            "-o",
            "log_cli=false",
            "sdks/python/tests",
            "-q",
            "--tb=short",
        ],
        cwd=root,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout.decode(
        "utf-8", errors="replace"
    ) + result.stderr.decode("utf-8", errors="replace")
