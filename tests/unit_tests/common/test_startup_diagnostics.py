# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for the stdlib-only frozen startup diagnostics."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from jiuwenswarm.common import startup_diagnostics as diagnostics


def test_doctor_reports_native_import_and_known_windows_dll_failures(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))

    def import_module(name: str) -> object:
        if name == "grpc._cython.cygrpc":
            raise ImportError("DLL load failed while importing cygrpc")
        return object()

    def load_dll(name: str) -> object:
        assert name == "dbghelp.dll"
        raise OSError("The specified module could not be found")

    result = diagnostics.run_doctor(
        import_module=import_module,
        platform_name="win32",
        windows_dll_loader=load_dll,
    )

    assert result["status"] == "environment_error"
    failed = {
        check["name"]: check
        for check in result["checks"]
        if check["status"] == "failed"
    }
    assert "dbghelp.dll" in failed
    assert "grpc._cython.cygrpc" in failed
    assert "DLL load failed" in failed["grpc._cython.cygrpc"]["message"]


def test_doctor_main_writes_structured_and_human_readable_results(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    result = {
        "schema_version": 1,
        "type": "doctor_result",
        "status": "environment_error",
        "checks": [
            {
                "name": "dbghelp.dll",
                "display_name": "Windows 系统库 dbghelp.dll",
                "kind": "system_library",
                "status": "failed",
                "message": "OSError: missing",
            }
        ],
    }
    monkeypatch.setattr(diagnostics, "run_doctor", lambda: result)
    output_path = tmp_path / "doctor.json"
    summary_path = tmp_path / "doctor.txt"

    exit_code = diagnostics.doctor_main(
        [
            "--doctor",
            "--doctor-output",
            str(output_path),
            "--doctor-summary-output",
            str(summary_path),
        ]
    )

    assert exit_code == diagnostics.DOCTOR_EXIT_ENVIRONMENT_ERROR
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "environment_error"
    assert "dbghelp.dll" in summary_path.read_text(encoding="utf-8")
    assert "dbghelp.dll" in capsys.readouterr().out


def test_emit_console_falls_back_when_stdout_is_unavailable(monkeypatch) -> None:
    writes: list[tuple[int, bytes]] = []
    monkeypatch.setattr(diagnostics.sys, "stdout", None)
    monkeypatch.setattr(diagnostics.os, "write", lambda fd, data: writes.append((fd, data)))

    diagnostics._emit_console("doctor summary")

    assert writes == [(1, b"doctor summary\n")]


def test_doctor_supervisor_forwards_worker_result(
    tmp_path: Path, monkeypatch
) -> None:
    output_path = tmp_path / "doctor.json"
    summary_path = tmp_path / "doctor.txt"
    worker_result = {
        "schema_version": 1,
        "type": "doctor_result",
        "status": "environment_error",
        "checks": [],
    }

    def run_worker(command, **kwargs):
        assert diagnostics.DOCTOR_WORKER_FLAG in command
        assert kwargs["timeout"] == diagnostics.DOCTOR_TIMEOUT_SECONDS
        diagnostics.write_doctor_result(output_path, worker_result)
        return subprocess.CompletedProcess(command, diagnostics.DOCTOR_EXIT_ENVIRONMENT_ERROR)

    monkeypatch.setattr(diagnostics.subprocess, "run", run_worker)

    exit_code = diagnostics.doctor_supervisor_main(
        [
            "--doctor",
            "--doctor-output",
            str(output_path),
            "--doctor-summary-output",
            str(summary_path),
        ]
    )

    assert exit_code == diagnostics.DOCTOR_EXIT_ENVIRONMENT_ERROR


def test_doctor_supervisor_times_out_with_actionable_summary(
    tmp_path: Path, monkeypatch
) -> None:
    summary_path = tmp_path / "doctor.txt"

    def time_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(diagnostics.subprocess, "run", time_out)

    exit_code = diagnostics.doctor_supervisor_main(
        [
            "--doctor",
            "--doctor-output",
            str(tmp_path / "doctor.json"),
            "--doctor-summary-output",
            str(summary_path),
        ]
    )

    assert exit_code == diagnostics.DOCTOR_EXIT_INTERNAL_ERROR
    assert (
        f"超过 {int(diagnostics.DOCTOR_TIMEOUT_SECONDS)} 秒"
        in summary_path.read_text(encoding="utf-8")
    )


def test_startup_failure_selection_does_not_read_other_sessions(tmp_path: Path) -> None:
    current_session = tmp_path / "current"
    stale_session = tmp_path / "stale"
    stale_session.mkdir()
    (stale_session / "failure-1-old.json").write_text(
        json.dumps(
            {
                "type": "startup_failure",
                "timestamp_utc": "2026-01-01T00:00:00+00:00",
                "error_type": "ImportError",
                "message": "old cygrpc failure",
            }
        ),
        encoding="utf-8",
    )

    try:
        raise RuntimeError("current startup failed")
    except RuntimeError as exc:
        diagnostics.write_startup_failure(
            exc,
            argv=["jiuwenswarm.exe", "--desktop-run-web"],
            diagnostics_dir=current_session,
        )

    records = diagnostics.load_startup_failures(current_session)
    selected = diagnostics.select_startup_failure(records)

    assert len(records) == 1
    assert selected is not None
    assert selected["message"] == "current startup failed"
    assert selected["process_role"] == "web"


def test_startup_failure_prefers_import_error_over_wrapper_system_exit() -> None:
    selected = diagnostics.select_startup_failure(
        [
            {
                "timestamp_utc": "2026-01-01T00:00:02+00:00",
                "error_type": "SystemExit",
                "message": "1",
            },
            {
                "timestamp_utc": "2026-01-01T00:00:01+00:00",
                "error_type": "ImportError",
                "message": "DLL load failed while importing cygrpc",
            },
        ]
    )

    assert selected is not None
    assert selected["error_type"] == "ImportError"


def test_native_startup_failure_detection_is_conservative() -> None:
    assert diagnostics.is_native_startup_failure(
        {
            "error_type": "ImportError",
            "message": "DLL load failed while importing cygrpc",
        }
    )
    assert diagnostics.is_native_startup_failure(
        {
            "error_type": "RuntimeError",
            "message": "Failed to load native extension package.pyd",
        }
    )
    assert not diagnostics.is_native_startup_failure(
        {
            "error_type": "OSError",
            "message": "[WinError 10048] address already in use",
        }
    )
    assert not diagnostics.is_native_startup_failure(
        {
            "error_type": "RuntimeError",
            "message": "Timed out waiting for http://127.0.0.1:7860",
        }
    )
