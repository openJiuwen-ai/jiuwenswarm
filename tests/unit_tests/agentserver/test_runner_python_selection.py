"""Reject unusable runner interpreters without replacing explicit configuration."""

from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server import agent_ws_server as server


@pytest.mark.asyncio
async def test_selection_skips_failed_and_duplicate_candidates(monkeypatch):
    monkeypatch.setattr(server, "_is_std_cpython", lambda path: path != "launcher")
    probe = Mock(side_effect=["missing module", None])
    monkeypatch.setattr(server, "probe_runner_python", probe)
    env = {"PYTHONPATH": "source"}

    selected = await server._select_runner_python(["launcher", "bad", "bad", "good"], env)

    assert selected == str(Path("good").resolve())
    assert probe.call_count == 2
    assert [call.args[0] for call in probe.call_args_list] == [
        str(Path("bad").resolve()), str(Path("good").resolve()),
    ]
    assert all(call.args[1] is env for call in probe.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_failed_selection_returns_no_fallback(monkeypatch, explicit):
    monkeypatch.setattr(server, "_is_std_cpython", lambda path: True)
    probe = Mock(return_value="ModuleNotFoundError: missing")
    monkeypatch.setattr(server, "probe_runner_python", probe)
    error = Mock()
    monkeypatch.setattr(server.logger, "error", error)

    assert await server._select_runner_python(["bad"], {}, explicit=explicit) is None
    probe.assert_called_once()
    error.assert_called_once()
    assert "ModuleNotFoundError: missing" in error.call_args.args[2][0]
    assert "不启动沙箱" in error.call_args.args[0]


@pytest.mark.asyncio
async def test_explicit_interpreter_is_probed_without_standard_filter(monkeypatch):
    monkeypatch.setattr(server, "_is_std_cpython", lambda path: False)
    probe = Mock(return_value=None)
    monkeypatch.setattr(server, "probe_runner_python", probe)

    assert await server._select_runner_python(["configured"], {}, explicit=True) == str(
        Path("configured").resolve()
    )
    probe.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "explicit", "frozen"])
