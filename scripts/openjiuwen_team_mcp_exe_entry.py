# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""PyInstaller entry point for the bundled OpenJiuWen team MCP server."""

from __future__ import annotations

import atexit
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import TextIO


_REBOUND_STDIO_STACK = ExitStack()
atexit.register(_REBOUND_STDIO_STACK.close)


def _register_rebound_stdio_stream(stream: TextIO) -> TextIO:
    """Keep the rebound stdio stream alive until process shutdown."""
    return _REBOUND_STDIO_STACK.enter_context(stream)


def _open_rebound_stdio_fd(fd: int, mode: str) -> TextIO:
    """Open an existing stdio file descriptor as a text stream."""
    return os.fdopen(
        fd,
        mode,
        encoding="utf-8",
        errors="replace",
        buffering=1,
    )


def _open_rebound_devnull(fallback_mode: str) -> TextIO:
    """Open the null device as a text stream for missing stdio."""
    return Path(os.devnull).open(
        fallback_mode,
        encoding="utf-8",
        errors="replace",
    )


def _ensure_stdio() -> None:
    """Rebind stdio streams for windowed frozen helper processes."""
    streams = (
        ("stdin", 0, "r"),
        ("stdout", 1, "w"),
        ("stderr", 2, "w"),
    )
    for attr_name, fd, mode in streams:
        stream = getattr(sys, attr_name)
        if stream is not None and not getattr(stream, "closed", False):
            continue
        try:
            rebound = _register_rebound_stdio_stream(_open_rebound_stdio_fd(fd, mode))
        except Exception as exc:  # noqa: BLE001
            if attr_name in ("stdin", "stdout"):
                raise RuntimeError(
                    f"team MCP stdio requires {attr_name}; failed to bind fd {fd}",
                ) from exc
            fallback_mode = "r" if "r" in mode else "w"
            rebound = _register_rebound_stdio_stream(_open_rebound_devnull(fallback_mode))
        setattr(sys, attr_name, rebound)


def main() -> None:
    """Run the OpenJiuWen team MCP server over stdio."""
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    if getattr(sys, "frozen", False):
        from jiuwenswarm.common.external_cli_runtime import activate_external_cli_runtime_paths

        activate_external_cli_runtime_paths()
        _ensure_stdio()

    from openjiuwen.agent_teams.mcp.server import main as server_main

    server_main()


if __name__ == "__main__":
    main()
