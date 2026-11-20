"""Run system plugin tests with the Python environment supplied by the host."""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO


def _emit_error(stream: TextIO, message: str) -> None:
    """Write a user-facing error without relying on host logger setup."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord(__name__, logging.ERROR, __file__, 0, message, (), None)
    try:
        handler.emit(record)
    finally:
        handler.close()


def main(argv: list[str] | None = None) -> int:
    """Run pytest without preparing or changing the host environment."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        _emit_error(sys.stderr, "请提供系统插件的测试目录或测试文件。")
        return 2

    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

    try:
        import pytest
    except ImportError as error:
        _emit_error(sys.stderr, f"当前宿主环境无法加载 pytest：{error}")
        return 2

    return int(pytest.main([
        "-q", "-o", "addopts=",
        "-o", "asyncio_mode=auto",
        "-o", "asyncio_default_fixture_loop_scope=function",
        "-p", "pytest_asyncio.plugin",
        "-p", "no:cacheprovider",
        *arguments,
    ]))


if __name__ == "__main__":
    raise SystemExit(main())
