"""Build one system plugin ZIP and run its focused tests before delivery."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import TextIO

from build_package import build_package
from run_tests import main as run_tests

ZIP_ENV = "JIUWEN_SYSTEM_PLUGIN_ZIP"
SOURCE_ENV = "JIUWEN_SYSTEM_PLUGIN_SOURCE"


def _emit(stream: TextIO, message: str, *, level: int = logging.INFO) -> None:
    """Write one stable UTF-8 friendly line without host logger configuration."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord(__name__, level, __file__, 0, message, (), None)
    try:
        handler.emit(record)
    finally:
        handler.close()


def _temporary_zip(output: Path) -> Path:
    """Reserve a short-lived path next to the final artifact for atomic delivery."""
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".extension-validation-", suffix=".zip", dir=output.parent)
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def _restore_environment(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


def main(argv: list[str] | None = None) -> int:
    """Build a temporary ZIP, run plugin-owned tests once, then publish it unchanged."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="测试系统插件的最终 ZIP，并在通过后生成交付文件")
    parser.add_argument("source", type=Path, help="系统插件源码目录")
    parser.add_argument("output", type=Path, help="源码目录之外的最终 ZIP")
    parser.add_argument("tests", type=Path, nargs="+", help="本系统插件的测试目录或测试文件")
    parser.add_argument("--force", action="store_true", help="通过全部验证后覆盖已有最终 ZIP")
    arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)

    source = arguments.source.expanduser().resolve()
    output = arguments.output.expanduser().resolve()
    tests = [path.expanduser().resolve() for path in arguments.tests]
    if output.exists() and not arguments.force:
        parser.error(f"输出已存在；确认替换时使用 --force：{output}")
    missing = [str(path) for path in tests if not path.exists()]
    if missing:
        parser.error(f"测试路径不存在：{', '.join(missing)}")

    temporary = _temporary_zip(output)
    previous_zip = os.environ.get(ZIP_ENV)
    previous_source = os.environ.get(SOURCE_ENV)
    try:
        build_package(source, temporary)
        os.environ[ZIP_ENV] = str(temporary)
        os.environ[SOURCE_ENV] = str(source)
        result = run_tests([str(path) for path in tests])
        if result != 0:
            _emit(sys.stderr, "系统插件测试失败，未生成最终 ZIP。", level=logging.ERROR)
            return result
        os.replace(temporary, output)
        _emit(sys.stdout, json.dumps({
            "zip": str(output),
            "tests": [str(path) for path in tests],
            "validated": ["core_manifest", "entry_paths", "zip_layout", "plugin_tests"],
        }, ensure_ascii=False))
        return 0
    except Exception as error:
        _emit(sys.stderr, f"最终验证失败：{error}", level=logging.ERROR)
        return 1
    finally:
        _restore_environment(ZIP_ENV, previous_zip)
        _restore_environment(SOURCE_ENV, previous_source)
        if temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
