# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows installer guard and cleanup contract tests."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[2]
ENTRY_PATH = ROOT / "scripts" / "jiuwenswarm_exe_entry.py"
INSTALLER_PATH = ROOT / "scripts" / "installer.iss"


@pytest.fixture
def exe_entry():
    spec = importlib.util.spec_from_file_location(
        "jiuwenswarm_exe_entry_uninstall_test",
        ENTRY_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "arguments, expected_on_windows",
    [
        (["jiuwenswarm.exe"], True),
        (["jiuwenswarm.exe", "--desktop-run-app"], True),
        (["jiuwenswarm.exe", "--desktop-run-web"], True),
        (["jiuwenswarm.exe", "--desktop-run-agent"], True),
        (["jiuwenswarm.exe", "--desktop-run-gateway"], True),
        (["jiuwenswarm.exe", "--desktop-install-update"], False),
    ],
)
def test_mutex_covers_desktop_process_tree(
    exe_entry,
    monkeypatch,
    arguments: list[str],
    expected_on_windows: bool,
):
    monkeypatch.setattr(exe_entry.sys, "frozen", True, raising=False)
    monkeypatch.setattr(exe_entry.sys, "argv", arguments)

    expected = expected_on_windows if os.name == "nt" else False
    assert exe_entry._should_hold_windows_app_mutex() is expected


def test_main_acquires_mutex_before_dispatch(exe_entry, monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(
        exe_entry,
        "_acquire_windows_app_mutexes",
        lambda: events.append("acquire"),
    )
    monkeypatch.setattr(exe_entry, "_dispatch", lambda: events.append("dispatch"))

    exe_entry.main()

    assert events == ["acquire", "dispatch"]


def test_main_keeps_mutex_until_process_exit_after_failure(exe_entry, monkeypatch):
    events: list[str] = []

    def fail_dispatch() -> None:
        events.append("dispatch")
        raise RuntimeError("boom")

    monkeypatch.setattr(
        exe_entry,
        "_acquire_windows_app_mutexes",
        lambda: events.append("acquire"),
    )
    monkeypatch.setattr(exe_entry, "_dispatch", fail_dispatch)
    monkeypatch.setattr(exe_entry, "_write_child_error", lambda exc: None)

    with pytest.raises(SystemExit) as exc_info:
        exe_entry.main()

    assert exc_info.value.code == 1
    assert events == ["acquire", "dispatch"]


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows kernel API")
def test_windows_mutex_lifetime_follows_last_holder_process(tmp_path):
    from ctypes import wintypes
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    open_mutex.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    def mutex_exists(name: str) -> bool:
        handle = open_mutex(0x00100000, False, name)  # SYNCHRONIZE
        if not handle:
            return False
        close_handle(handle)
        return True

    def start_holder(ready_path: Path, names: tuple[str, str]):
        child_code = """
import importlib.util
from pathlib import Path
import sys

entry_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
mutex_names = tuple(sys.argv[3:])
spec = importlib.util.spec_from_file_location("mutex_holder_entry", entry_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._WINDOWS_APP_MUTEX_NAMES = mutex_names
module.sys.frozen = True
module.sys.argv = ["jiuwenswarm.exe"]
module._dispatch = lambda: None
module.main()
if len(module._WINDOWS_APP_MUTEX_HANDLES) != len(mutex_names):
    raise SystemExit("not all mutex handles were created")
ready_path.write_text("ready", encoding="utf-8")
sys.stdin.read(1)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", child_code, str(ENTRY_PATH), str(ready_path), *names],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while not ready_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                process.kill()
                raise AssertionError("mutex holder did not become ready")
            time.sleep(0.05)
        if process.poll() is not None:
            error = process.stderr.read() if process.stderr else ""
            raise AssertionError(f"mutex holder exited early: {error}")
        return process

    def stop_holder(process: subprocess.Popen[str]) -> None:
        assert process.stdin is not None
        process.stdin.write("x")
        process.stdin.flush()
        process.stdin.close()
        process.wait(timeout=10)
        error = process.stderr.read() if process.stderr else ""
        assert process.returncode == 0, error

    suffix = uuid.uuid4().hex
    mutex_names = (f"JiuwenSwarm.Test.{suffix}", f"Global\\JiuwenSwarm.Test.{suffix}")
    first = start_holder(tmp_path / "first.ready", mutex_names)
    second = start_holder(tmp_path / "second.ready", mutex_names)

    try:
        assert all(mutex_exists(name) for name in mutex_names)

        stop_holder(first)
        assert all(mutex_exists(name) for name in mutex_names)

        stop_holder(second)
        assert not any(mutex_exists(name) for name in mutex_names)
    finally:
        for process in (first, second):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_installer_blocks_running_app_and_preserves_user_data(exe_entry):
    script = INSTALLER_PATH.read_text(encoding="utf-8")
    uninstall_delete = script.split("[UninstallDelete]", maxsplit=1)[1].split(
        "[Run]",
        maxsplit=1,
    )[0]

    assert exe_entry._WINDOWS_APP_MUTEX_NAMES == (
        "JiuwenSwarm.App",
        r"Global\JiuwenSwarm.App",
        "WorkSwarm.App",
        r"Global\WorkSwarm.App",
    )
    app_mutex = next(
        line.removeprefix("AppMutex=")
        for line in script.splitlines()
        if line.startswith("AppMutex=")
    ).split(",")
    assert set(exe_entry._LEGACY_WINDOWS_APP_MUTEX_NAMES) <= set(app_mutex)
    assert "{#MyAppName}.App" in app_mutex
    assert "Global\\{#MyAppName}.App" in app_mutex
    assert "CloseApplications=yes" in script
    assert "CloseApplications=force" not in script
    assert 'Name: "{app}\\_internal"' in uninstall_delete
    assert 'Name: "{app}\\runtime"' in uninstall_delete
    assert 'Name: "{app}\\{#MyAppExeName}"' in uninstall_delete
    assert 'Type: dirifempty; Name: "{app}"' in uninstall_delete
    assert 'Type: filesandordirs; Name: "{app}"' not in uninstall_delete
    assert "{userappdata}" not in uninstall_delete
    assert "{userprofile}" not in uninstall_delete
