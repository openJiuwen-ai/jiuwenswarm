# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Own and reap an abruptly orphaned Worker in Linux container tests.

The real Front is a child of this isolated subreaper. Killing that Front
adopts its Worker here, so the test does not depend on container PID 1
reaping zombies. No subreaper state is changed in the pytest process.
"""

import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import psutil

_PR_SET_CHILD_SUBREAPER = 36


def _enable_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not enable the test child subreaper")


def _kill_descendants() -> None:
    for process in psutil.Process().children(recursive=True):
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass


def _reap_children() -> dict[str, int]:
    exits = {}
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            return exits
        exits[str(pid)] = os.waitstatus_to_exitcode(status)


def _terminate(_signum: int, _frame: object) -> None:
    raise SystemExit(1)


def main() -> None:
    _enable_subreaper()
    signal.signal(signal.SIGTERM, _terminate)
    output = Path(sys.argv[1])
    script = Path(__file__).with_name("loopback_front.py")
    try:
        front = subprocess.Popen([sys.executable, str(script), str(output)], env=os.environ.copy())
        front.wait()
        # Normal execution never signals the Worker: its exit must follow
        # IPC EOF from the real Front, with a successful exit status.
        exits = _reap_children()
        output.with_suffix(".reaped.json").write_text(json.dumps(exits), encoding="utf-8")
    finally:
        _kill_descendants()
        _reap_children()


if __name__ == "__main__":
    main()
