# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Execute debug Makefile recipes with an isolated command recorder."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"
pytestmark = pytest.mark.skipif(os.name == "nt" or shutil.which("make") is None, reason="POSIX Makefile recipes")


@pytest.mark.parametrize("target", ["stop", "restart-debug", "restart-debug-fast"])
@pytest.mark.parametrize("stop_failure", [False, True])
def test_debug_recipes_forward_options_and_do_not_restart_after_stop_failure(
    tmp_path: Path,
    target: str,
    stop_failure: bool,
) -> None:
    """Run actual make control flow without launching services or building the frontend."""
    calls_path = tmp_path / "calls.jsonl"
    recorder = tmp_path / "record.py"
    recorder.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "with Path(os.environ['JW_MAKE_CALLS']).open('a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['run', 'jiuwenswarm-stop']:\n"
        "    raise SystemExit(int(os.environ['JW_MAKE_STOP_EXIT']))\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({"JW_MAKE_CALLS": str(calls_path), "JW_MAKE_STOP_EXIT": "1" if stop_failure else "0"})
    uv = f"{shlex.quote(sys.executable)} {shlex.quote(str(recorder))}"
    completed = subprocess.run(
        [
            "make", "--no-print-directory", "-f", str(MAKEFILE), target, f"UV={uv}",
            "STOP_ARGS=--legacy-worker-pid 7777 --worker-timeout 45",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    expected = [["run", "jiuwenswarm-stop", "--legacy-worker-pid", "7777", "--worker-timeout", "45"]]
    if not stop_failure and target != "stop":
        expected.append(["run", "jiuwenswarm-start", "debug"])
        if target == "restart-debug-fast":
            expected[-1].append("--skip-build")
    assert calls == expected, completed.stdout + completed.stderr
    assert (completed.returncode != 0) is stop_failure