async def test_bootstrap_stops_on_probe_failure_and_preserves_frozen_branch(monkeypatch, tmp_path, mode):
    from jiuwenswarm.common import net_guard_config
    from jiuwenswarm.server import sandbox_policy_render
    from jiuwenswarm.server.runtime import pip_env

    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1", encoding="utf-8")
    monkeypatch.setattr(server, "get_sandbox_runtime", lambda: {"enabled": True})
    monkeypatch.setattr(server, "get_sandbox_startup_mode_explicit", lambda: "internal")
    monkeypatch.setattr(server, "get_sandbox_endpoint", lambda: {"policy_file": str(policy)})
    monkeypatch.setattr(server, "ensure_portable_sandbox_policy_file", Mock())
    monkeypatch.setattr(server, "resolve_sandbox_policy_path", lambda path: policy)
    monkeypatch.setattr(server, "update_sandbox_endpoint", Mock())
    monkeypatch.setattr(server, "update_sandbox_runtime", Mock())
    monkeypatch.setattr(server, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(net_guard_config, "render_saved_sandbox_urls", Mock())
    monkeypatch.setattr(sandbox_policy_render, "_ensure_copy_exists", lambda: None)
    monkeypatch.setattr(pip_env, "resolve_base_python", lambda: Path(sys.base_prefix) / "python.exe")
    monkeypatch.setattr(pip_env, "ensure_runtime_venv", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", mode == "frozen", raising=False)
    monkeypatch.delenv("JIUWENBOX_RUNNER_PYTHON", raising=False)
    if mode == "explicit":
        monkeypatch.setenv("JIUWENBOX_RUNNER_PYTHON", "configured-python")
    select = AsyncMock(return_value=None)
    monkeypatch.setattr(server, "_select_runner_python", select)
    monkeypatch.setattr(server, "_runner_python_candidates", lambda: ["auto-candidate"])
    start = AsyncMock(return_value=True)
    owner = SimpleNamespace(
        _jiuwenbox_runner=SimpleNamespace(ensure_running=start),
        _parse_sandbox_host_port=lambda url: ("127.0.0.1", 8321),
        _allocate_internal_jiuwenbox_port=lambda host, port: port,
    )

    await server.AgentWebSocketServer._bootstrap_internal_jiuwenbox(owner)

    if mode == "frozen":
        select.assert_not_called()
        start.assert_awaited_once()
        assert start.call_args.kwargs["extra_env"]["JIUWENBOX_RUNNER_PYTHON"] == str(
            Path(sys.executable).resolve()
        )
    else:
        start.assert_not_called()
        select.assert_awaited_once()
        if mode == "explicit":
            assert select.call_args.args[0] == ["configured-python"]
            assert select.call_args.kwargs["explicit"] is True
        else:
            assert select.call_args.args[0] == ["auto-candidate"]


def _patch_candidate_sources(monkeypatch, tmp_path, base_prefix):
    roaming = tmp_path / "Roaming"
    local = tmp_path / "Local"
    globs = {
        r"C:\Python3*\python.exe": [r"C:\Python313\python.exe"],
        str(local / "Programs" / "Python" / "Python3*" / "python.exe"): [
            str(local / "Programs" / "Python" / "Python314" / "python.exe"),
        ],
        str(roaming / "uv" / "python" / "cpython-*" / "python.exe"): [
            str(roaming / "uv" / "python" / "cpython-3.12" / "python.exe"),
        ],
    }
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(sys, "base_prefix", str(base_prefix))
    monkeypatch.setattr(server, "_is_std_cpython", lambda path: False)
    monkeypatch.setattr("glob.glob", lambda pattern: globs.get(pattern, []))
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    return roaming, local


def test_uv_base_python_follows_standalone_installs(monkeypatch, tmp_path):
    roaming, local = _patch_candidate_sources(
        monkeypatch, tmp_path, tmp_path / "Roaming" / "uv" / "python" / "cpython-3.13",
    )

    candidates = server._runner_python_candidates()[1:]

    assert candidates == [
        r"C:\Python313\python.exe",
        str(local / "Programs" / "Python" / "Python314" / "python.exe"),
        str(roaming / "uv" / "python" / "cpython-3.13" / "python.exe"),
        str(roaming / "uv" / "python" / "cpython-3.12" / "python.exe"),
    ]


def test_standalone_base_python_is_preferred(monkeypatch, tmp_path):
    base = tmp_path / "Python313"
    _patch_candidate_sources(monkeypatch, tmp_path, base)

    candidates = server._runner_python_candidates()

    assert candidates[0].endswith(str(Path("tools") / "python" / "python.exe"))
    assert candidates[1] == str(base / "python.exe")


def _enable_owner(monkeypatch, tmp_path, start):
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1", encoding="utf-8")
    monkeypatch.setattr(server, "ensure_portable_sandbox_policy_file", Mock())
    monkeypatch.setattr(server, "get_sandbox_endpoint", lambda: {
        "url": "http://127.0.0.1:8321", "startup_mode": "internal", "policy_file": str(policy),
    })
    monkeypatch.setattr(server, "resolve_sandbox_policy_path", lambda path: policy)
    return SimpleNamespace(
        _jiuwenbox_runner=SimpleNamespace(ensure_running=start),
        _parse_sandbox_host_port=lambda url: ("127.0.0.1", 8321),
        _allocate_internal_jiuwenbox_port=lambda host, port: port,
    )


@pytest.mark.asyncio
async def test_enable_refuses_to_start_without_runner_python(monkeypatch, tmp_path):
    start = AsyncMock(return_value=True)
    owner = _enable_owner(monkeypatch, tmp_path, start)
    monkeypatch.setattr(server, "_build_sandbox_env", AsyncMock(return_value=None))

    with pytest.raises(RuntimeError, match="JIUWENBOX_RUNNER_PYTHON"):
        await server.AgentWebSocketServer._handle_sandbox_enable(owner, "channel")
    start.assert_not_called()


@pytest.mark.asyncio
async def test_enable_passes_rebuilt_sandbox_env(monkeypatch, tmp_path):
    start = AsyncMock(return_value=False)
    owner = _enable_owner(monkeypatch, tmp_path, start)
    owner._jiuwenbox_runner.get_stderr_tail = lambda lines: ""
    env = {"JIUWENBOX_RUNNER_PYTHON": "runner-python", "JIUWENBOX_SKILLS_DIR": "skills"}
    monkeypatch.setattr(server, "_build_sandbox_env", AsyncMock(return_value=env))

    with pytest.raises(RuntimeError, match="健康检查失败"):
        await server.AgentWebSocketServer._handle_sandbox_enable(owner, "channel")
    assert start.call_args.kwargs["extra_env"] == env
