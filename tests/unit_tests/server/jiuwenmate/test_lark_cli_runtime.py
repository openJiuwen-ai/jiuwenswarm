"""Independent CLI authentication must never borrow personal credentials."""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def runtime_class():
    try:
        return importlib.import_module("jiuwenswarm.server.jiuwenmate.lark_cli_runtime").FeishuCliRuntime
    except ModuleNotFoundError:
        pytest.fail("Persistent Feishu CLI runtime has not been implemented")


def config(agent="agent-a", app="app-a", secret="test-secret"):
    return {"channels": {"feishu": {"apps": [
        {"enabled": True, "persistent_agent_id": agent, "app_id": app, "app_secret": secret},
    ]}}}


@pytest.fixture
def setup_cli(tmp_path):
    from jiuwenswarm.server.runtime.mcp.package_manifest import load_mcp_package

    package = tmp_path / "packages" / "feishu"
    skill = package / "skills" / "lark-doc"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: lark-doc\n---\nCreate a document.", encoding="utf-8")
    (package / "cli.json").write_text(json.dumps({
        "runtime": {"type": "node"},
        "init": {"windows": "npm install -g @larksuite/cli", "mac": "npm install -g @larksuite/cli", "linux": "npm install -g @larksuite/cli"},
        "versionCheck": {"command": {"windows": "lark-cli --version", "mac": "lark-cli --version", "linux": "lark-cli --version"}, "minVersion": "1.0.94"},
    }), encoding="utf-8")
    (package / "manifest.json").write_text(json.dumps({
        "package_type": "mcp", "id": "feishu", "version": "1.0.0", "name": "feishu", "description": "Feishu",
        "display_name": {"zh": "飞书", "en": "Feishu"},
        "display_description": {"zh": "飞书", "en": "Feishu"},
        "integration": {"type": "cli", "file": "cli.json"}, "skills": [{"dir": "skills"}],
    }), encoding="utf-8")
    calls = []

    async def acquire(_root):
        return load_mcp_package(package)

    async def run(argv, stdin, env):
        calls.append((argv, stdin, env))
        if "install" in argv:
            prefix = Path(argv[argv.index("--prefix") + 1])
            (prefix / "bin").mkdir(parents=True, exist_ok=True)
            (prefix / "bin/lark-cli").touch()
            (prefix / "lark-cli.cmd").touch()
        return "lark-cli version 1.0.94"

    return acquire, run, calls


@pytest.mark.asyncio
async def test_prepare_uses_private_prefix_without_exposing_secret_to_commands(tmp_path, setup_cli):
    acquire, run, calls = setup_cli
    parent = dict(os.environ)
    runtime = runtime_class()(tmp_path / "jm", config_loader=lambda: config(), package_loader=acquire, runner=run)
    result = await runtime.prepare("local", "agent-a")
    assert result["status"] == "ready"
    assert not any("init" in call[0] for call in calls)
    assert "test-secret" not in json.dumps(calls)
    assert "test-secret" not in json.dumps(result)
    assert "test-secret" not in (tmp_path / "jm/runtime/local/agent-a/lark-cli/status.json").read_text()
    assert str(tmp_path / "jm" / "cli" / "bin") in calls[-1][2]["PATH"]
    assert os.environ == parent
    assert runtime.skill_directories("local", "agent-a")


@pytest.mark.asyncio
async def test_repeated_prepare_reuses_matching_profile(tmp_path, setup_cli):
    acquire, run, calls = setup_cli
    runtime = runtime_class()(tmp_path, config_loader=lambda: config(), package_loader=acquire, runner=run)
    await runtime.prepare("local", "agent-a")
    profile = tmp_path / "runtime/local/agent-a/lark-cli/config/config.json"
    unchanged = profile.stat().st_mtime_ns
    await runtime.prepare("local", "agent-a")
    assert profile.stat().st_mtime_ns == unchanged
    assert not any("init" in item[0] for item in calls)


@pytest.mark.asyncio
async def test_missing_private_binary_invalidates_readiness(tmp_path, setup_cli):
    acquire, run, _ = setup_cli
    runtime = runtime_class()(tmp_path, config_loader=lambda: config(), package_loader=acquire, runner=run)
    assert (await runtime.prepare("local", "agent-a"))["status"] == "ready"
    binary = tmp_path / "cli/bin" / ("lark-cli.cmd" if os.name == "nt" else "lark-cli")
    binary.unlink()
    assert runtime.status("local", "agent-a")["status"] != "ready"
    assert not runtime.skill_directories("local", "agent-a")
    assert (await runtime.prepare("local", "agent-a"))["status"] == "ready"


@pytest.mark.asyncio
async def test_private_secret_reference_never_initializes_system_keychain(tmp_path, setup_cli):
    acquire, run, calls = setup_cli
    runtime = runtime_class()(tmp_path, config_loader=lambda: config(), package_loader=acquire, runner=run)
    await runtime.prepare("local", "agent-a")
    profile = tmp_path / "runtime/local/agent-a/lark-cli/config"
    data = json.loads((profile / "config.json").read_text(encoding="utf-8"))
    assert data["apps"][0]["appSecret"] == {"source": "file", "id": str(profile / "app-secret")}
    assert (profile / "app-secret").read_text(encoding="utf-8") == "test-secret"
    assert data["apps"][0]["defaultAs"] == "bot"
    assert not any("init" in argv for argv, _, _ in calls)
    assert not any("test-secret" in json.dumps([argv, stdin, env]) for argv, stdin, env in calls)


