# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Desktop startup state and per-session diagnostic behavior."""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from jiuwenswarm.common.startup_diagnostics import write_startup_failure
from jiuwenswarm.instance_manager.config import calculate_instance_ports


@pytest.fixture
def desktop_app(monkeypatch):
    if "webview" not in sys.modules:
        monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))
    sys.modules.pop("jiuwenswarm.channels.desktop.desktop_app", None)
    from jiuwenswarm.channels.desktop import desktop_app as mod

    return mod


def _runtime(desktop_app, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(desktop_app, "get_logs_dir", lambda: tmp_path / "logs")
    return desktop_app.DesktopRuntime(
        frontend_host="127.0.0.1",
        ports=calculate_instance_ports(0),
    )


def test_child_env_is_scoped_to_current_startup_session(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    session_dir = tmp_path / "session"
    monkeypatch.delenv("JIUWENSWARM_START_CMD", raising=False)
    monkeypatch.setattr(desktop_app.sys, "argv", ["workswarm.exe"])
    env = desktop_app._build_child_env(
        "app",
        calculate_instance_ports(0),
        startup_diagnostics_dir=session_dir,
    )

    assert env[desktop_app.STARTUP_DIAGNOSTICS_DIR_ENV] == str(session_dir)
    assert json.loads(env["JIUWENSWARM_START_CMD"]) == ["workswarm.exe"]


def test_failed_status_reports_correlated_system_dependency(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    try:
        raise ImportError("DLL load failed while importing cygrpc")
    except ImportError as exc:
        write_startup_failure(
            exc,
            argv=["jiuwenswarm.exe", "--desktop-run-agent"],
            diagnostics_dir=runtime._startup_diagnostics_dir,
        )
    doctor_result = {
        "status": "environment_error",
        "checks": [
            {
                "name": "dbghelp.dll",
                "display_name": "Windows 系统库 dbghelp.dll",
                "status": "failed",
                "message": "OSError: missing",
            },
            {
                "name": "grpc._cython.cygrpc",
                "display_name": "gRPC Native 扩展 cygrpc",
                "kind": "native_import",
                "status": "failed",
                "message": "ImportError: DLL load failed",
            },
        ],
    }

    status = runtime._build_failed_status(RuntimeError("app exited 1"), doctor_result)

    assert status["title"] == "运行环境缺少必要组件"
    assert status["component"] == "dbghelp.dll"
    assert "missing" in status["message"]


def test_unrelated_doctor_failure_does_not_replace_known_child_failure(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    try:
        raise RuntimeError("port 7860 is already in use")
    except RuntimeError as exc:
        write_startup_failure(
            exc,
            argv=["jiuwenswarm.exe", "--desktop-run-web"],
            diagnostics_dir=runtime._startup_diagnostics_dir,
        )
    doctor_result = {
        "status": "environment_error",
        "checks": [
            {
                "name": "faiss",
                "display_name": "FAISS Native 扩展",
                "kind": "native_import",
                "status": "failed",
                "message": "ImportError: unrelated",
            }
        ],
    }

    status = runtime._build_failed_status(RuntimeError("wrapper"), doctor_result)

    assert status["component"] == "web"
    assert "port 7860" in status["message"]
    assert "FAISS" not in status["message"]


def test_unrelated_doctor_failure_does_not_replace_native_child_failure(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    try:
        raise ImportError("DLL load failed while importing cygrpc")
    except ImportError as exc:
        write_startup_failure(
            exc,
            argv=["jiuwenswarm.exe", "--desktop-run-agent"],
            diagnostics_dir=runtime._startup_diagnostics_dir,
        )
    doctor_result = {
        "status": "environment_error",
        "checks": [
            {
                "name": "faiss",
                "display_name": "FAISS Native 扩展",
                "kind": "native_import",
                "status": "failed",
                "message": "ImportError: unrelated",
            }
        ],
    }

    status = runtime._build_failed_status(RuntimeError("wrapper"), doctor_result)

    assert status["component"] == "agent"
    assert "cygrpc" in status["message"]
    assert "FAISS" not in status["message"]


def test_runtime_doctor_trigger_requires_native_evidence_or_unexplained_exit(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    monkeypatch.setattr(desktop_app.sys, "frozen", True, raising=False)

    assert runtime._should_run_startup_doctor(
        RuntimeError("app exited early with code 1"),
        None,
    )
    assert runtime._should_run_startup_doctor(
        RuntimeError("wrapper"),
        {
            "error_type": "ImportError",
            "message": "DLL load failed while importing cygrpc",
        },
    )
    assert not runtime._should_run_startup_doctor(
        RuntimeError("Timed out waiting for http://127.0.0.1:7860"),
        {
            "error_type": "RuntimeError",
            "message": "port 7860 is already in use",
        },
    )


def test_failed_status_reads_only_runtime_session(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    stale_dir = runtime._startup_diagnostics_dir.parent / "stale-session"
    try:
        raise ImportError("stale cygrpc error")
    except ImportError as exc:
        write_startup_failure(
            exc,
            argv=["jiuwenswarm.exe", "--desktop-run-agent"],
            diagnostics_dir=stale_dir,
        )
    try:
        raise RuntimeError("current web error")
    except RuntimeError as exc:
        write_startup_failure(
            exc,
            argv=["jiuwenswarm.exe", "--desktop-run-web"],
            diagnostics_dir=runtime._startup_diagnostics_dir,
        )

    status = runtime._build_failed_status(RuntimeError("wrapper"), None)

    assert status["component"] == "web"
    assert "current web error" in status["message"]
    assert "stale cygrpc" not in status["message"]


def test_startup_status_is_terminal_after_failure(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    runtime._set_startup_status("diagnosing", message="checking")
    runtime._set_startup_status("failed", message="failed")
    runtime._set_startup_status("ready", message="late ready")

    status = runtime.get_startup_status()
    assert status["state"] == "failed"
    assert status["message"] == "failed"


def test_process_started_during_shutdown_is_terminated(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    fake_process = object()
    terminated = []
    monkeypatch.setattr(desktop_app, "_start_process", lambda *_args, **_kwargs: fake_process)
    monkeypatch.setattr(desktop_app, "_terminate_process_tree", terminated.append)
    runtime._is_shutting_down = True

    with pytest.raises(RuntimeError, match="startup cancelled"):
        runtime._start_managed_process("web", ["fake-web"])

    assert terminated == [fake_process]
    assert "web" not in runtime.processes


def test_start_services_terminates_peer_waiter_after_first_readiness_failure(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    """A failed readiness probe must not leave the other probe timing out."""
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    agent_process = types.SimpleNamespace(poll=lambda: None)
    gateway_process = types.SimpleNamespace(poll=lambda: None)
    web_process = types.SimpleNamespace(poll=lambda: None)
    peer_terminated = threading.Event()
    terminated = []

    monkeypatch.setattr(runtime, "_preflight_gateway_singleton", lambda: None)
    monkeypatch.setattr(desktop_app, "_warmup_page_cache_background", lambda: None)
    monkeypatch.setattr(desktop_app, "prepare_runtime_workspace", lambda: None)
    monkeypatch.setattr(
        runtime,
        "_start_managed_process",
        lambda name, _command: {
            "agent": agent_process,
            "gateway": gateway_process,
            "web": web_process,
        }[name],
    )
    monkeypatch.setattr(desktop_app, "_ensure_process_running", lambda *_args: None)

    def wait_for_tcp(_host, port, *_args, **_kwargs):
        if port == runtime.ports["agent_server"]:
            raise RuntimeError("agent failed")
        assert peer_terminated.wait(timeout=1.0)
        raise RuntimeError("gateway terminated")

    def wait_for_termination(*_args, **_kwargs):
        assert peer_terminated.wait(timeout=1.0)
        raise RuntimeError("web terminated")

    def terminate(process):
        terminated.append(process)
        peer_terminated.set()

    monkeypatch.setattr(desktop_app, "_wait_for_tcp", wait_for_tcp)
    monkeypatch.setattr(desktop_app, "_wait_for_http", wait_for_termination)
    monkeypatch.setattr(desktop_app, "_terminate_process_tree", terminate)

    started_at = time.monotonic()
    with pytest.raises(RuntimeError, match="agent failed"):
        runtime.start_services()

    assert time.monotonic() - started_at < 1.0
    assert {id(process) for process in terminated} == {
        id(agent_process),
        id(gateway_process),
        id(web_process),
    }


def test_web_ready_state_is_overridable_by_failed(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    """先行导航态(web_ready)非终态, app 随后失败时 failed 必须能覆盖。"""
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    runtime._set_startup_status("web_ready", frontend_url=runtime.frontend_url)
    assert runtime.get_startup_status()["state"] == "web_ready"
    runtime._set_startup_status(
        "failed", title="t", message="app failed", component="app"
    )
    status = runtime.get_startup_status()
    assert status["state"] == "failed"
    assert status["component"] == "app"


def test_start_services_reports_web_ready_when_backend_fails_at_same_time(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    """A concurrent Web-ready and Agent failure must still surface web_ready first."""
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)
    agent_process = types.SimpleNamespace(poll=lambda: None)
    gateway_process = types.SimpleNamespace(poll=lambda: None)
    web_process = types.SimpleNamespace(poll=lambda: None)
    web_ready = threading.Event()
    callback_calls: list[str] = []

    monkeypatch.setattr(runtime, "_preflight_gateway_singleton", lambda: None)
    monkeypatch.setattr(desktop_app, "_warmup_page_cache_background", lambda: None)
    monkeypatch.setattr(desktop_app, "prepare_runtime_workspace", lambda: None)
    monkeypatch.setattr(
        runtime,
        "_start_managed_process",
        lambda name, _command: {
            "agent": agent_process,
            "gateway": gateway_process,
            "web": web_process,
        }[name],
    )
    monkeypatch.setattr(desktop_app, "_ensure_process_running", lambda *_args: None)
    monkeypatch.setattr(desktop_app, "_terminate_process_tree", lambda *_args: None)

    def wait_for_tcp(_host, port, *_args, **_kwargs):
        if port == runtime.ports["agent_server"]:
            assert web_ready.wait(timeout=1.0)
            raise RuntimeError("agent failed immediately after web ready")
        raise RuntimeError("gateway terminated")

    def wait_for_http(*_args, **_kwargs):
        web_ready.set()

    monkeypatch.setattr(desktop_app, "_wait_for_tcp", wait_for_tcp)
    monkeypatch.setattr(desktop_app, "_wait_for_http", wait_for_http)

    # Either backend waiter may win the race and provide the surfaced error;
    # the invariant is that Web-ready was still observed exactly once before
    # startup transitioned to failure.
    with pytest.raises(RuntimeError):
        runtime.start_services(on_web_ready=lambda: callback_calls.append("web_ready"))

    assert callback_calls == ["web_ready"]


def test_failure_after_early_navigation_shows_diagnostics(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    """web 先行导航后 app 失败: failed 必须可达, 诊断页须重新载入窗口。"""
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)

    class FakeEvent:
        def __iadd__(self, _handler):
            return self

    class FakeWindow:
        def __init__(self) -> None:
            self.events = types.SimpleNamespace(
                loaded=FakeEvent(), closed=FakeEvent()
            )
            self.loaded_html = None

        def load_html(self, html):
            self.loaded_html = html

    fake_window = FakeWindow()
    monkeypatch.setattr(desktop_app, "get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        desktop_app.webview,
        "create_window",
        lambda *_args, **_kwargs: fake_window,
        raising=False,
    )
    monkeypatch.setattr(runtime, "_clear_wkwebview_system_cache", lambda: None)
    monkeypatch.setattr(
        runtime, "_should_run_startup_doctor", lambda _exc, _cf: False
    )
    monkeypatch.setattr(
        runtime,
        "_build_failed_status",
        lambda _exc, _dr, _cf=None: {
            "title": "t",
            "message": "app failed",
            "component": "app",
            "diagnostic_path": str(runtime._startup_diagnostics_dir),
        },
    )
    monkeypatch.setattr(runtime, "shutdown", lambda: None)

    def fake_start_services(on_web_ready=None) -> None:
        if on_web_ready:
            on_web_ready()
        raise RuntimeError("app failed")

    monkeypatch.setattr(runtime, "start_services", fake_start_services)

    def start_webview(**_kwargs) -> None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if (
                runtime.get_startup_status()["state"] == "failed"
                and fake_window.loaded_html is not None
            ):
                return
            time.sleep(0.01)
        raise AssertionError("startup failure surface not presented")

    monkeypatch.setattr(desktop_app.webview, "start", start_webview, raising=False)

    runtime.run(window_title="test", width=1200, height=800, debug=False)

    status = runtime.get_startup_status()
    assert status["state"] == "failed"
    assert status["component"] == "app"
    assert fake_window.loaded_html is not None
    assert "get_startup_status" in fake_window.loaded_html


def test_loading_page_polls_state_and_has_bridge_watchdog(desktop_app) -> None:
    html = desktop_app.DesktopRuntime._build_loading_html()

    assert "get_startup_status" in html
    assert "status.state==='failed'" in html
    assert "desktop-webview-bridge" in html
    assert "desktop-navigation" in html
    assert "window.location.replace(status.frontend_url)" in html


def test_desktop_doctor_timeout_leaves_supervisor_cleanup_margin(desktop_app) -> None:
    assert (
        desktop_app.STARTUP_DOCTOR_TIMEOUT_SECONDS
        > desktop_app.DOCTOR_TIMEOUT_SECONDS
    )


def test_service_failure_transitions_loading_state_without_worker_window_calls(
    desktop_app, tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(desktop_app, tmp_path, monkeypatch)

    class FakeEvent:
        def __iadd__(self, _handler):
            return self

    class FakeWindow:
        def __init__(self) -> None:
            self.events = types.SimpleNamespace(loaded=FakeEvent(), closed=FakeEvent())

    fake_window = FakeWindow()
    monkeypatch.setattr(desktop_app, "get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        desktop_app.webview,
        "create_window",
        lambda *_args, **_kwargs: fake_window,
        raising=False,
    )

    def start_webview(**_kwargs) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if runtime.get_startup_status()["state"] == "failed":
                return
            time.sleep(0.01)
        raise AssertionError("startup failure did not reach the UI state")

    def fail_services(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("child exited early")

    monkeypatch.setattr(desktop_app.webview, "start", start_webview, raising=False)
    monkeypatch.setattr(runtime, "_clear_wkwebview_system_cache", lambda: None)
    monkeypatch.setattr(runtime, "start_services", fail_services)
    doctor_calls = []
    monkeypatch.setattr(
        runtime,
        "_run_doctor_after_failure",
        lambda: doctor_calls.append(True),
    )
    monkeypatch.setattr(runtime, "shutdown", lambda: None)

    runtime.run(window_title="test", width=1200, height=800, debug=False)

    status = runtime.get_startup_status()
    assert status["state"] == "failed"
    assert "child exited early" in status["message"]
    assert doctor_calls == []


def test_installer_does_not_block_completion_on_doctor() -> None:
    installer = (
        Path(__file__).resolve().parents[2] / "scripts" / "installer.iss"
    ).read_text(encoding="utf-8")

    assert "Flags: nowait postinstall" in installer
    assert "--doctor --doctor-output" not in installer
    assert "Check: DoctorPassed" not in installer
    assert "DoctorSucceeded" not in installer
    assert "ExecAsOriginalUser" not in installer
