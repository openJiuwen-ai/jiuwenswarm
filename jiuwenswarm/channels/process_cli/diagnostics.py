# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep one-shot CLI diagnostics separate from user-visible output."""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import traceback
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


@dataclass(frozen=True)
class VisibleStreams:
    stdout: TextIO
    stderr: TextIO
    log_path: Path | None


def _diagnostic_path() -> Path:
    data_dir = os.getenv("JIUWENSWARM_DATA_DIR")
    if data_dir:
        root = Path(data_dir).expanduser()
    else:
        home = Path(os.getenv("JIUWENSWARM_HOME") or Path.home()).expanduser()
        root = home / ".jiuwenswarm"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}.log"
    # Do not create agent/.logs before common.utils migrates a legacy .logs.
    return root.resolve() / "agent" / "process_cli_logs" / name


def _fileno(stream: TextIO) -> int | None:
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None


def _report_log_setup_failure(error: OSError, stream: TextIO) -> None:
    """Keep the setup failure visible without relying on global log handlers."""
    logger = logging.Logger(f"{__name__}.fallback", level=logging.ERROR)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        logger.error("jiuwenswarm-process: 无法写入诊断日志：%s", error)
    finally:
        handler.close()


@contextlib.contextmanager
def capture_diagnostics(*, capture_stdout: bool, debug: bool) -> Iterator[VisibleStreams]:
    """Capture Python and inherited native diagnostics for one CLI invocation.

    The renderer writes through duplicate terminal handles. Runtime logging,
    tracebacks, and subprocesses inherit the redirected standard handles.
    In-memory test streams cannot be redirected at the descriptor level.
    """
    if debug or os.getenv("JIUWENSWARM_PROCESS_DEBUG") == "1":
        yield VisibleStreams(sys.stdout, sys.stderr, None)
        return

    stdout_fd = _fileno(sys.stdout) if capture_stdout else None
    stderr_fd = _fileno(sys.stderr)
    if stderr_fd is None or (capture_stdout and stdout_fd is None):
        yield VisibleStreams(sys.stdout, sys.stderr, None)
        return

    log_path = _diagnostic_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(mode=0o600, exist_ok=False)
    except OSError as error:
        _report_log_setup_failure(error, sys.stderr)
        yield VisibleStreams(sys.stdout, sys.stderr, None)
        return

    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        visible_stdout = sys.stdout
        visible_stderr = sys.stderr
        # Windows console streams use Win32 handles beyond the CRT file
        # descriptors. Replacing descriptor 1/2 invalidates those handles.
        # Python logging is the noisy path in a terminal; keep the console
        # handles intact while routing Python diagnostics to the file.
        if os.name == "nt" and (sys.stdout.isatty() or sys.stderr.isatty()):
            with contextlib.redirect_stderr(log_file):
                with (
                    contextlib.redirect_stdout(log_file)
                    if capture_stdout
                    else contextlib.nullcontext()
                ):
                    try:
                        yield VisibleStreams(visible_stdout, visible_stderr, log_path)
                    except Exception:  # noqa: BLE001 - preserve CLI traceback
                        traceback.print_exc(file=log_file)
                        raise
            return

        saved_stdout: TextIO | None = None
        saved_stderr: TextIO | None = None
        stdout_redirected = False
        stderr_redirected = False
        try:
            if capture_stdout and stdout_fd is not None:
                saved_stdout = os.fdopen(
                    os.dup(stdout_fd), "w", encoding="utf-8", errors="replace"
                )
                visible_stdout = saved_stdout
            saved_stderr = os.fdopen(
                os.dup(stderr_fd), "w", encoding="utf-8", errors="replace"
            )
            visible_stderr = saved_stderr
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(log_file.fileno(), stderr_fd)
            stderr_redirected = True
            if capture_stdout and stdout_fd is not None:
                os.dup2(log_file.fileno(), stdout_fd)
                stdout_redirected = True
            try:
                yield VisibleStreams(visible_stdout, visible_stderr, log_path)
            except Exception:  # noqa: BLE001 - preserve diagnostics at CLI boundary
                traceback.print_exc(file=sys.stderr)
                raise
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            if stdout_redirected and saved_stdout is not None and stdout_fd is not None:
                os.dup2(saved_stdout.fileno(), stdout_fd)
            if stderr_redirected and saved_stderr is not None:
                os.dup2(saved_stderr.fileno(), stderr_fd)
            if saved_stdout is not None:
                saved_stdout.close()
            if saved_stderr is not None:
                saved_stderr.close()