@pytest.mark.asyncio
async def test_rebinding_rotates_profile(tmp_path, setup_cli):
    acquire, run, calls = setup_cli
    current = config()
    runtime = runtime_class()(tmp_path, config_loader=lambda: current, package_loader=acquire, runner=run)
    await runtime.prepare("local", "agent-a")
    current["channels"]["feishu"]["apps"][0]["app_secret"] = "rotated-secret"
    assert runtime.status("local", "agent-a")["status"] != "ready"
    await runtime.prepare("local", "agent-a")
    assert (tmp_path / "runtime/local/agent-a/lark-cli/config/app-secret").read_text(encoding="utf-8") == "rotated-secret"
    current["channels"]["feishu"]["apps"].clear()
    assert runtime.status("local", "agent-a")["status"] == "unbound"
    assert not runtime.skill_directories("local", "agent-a")
    assert "unbound" in runtime.shell_environment("local", "agent-a")["LARKSUITE_CLI_CONFIG_DIR"]


@pytest.mark.asyncio
async def test_ambiguous_binding_does_not_initialize_cli(tmp_path, setup_cli):
    acquire, run, calls = setup_cli
    current = config()
    current["channels"]["feishu"]["apps"].append(dict(current["channels"]["feishu"]["apps"][0]))
    runtime = runtime_class()(tmp_path, config_loader=lambda: current, package_loader=acquire, runner=run)
    assert (await runtime.prepare("local", "agent-a"))["status"] == "unavailable"
    assert calls == []


@pytest.mark.asyncio
async def test_child_environment_is_agent_specific(tmp_path, setup_cli):
    acquire, run, _ = setup_cli
    current = config()
    current["channels"]["feishu"]["apps"] += config("agent-b", "app-b")["channels"]["feishu"]["apps"]
    runtime = runtime_class()(tmp_path, config_loader=lambda: current, package_loader=acquire, runner=run)
    await runtime.prepare("local", "agent-a")
    await runtime.prepare("local", "agent-b")
    original = dict(os.environ)
    for agent in ("agent-a", "agent-b"):
        output = subprocess.check_output(
            [sys.executable, "-c", "import os; print(os.environ['LARKSUITE_CLI_CONFIG_DIR'])"],
            env=runtime.shell_environment("local", agent), text=True,
        ).strip()
        assert output == str(tmp_path / "runtime/local" / agent / "lark-cli/config")
        assert runtime.shell_environment("local", agent)["LARKSUITE_CLI_DATA_DIR"] == str(
            tmp_path / "runtime/local" / agent / "lark-cli/data")
    assert os.environ == original


@pytest.mark.asyncio
async def test_changed_binding_during_prepare_cannot_become_ready(tmp_path, setup_cli):
    acquire, run, _ = setup_cli
    current = config()

    async def rotating(root):
        result = await acquire(root)
        current["channels"]["feishu"]["apps"].clear()
        return result

    runtime = runtime_class()(tmp_path, config_loader=lambda: current, package_loader=rotating, runner=run)
    assert (await runtime.prepare("local", "agent-a"))["status"] != "ready"
    assert not runtime.skill_directories("local", "agent-a")


@pytest.mark.asyncio
async def test_ready_profile_still_checks_private_cli_version_on_prepare(tmp_path, setup_cli):
    acquire, run, _ = setup_cli
    changed = False

    async def version(argv, stdin, env):
        if changed and "--version" in argv:
            return "lark-cli version 9.9.9"
        return await run(argv, stdin, env)

    runtime = runtime_class()(tmp_path, config_loader=lambda: config(), package_loader=acquire, runner=version)
    assert (await runtime.prepare("local", "agent-a"))["status"] == "ready"
    changed = True
    assert (await runtime.prepare("local", "agent-a"))["status"] != "ready"


@pytest.mark.asyncio
async def test_prepare_errors_never_persist_secret_output(tmp_path, setup_cli):
    acquire, _, _ = setup_cli

    async def failing(*_args):
        raise RuntimeError("test-secret returned by CLI")

    runtime = runtime_class()(tmp_path, config_loader=lambda: config(), package_loader=acquire, runner=failing)
    result = await runtime.prepare("local", "agent-a")
    assert result["status"] == "unavailable"
    assert "test-secret" not in json.dumps(result)
    assert "test-secret" not in (tmp_path / "runtime/local/agent-a/lark-cli/status.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_windows_launcher_and_unicode_path_support_stdin(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows launcher characterization")
    runtime_module = importlib.import_module("jiuwenswarm.server.jiuwenmate.lark_cli_runtime")
    directory = tmp_path / "中文 路径"
    directory.mkdir()
    script = directory / "stdin.py"
    script.write_text("import sys; print(sys.stdin.read().strip())", encoding="utf-8")
    launcher = directory / "test-cli.cmd"
    launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    output = await runtime_module._run([str(launcher)], "stdin-marker\n", dict(os.environ))
    assert output.strip() == "stdin-marker"


@pytest.mark.asyncio
async def test_cancelled_cli_preparation_stops_its_process(tmp_path):
    import asyncio
    runtime_module = importlib.import_module("jiuwenswarm.server.jiuwenmate.lark_cli_runtime")
    script = tmp_path / "slow-cli.py"
    started, completed = tmp_path / "started", tmp_path / "completed"
    script.write_text(
        f"from pathlib import Path\nimport time\nPath({str(started)!r}).touch()\n"
        f"time.sleep(1)\nPath({str(completed)!r}).touch()\n", encoding="utf-8",
    )
    task = asyncio.create_task(runtime_module._run([sys.executable, str(script)], "", dict(os.environ)))
    for _ in range(100):
        if started.exists():
            break
        await asyncio.sleep(0.01)
    assert started.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.2)
    assert not completed.exists()
