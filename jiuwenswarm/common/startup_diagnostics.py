# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Early, stdlib-only diagnostics for frozen desktop startup failures.

This module must stay importable before application business modules.  The
Windows installer and a failed desktop startup both execute ``--doctor`` via
the installed executable, so a broken native extension can be reported
without entering the normal application import chain.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import traceback
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.common._build_config import DISPLAY_NAME


DOCTOR_FLAG = "--doctor"
DOCTOR_WORKER_FLAG = "--doctor-worker"
DOCTOR_OUTPUT_FLAG = "--doctor-output"
DOCTOR_SUMMARY_OUTPUT_FLAG = "--doctor-summary-output"
DOCTOR_TIMEOUT_SECONDS = 45.0
DOCTOR_EXIT_OK = 0
DOCTOR_EXIT_ENVIRONMENT_ERROR = 10
DOCTOR_EXIT_INTERNAL_ERROR = 11

STARTUP_DIAGNOSTICS_DIR_ENV = "JIUWENSWARM_STARTUP_DIAGNOSTICS_DIR"
SCHEMA_VERSION = 1

_NATIVE_IMPORT_CHECKS: tuple[tuple[str, str], ...] = (
    ("tiktoken._tiktoken", "tiktoken Native 扩展"),
    ("grpc._cython.cygrpc", "gRPC Native 扩展 cygrpc"),
    ("cryptography.hazmat.bindings._rust", "cryptography Rust 扩展"),
    ("numpy", "NumPy Native 扩展"),
    ("pandas", "Pandas Native 扩展"),
    ("lxml.etree", "lxml Native 扩展"),
    ("PIL._imaging", "Pillow Native 扩展"),
    ("bcrypt._bcrypt", "bcrypt Native 扩展"),
    ("faiss", "FAISS Native 扩展"),
    ("chromadb_rust_bindings", "ChromaDB Rust 扩展"),
)

# Known system-level prerequisites for native extensions checked by doctor.
# Keep this mapping narrow: it is also used to decide which single failure can
# be presented as the root blocker after a child import error.
_NATIVE_SYSTEM_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "grpc._cython.cygrpc": ("dbghelp.dll",),
}

