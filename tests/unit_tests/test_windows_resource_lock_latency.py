# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A real competing writer must not incur msvcrt's one-second retry interval."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.session.project_store import file_lock


@pytest.mark.skipif(sys.platform != "win32", reason="Windows locking retry regression")
def test_brief_cross_process_contention_does_not_add_a_second(tmp_path):
    target = tmp_path / "metadata.json"
    script = '''
import sys, time
from pathlib import Path
from jiuwenswarm.server.runtime.session.project_store import file_lock
print("ready", flush=True)
started = time.perf_counter()
with file_lock(Path(sys.argv[1])):
    print(time.perf_counter() - started, flush=True)
'''
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root)}
    with file_lock(target):
        child = subprocess.Popen([sys.executable, "-c", script, str(target)],
                                 cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "ready"
            time.sleep(.03)
        except BaseException:
            child.kill()
            child.communicate(timeout=5)
            raise
    output, error = child.communicate(timeout=5)
    assert child.returncode == 0, error
    assert float(output.strip()) < .5
