"""Runner interpreter selection must not silently switch environments."""

import sys
import ctypes
import os
import json
from types import SimpleNamespace

import pytest

from jiuwenbox.supervisor import win_exec


def test_missing_explicit_runner_python_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWENBOX_RUNNER_PYTHON", str(tmp_path / "missing" / "python.exe"))

    with pytest.raises(RuntimeError, match="JIUWENBOX_RUNNER_PYTHON"):
        win_exec._build_runner_command("sb", "C:\\ws", 60080, 60089, 60100)


def test_existing_explicit_runner_python_is_used(monkeypatch, tmp_path):
    runner_python = tmp_path / "python.exe"
    runner_python.write_bytes(b"")
    monkeypatch.setenv("JIUWENBOX_RUNNER_PYTHON", str(runner_python))

    assert str(runner_python) in win_exec._build_runner_command("sb", "C:\\ws", 60080, 60089, 60100)


def test_unset_runner_python_uses_current_interpreter(monkeypatch):
    monkeypatch.delenv("JIUWENBOX_RUNNER_PYTHON", raising=False)

    assert sys.executable in win_exec._build_runner_command("sb", "C:\\ws", 60080, 60089, 60100)


@pytest.mark.parametrize("explicit_env", [False, True])
@pytest.mark.parametrize("profile_available", [False, True])
def test_child_inherits_environment_with_sandbox_profile_and_proxy(
    monkeypatch, tmp_path, explicit_env, profile_available,
):
    from jiuwenbox.supervisor import win_softdelete
    from jiuwenbox.server.sandbox_manager import _build_windows_exec_env

    captured = {}

    def capture_environment(*args):
        pointer = ctypes.cast(args[7], ctypes.POINTER(ctypes.c_wchar))
        chars = []
        index = 0
        while not (index and pointer[index] == "\0" and chars[-1] == "\0"):
            chars.append(pointer[index])
            index += 1
        captured.update(item.split("=", 1) for item in "".join(chars).split("\0") if item)
        raise RuntimeError("environment captured")

    monkeypatch.setattr(win_exec, "_get_advapi32", lambda: SimpleNamespace(
        CreateProcessAsUserW=capture_environment,
    ))
    profile = str(tmp_path / "sandbox-profile")
    monkeypatch.setattr(
        win_exec, "get_sandbox_profile_dir", lambda: profile if profile_available else None,
    )
    monkeypatch.setattr(win_exec, "_push_log", lambda *args: None)
    monkeypatch.setattr(win_exec.os, "makedirs", lambda *args, **kwargs: None)
    monkeypatch.setattr(win_softdelete, "merge_soft_delete_env", lambda env: None)
    monkeypatch.setenv("CUSTOM_TOOL_SETTING", "inherited-setting")
    monkeypatch.setenv("EMPTY_TOOL_SETTING", "")
    monkeypatch.setenv("USERPROFILE", "C:/host-profile")
    monkeypatch.setenv("HOME", "C:/host-profile")
    monkeypatch.setenv("TEMP", "C:/host-temp")
    monkeypatch.setenv("TMPDIR", "C:/host-temp")
    monkeypatch.setenv("HTTP_PROXY", "http://host-proxy:8080")
    monkeypatch.setenv("all_proxy", "http://host-proxy:8080")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("JIUWENBOX_ENV_ALLOWLIST", json.dumps([
        "CUSTOM_TOOL_SETTING", "EMPTY_TOOL_SETTING",
    ]))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("DATABASE_URL", "must-not-be-forwarded")
    monkeypatch.setenv("PYTHONPATH", "runner-loader-path")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", "C:/host-bytecode")
    monkeypatch.setenv("NODE_OPTIONS", "runner-loader-options")
    monkeypatch.setenv("JIUWENBOX_SOFT_DELETE", "1")
    request_env = _build_windows_exec_env(
        {"CUSTOM_TOOL_SETTING": "request-override"} if explicit_env else None,
    )
    # Reproduce caller-supplied host directories; both profile branches override them.
    request_env.update({key: "C:/host-profile" for key in (
        "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA",
    )})
    request_env["jiuwenbox_service_read_roots"] = '["C:/direct-request"]'
    request_env["JIUWENBOX_INJECT_ENV"] = json.dumps({
        "JIUWENBOX_SERVICE_READ_ROOTS": '["C:/injected-request"]', "INJECTED_SETTING": "kept",
    })
    if explicit_env:
        request_env["NODE_OPTIONS"] = ""
        request_env["PYTHONPYCACHEPREFIX"] = "C:/request-bytecode"
    with pytest.raises(RuntimeError, match="environment captured"):
        win_exec._create_process_as_user(
            1, ["python"], request_env, None, 2, 3, workspace=str(tmp_path / "sb"),
        )
    assert captured["CUSTOM_TOOL_SETTING"] == (
        "request-override" if explicit_env else "inherited-setting"
    )
    assert captured["EMPTY_TOOL_SETTING"] == ""
    if profile_available:
        assert captured["USERPROFILE"] == captured["HOME"] == profile
        expected_temp = os.path.join(profile, "AppData", "Local", "Temp", "jiuwenbox", "sb")
    else:
        assert captured["USERPROFILE"] == captured["HOME"] == str(tmp_path / "sb")
        expected_temp = str(tmp_path / "sb" / ".tmp")
    assert captured["TMP"] == captured["TEMP"] == captured["TMPDIR"] == expected_temp
    assert captured["PYTHONPYCACHEPREFIX"] == os.path.join(expected_temp, "python-cache")
    assert captured["HTTP_PROXY"] == f"http://127.0.0.1:{win_exec._proxy_port_start}"
    assert captured["all_proxy"] == captured["HTTP_PROXY"]
    assert captured["no_proxy"] == "127.0.0.1,localhost,::1"
    assert os.environ["USERPROFILE"] == "C:/host-profile"
    assert captured["APPDATA"] != "C:/host-profile"
    assert captured["LOCALAPPDATA"] != "C:/host-profile"
    assert "OPENAI_API_KEY" not in captured
    assert "DATABASE_URL" not in captured
    assert "PYTHONPATH" not in captured
    assert "JIUWENBOX_SERVICE_READ_ROOTS" not in {key.upper() for key in captured}
    assert captured["INJECTED_SETTING"] == "kept"
    assert captured["JIUWENBOX_SOFT_DELETE"] == "1"
    if explicit_env:
        assert captured["NODE_OPTIONS"] == ""
    else:
        assert "NODE_OPTIONS" not in captured