_NATIVE_FAILURE_MARKERS = (
    "dll load failed",
    ".pyd",
    "dynamic module",
    "native extension",
    "cannot open shared object file",
    "failed to load shared library",
    "mach-o",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _data_dir() -> Path:
    configured = os.environ.get("JIUWENSWARM_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".jiuwenswarm"


def default_doctor_output_path() -> Path:
    """Return the persistent diagnostic path used outside installer overrides."""
    return _data_dir() / "agent" / ".logs" / "startup-doctor.json"


def _check_result(
    *,
    name: str,
    display_name: str,
    kind: str,
    status: str,
    message: str = "",
) -> dict[str, str]:
    return {
        "name": name,
        "display_name": display_name,
        "kind": kind,
        "status": status,
        "message": message,
    }


def _check_data_directory(data_dir: Path) -> dict[str, str]:
    probe_path: Path | None = None
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix=".doctor-write-", dir=data_dir)
        os.close(fd)
        probe_path = Path(raw_path)
        return _check_result(
            name="data_directory",
            display_name="用户数据目录",
            kind="filesystem",
            status="ok",
            message=str(data_dir),
        )
    except OSError as exc:
        return _check_result(
            name="data_directory",
            display_name="用户数据目录",
            kind="filesystem",
            status="failed",
            message=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass


def run_doctor(
    *,
    import_module: Callable[[str], object] = importlib.import_module,
    platform_name: str | None = None,
    windows_dll_loader: Callable[[str], object] | None = None,
) -> dict[str, Any]:
    """Run environment checks without importing application business modules."""
    current_platform = platform_name or sys.platform
    checks = [_check_data_directory(_data_dir())]

    if current_platform == "win32":
        if windows_dll_loader is None:
            import ctypes

            windows_dll_loader = ctypes.WinDLL
        try:
            windows_dll_loader("dbghelp.dll")
            checks.append(
                _check_result(
                    name="dbghelp.dll",
                    display_name="Windows 系统库 dbghelp.dll",
                    kind="system_library",
                    status="ok",
                    message="LoadLibrary succeeded",
                )
            )
        except (OSError, AttributeError) as exc:
            checks.append(
                _check_result(
                    name="dbghelp.dll",
                    display_name="Windows 系统库 dbghelp.dll",
                    kind="system_library",
                    status="failed",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )

    for module_name, display_name in _NATIVE_IMPORT_CHECKS:
        try:
            import_module(module_name)
            checks.append(
                _check_result(
                    name=module_name,
                    display_name=display_name,
                    kind="native_import",
                    status="ok",
                )
            )
        except Exception as exc:  # noqa: BLE001 - every import failure is diagnostic data
            checks.append(
                _check_result(
                    name=module_name,
                    display_name=display_name,
                    kind="native_import",
                    status="failed",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )

    failed_checks = [check for check in checks if check["status"] == "failed"]
    status = "ok" if not failed_checks else "environment_error"
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "doctor_result",
        "status": status,
        "timestamp_utc": _utc_now(),
        "platform": current_platform,
        "platform_version": platform.platform(),
        "python_version": platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "data_dir": str(_data_dir()),
        "checks": checks,
    }


def summarize_doctor_result(result: dict[str, Any]) -> str:
    """Build a user-facing summary that is safe for the installer and UI."""
    if result.get("status") == "ok":
        return f"{DISPLAY_NAME} 安装后环境自检通过。"

    failed_checks = [
        check
        for check in result.get("checks", [])
        if isinstance(check, dict) and check.get("status") == "failed"
    ]
    lines = [f"{DISPLAY_NAME} 环境自检未通过，以下组件在当前系统中无法使用："]
    for check in failed_checks:
        display_name = str(check.get("display_name") or check.get("name") or "未知组件")
        message = str(check.get("message") or "加载失败")
        lines.append(f"- {display_name}: {message}")
    lines.append("应用已完成安装，但不会自动启动。请修复系统环境后重新运行自检。")
    return "\n".join(lines)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_temp_path)
    try:
        try:
            stream = os.fdopen(fd, "w", encoding="utf-8", errors="replace")
        except Exception:
            os.close(fd)
            raise
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def write_doctor_result(path: Path, result: dict[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def load_doctor_result(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "doctor_result":
        return None
    return payload


def _process_role(argv: Sequence[str]) -> str:
    role_flags = {
        "--desktop-run-app": "app",
        "--desktop-run-web": "web",
        "--desktop-run-agent": "agent",
        "--desktop-run-gateway": "gateway",
        "--desktop-install-update": "update-helper",
    }
    for flag, role in role_flags.items():
        if flag in argv:
            return role
    return "unknown"


def write_startup_failure(
    exc: BaseException,
    *,
    argv: Sequence[str],
    diagnostics_dir: Path | None = None,
) -> Path | None:
    """Atomically record an exception for the current desktop startup session."""
    if diagnostics_dir is None:
        configured = os.environ.get(STARTUP_DIAGNOSTICS_DIR_ENV)
        if not configured:
            return None
        diagnostics_dir = Path(configured)

    record = {
        "schema_version": SCHEMA_VERSION,
        "type": "startup_failure",
        "timestamp_utc": _utc_now(),
        "pid": os.getpid(),
        "process_role": _process_role(argv),
        "argv": list(argv),
        "error_type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }
    output_path = diagnostics_dir / f"failure-{os.getpid()}-{uuid.uuid4().hex}.json"
    _atomic_write_text(
        output_path,
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return output_path


def load_startup_failures(diagnostics_dir: Path) -> list[dict[str, Any]]:
    """Load only records belonging to the explicitly provided startup session."""
    records: list[dict[str, Any]] = []
    try:
        paths = list(diagnostics_dir.glob("failure-*.json"))
    except OSError:
        return records
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("type") == "startup_failure":
            payload["diagnostic_path"] = str(path)
            records.append(payload)
    records.sort(key=lambda item: str(item.get("timestamp_utc", "")))
    return records


def is_native_startup_failure(record: dict[str, Any] | None) -> bool:
    """Return whether a child failure justifies running native diagnostics."""
    if record is None:
        return False
    error_type = str(record.get("error_type", ""))
    if error_type in {"ImportError", "ModuleNotFoundError"}:
        return True
    details = "\n".join(
        str(record.get(field, "")) for field in ("message", "traceback")
    ).lower()
    return any(marker in details for marker in _NATIVE_FAILURE_MARKERS)


def _name_appears_in_failure(name: str, leaf_name: str, failure_text: str) -> bool:
    """Whether a native check name is referenced in the failure text.

    Matches the full module name or, when the leaf name is long enough to be
    distinctive, the leaf name. Kept as a helper so callers' ``if`` guards stay
    below the boolean-expression limit (G.CTL.03).
    """
    return name in failure_text or (
        len(leaf_name) >= 4 and leaf_name in failure_text
    )


def select_startup_failure(records: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer an actionable import/native error over a wrapper SystemExit."""
    if not records:
        return None

    def score(record: dict[str, Any]) -> tuple[int, str]:
        error_type = str(record.get("error_type", ""))
        actionable = int(is_native_startup_failure(record))
        non_wrapper = int(error_type != "SystemExit")
        return actionable * 10 + non_wrapper, str(record.get("timestamp_utc", ""))

    return max(records, key=score)


def select_blocking_doctor_check(
    doctor_result: dict[str, Any] | None,
    startup_failure: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Select one doctor finding only when it can explain the startup blocker."""
    if not doctor_result or doctor_result.get("status") != "environment_error":
        return None
    checks = doctor_result.get("checks", [])
    if not isinstance(checks, list):
        return None
    failed_checks = [
        check
        for check in checks
        if isinstance(check, dict) and check.get("status") == "failed"
    ]
    if not failed_checks:
        return None
    if startup_failure is not None and not is_native_startup_failure(startup_failure):
        return None

    failure_text = ""
    if startup_failure is not None:
        failure_text = "\n".join(
            str(startup_failure.get(field, "")) for field in ("message", "traceback")
        ).lower()

    matched_native_checks: list[dict[str, Any]] = []
    for check in failed_checks:
        if check.get("kind") != "native_import":
            continue
        name = str(check.get("name") or "").lower()
        leaf_name = name.rsplit(".", 1)[-1]
        if failure_text and _name_appears_in_failure(name, leaf_name, failure_text):
            matched_native_checks.append(check)

    # When both an extension and its known system prerequisite fail, report
    # the prerequisite as the single root blocker rather than both symptoms.
    dependency_candidates = matched_native_checks
    if startup_failure is None:
        dependency_candidates = [
            check for check in failed_checks if check.get("kind") == "native_import"
        ]
    failed_by_name = {
        str(check.get("name") or "").lower(): check for check in failed_checks
    }
    for native_check in dependency_candidates:
        native_name = str(native_check.get("name") or "").lower()
        for dependency in _NATIVE_SYSTEM_DEPENDENCIES.get(native_name, ()):
            if dependency.lower() in failed_by_name:
                return failed_by_name[dependency.lower()]

    if matched_native_checks:
        return matched_native_checks[0]
    if startup_failure is None and len(failed_checks) == 1:
        return failed_checks[0]
    return None


def _emit_console(message: str) -> None:
    if sys.stdout is not None:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            name=f"{__name__}.console",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg=message,
            args=(),
            exc_info=None,
        )
        try:
            handler.handle(record)
            return
        finally:
            handler.close()
    try:
        os.write(1, (message + "\n").encode("utf-8", errors="replace"))
    except OSError:
        pass


def _doctor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Diagnose {DISPLAY_NAME} desktop runtime dependencies."
    )
    parser.add_argument(DOCTOR_FLAG, action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(DOCTOR_WORKER_FLAG, action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(DOCTOR_OUTPUT_FLAG, default="")
    parser.add_argument(DOCTOR_SUMMARY_OUTPUT_FLAG, default="")
    return parser


def doctor_main(argv: Sequence[str] | None = None) -> int:
    """Run the actual checks in the isolated doctor worker process."""
    parser = _doctor_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        result = run_doctor()
        summary = summarize_doctor_result(result)
        output_path = (
            Path(args.doctor_output).expanduser()
            if args.doctor_output
            else default_doctor_output_path()
        )
        write_doctor_result(output_path, result)
        if args.doctor_summary_output:
            _atomic_write_text(Path(args.doctor_summary_output).expanduser(), summary + "\n")
        _emit_console(summary)
        return (
            DOCTOR_EXIT_OK
            if result.get("status") == "ok"
            else DOCTOR_EXIT_ENVIRONMENT_ERROR
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic failures use a distinct exit code
        message = f"{DISPLAY_NAME} 自检程序执行失败: {type(exc).__name__}: {exc}"
        if "args" in locals() and args.doctor_summary_output:
            try:
                _atomic_write_text(Path(args.doctor_summary_output).expanduser(), message + "\n")
            except OSError:
                pass
        _emit_console(message)
        return DOCTOR_EXIT_INTERNAL_ERROR


def _doctor_worker_command(
    *, output_path: Path, summary_output_path: Path | None
) -> list[str]:
    worker_args = [
        DOCTOR_WORKER_FLAG,
        DOCTOR_OUTPUT_FLAG,
        str(output_path),
    ]
    if summary_output_path is not None:
        worker_args.extend([DOCTOR_SUMMARY_OUTPUT_FLAG, str(summary_output_path)])
    if getattr(sys, "frozen", False):
        return [sys.executable, *worker_args]
    return [
        sys.executable,
        "-m",
        "jiuwenswarm.common.startup_diagnostics",
        *worker_args,
    ]


def _write_supervisor_failure(summary_output_path: Path | None, message: str) -> None:
    if summary_output_path is not None:
        try:
            _atomic_write_text(summary_output_path, message + "\n")
        except OSError:
            pass
    _emit_console(message)


def doctor_supervisor_main(argv: Sequence[str] | None = None) -> int:
    """Run doctor checks with a hard timeout so an installer cannot hang."""
    parser = _doctor_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    output_path = (
        Path(args.doctor_output).expanduser()
        if args.doctor_output
        else default_doctor_output_path()
    )
    summary_output_path = (
        Path(args.doctor_summary_output).expanduser()
        if args.doctor_summary_output
        else None
    )
    command = _doctor_worker_command(
        output_path=output_path,
        summary_output_path=summary_output_path,
    )

    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOCTOR_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        message = (
            f"{DISPLAY_NAME} 环境自检超过 {int(DOCTOR_TIMEOUT_SECONDS)} 秒，"
            "可能有 Native 扩展加载卡住。"
        )
        _write_supervisor_failure(summary_output_path, message)
        return DOCTOR_EXIT_INTERNAL_ERROR
    except OSError as exc:
        message = f"{DISPLAY_NAME} 自检程序无法启动: {type(exc).__name__}: {exc}"
        _write_supervisor_failure(summary_output_path, message)
        return DOCTOR_EXIT_INTERNAL_ERROR

    result = load_doctor_result(output_path)
    if completed.returncode not in {DOCTOR_EXIT_OK, DOCTOR_EXIT_ENVIRONMENT_ERROR} or result is None:
        message = f"{DISPLAY_NAME} 自检程序执行失败，退出码：{completed.returncode}"
        _write_supervisor_failure(summary_output_path, message)
        return DOCTOR_EXIT_INTERNAL_ERROR

    _emit_console(summarize_doctor_result(result))
    return completed.returncode


if __name__ == "__main__":
    if DOCTOR_WORKER_FLAG in sys.argv:
        raise SystemExit(doctor_main())
    raise SystemExit(doctor_supervisor_main())
