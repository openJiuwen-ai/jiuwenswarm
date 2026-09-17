# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Low-level terminal output primitives for CLI chat.

These are NOT log messages — they implement terminal UI (spinner animation,
streaming content to stdout, structured JSON/JSONL output).  Using ``os.write``
rather than ``sys.*.write`` or ``print`` because G.LOG.02 requires that
application-level log messages go through the ``logging`` module, but terminal
UI rendering is not logging.
"""

from __future__ import annotations

import os
import sys

# ── Ensure Windows console is UTF-8 capable ──────────────────────────
# os.write(fd, text.encode("utf-8")) outputs raw UTF-8 bytes to the console
# fd. On Chinese Windows the default code page is 936 (GBK), which would
# mangle Unicode spinner glyphs (✢✳✶✻✽·). Set the console to UTF-8 (65001)
# and configure Python I/O encoding before any output is emitted.
if os.name == "nt":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONLEGACYWINDOWSSTDIO", "utf-8")
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except OSError:
        pass
    # Reconfigure stdio streams to use UTF-8 (they may have been created
    # before the env vars above took effect).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

STDOUT_FD = 1
STDERR_FD = 2


def write_stdout(text: str) -> None:
    os.write(STDOUT_FD, text.encode("utf-8"))


def write_stderr(text: str) -> None:
    os.write(STDERR_FD, text.encode("utf-8"))
