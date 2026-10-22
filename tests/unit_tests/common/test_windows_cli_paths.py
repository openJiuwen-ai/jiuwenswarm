"""Windows npm preservation and path resolution regressions."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import windows_cli_paths as paths


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "preserve-windows-cli.ps1"


@pytest.fixture
def windows_environment(tmp_path, monkeypatch):
    root = tmp_path / "data" / "runtime" / "npm"
    root.mkdir(parents=True)
    monkeypatch.setattr(paths, "windows_npm_root", lambda: root)
    # Replace the module reference, not the process-wide os.name used by pathlib.
    environment = {"PATH": str(tmp_path / "system-node"), "NPM_CONFIG_USERCONFIG": str(tmp_path / "absent.npmrc")}
    monkeypatch.setattr(paths, "os", SimpleNamespace(name="nt", environ=environment, path=os.path, pathsep=os.pathsep))
    return root, environment


def test_missing_configured_cli_maps_to_preserved_tree_only(windows_environment, tmp_path):
    root, _ = windows_environment
    old = tmp_path / "app" / "runtime" / "node-runtime"
    saved = root / "preserved" / "snapshot"
    active = root / "installed" / "snapshot" / "prefix"
    target = active / "node_modules" / ".bin" / "claude.cmd"
    target.parent.mkdir(parents=True)
    saved.mkdir(parents=True)
    target.write_text("retained entry")
    (saved / "mapping.json").write_text(json.dumps({"schema": 1, "source": str(old), "target": str(active)}))
    original = old / "node_modules" / ".bin" / "claude.cmd"
    assert paths.resolve_preserved_cli_path(str(original)) == str(target)
    assert paths.resolve_preserved_cli_path(str(tmp_path / "custom" / "claude.cmd")) == str(tmp_path / "custom" / "claude.cmd")
    original.parent.mkdir(parents=True)
    original.write_text("new user installation")
    assert paths.resolve_preserved_cli_path(str(original)) == str(original)


def test_invalid_manifests_and_traversal_do_not_redirect(windows_environment, tmp_path):
    root, _ = windows_environment
    saved = root / "preserved" / "snapshot"
    saved.mkdir(parents=True)
    manifest = saved / "mapping.json"
    original = str(tmp_path / "old" / "cli.cmd")
    for value in ([], {"schema": 99}, {"schema": 1, "source": None}):
        manifest.write_text(json.dumps(value))
        assert paths.resolve_preserved_cli_path(original) == original
    manifest.write_text("not json")
    assert paths.resolve_preserved_cli_path(original) == original
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "cli.cmd").touch()
    manifest.write_text(json.dumps({"schema": 1, "source": str(tmp_path / "old"), "target": str(outside)}))
    assert paths.resolve_preserved_cli_path(original) == original


def test_preserved_commands_are_available_with_stale_parent_path(windows_environment, tmp_path):
    root, environment = windows_environment
    saved = root / "preserved" / "snapshot"
    active = root / "installed" / "snapshot" / "prefix"
    (active / "node_modules" / ".bin").mkdir(parents=True)
    saved.mkdir(parents=True)
    (saved / "mapping.json").write_text(json.dumps({"schema": 1, "source": str(tmp_path / "old"), "target": str(active)}))
    paths.configure_windows_npm_environment()
    assert str(active) in environment["PATH"].split(os.pathsep)
    assert str(active / "node_modules" / ".bin") in environment["PATH"].split(os.pathsep)


def test_managed_prefix_uses_only_npm_wrappers_before_bundled_node(windows_environment):
    root, environment = windows_environment
    (root / "global").mkdir()
    (root / "global" / "node.exe").write_bytes(b"node")
    (root / "bin").mkdir()
    system_path = environment["PATH"]
    paths.configure_windows_npm_environment()
    assert environment["NPM_CONFIG_PREFIX"] == str(root / "global")
    assert environment["PATH"].split(os.pathsep) == [str(root / "bin"), system_path, str(root / "global")]
    # Repeated process initialization is idempotent.
    environment["PATH"] = str(root / "bundled-node") + os.pathsep + environment["PATH"]
    paths.configure_windows_npm_environment()
    assert environment["PATH"].split(os.pathsep).count(str(root / "bin")) == 1
    assert environment["PATH"].split(os.pathsep)[0] == str(root / "bin")


@pytest.mark.parametrize("source", ["environment", "npmrc"])
def test_custom_external_prefix_is_preserved(windows_environment, tmp_path, source):
    root, environment = windows_environment
    (root / "global").mkdir()
    (root / "global" / "node.exe").touch()
    custom = str(tmp_path / "custom tools")
    original_path = environment["PATH"]
    if source == "environment":
        environment["NPM_CONFIG_PREFIX"] = custom
    else:
        config = tmp_path / "custom.npmrc"
        config.write_text("# keep custom prefix\nprefix  =  " + custom + "\n")
        environment["NPM_CONFIG_USERCONFIG"] = str(config)
    paths.configure_windows_npm_environment()
    assert environment["PATH"] == original_path
    assert environment.get("NPM_CONFIG_PREFIX") == (custom if source == "environment" else None)


def test_prefix_inside_electron_install_is_redirected(windows_environment, tmp_path, monkeypatch):
    root, environment = windows_environment
    (root / "global").mkdir()
    (root / "global" / "node.exe").touch()
    app = tmp_path / "app"
    monkeypatch.setattr(paths, "sys", SimpleNamespace(executable=str(app / "resources" / "backend" / "workswarm.exe")))
    environment["NPM_CONFIG_PREFIX"] = str(app / "runtime" / "node-runtime")
    paths.configure_windows_npm_environment()
    assert environment["NPM_CONFIG_PREFIX"] == str(root / "global")


def test_legacy_install_prefix_is_redirected_and_new_generation_has_priority(windows_environment, tmp_path):
    root, environment = windows_environment
    (root / "global").mkdir()
    (root / "global" / "node.exe").touch()
    old = tmp_path / "former Program Files install" / "runtime" / "node-runtime"
    generations = []
    for index in range(2):
        active = root / "installed" / str(index) / "prefix"
        active.mkdir(parents=True)
        saved = root / "preserved" / str(index) / "mapping.json"
        saved.parent.mkdir(parents=True)
        saved.write_text(json.dumps({"schema": 1, "source": str(old), "target": str(active)}))
        os.utime(saved, (100 + index, 100 + index))
        generations.append(str(active))
    environment["PATH"] = os.pathsep.join(["before", generations[0], "after"])
    environment["NPM_CONFIG_PREFIX"] = str(old)
    paths.configure_windows_npm_environment()
    assert environment["NPM_CONFIG_PREFIX"] == str(root / "global")
    assert environment["PATH"].split(os.pathsep)[:5] == ["before", str(root / "global"), generations[1], generations[0], "after"]


def test_new_global_command_wins_over_preserved_copy_without_shadowing_bundled_node(windows_environment, tmp_path):
    root, environment = windows_environment
    bundled = Path(environment["PATH"])
    bundled.mkdir()
    (bundled / "node.exe").write_bytes(b"bundled node")
    prefix = root / "global"
    prefix.mkdir()
    (prefix / "node.exe").write_bytes(b"pinned node")
    (prefix / "example.cmd").write_text("@echo new global version\n")
    active = root / "installed" / "snapshot" / "prefix"
    active.mkdir(parents=True)
    (active / "example.cmd").write_text("@echo old preserved version\n")
    # POSIX shutil.which requires executable fixtures even for Windows filenames.
    for command in (bundled / "node.exe", prefix / "node.exe", prefix / "example.cmd", active / "example.cmd"):
        command.chmod(command.stat().st_mode | 0o111)
    mapping = root / "preserved" / "snapshot" / "mapping.json"
    mapping.parent.mkdir(parents=True)
    mapping.write_text(json.dumps({"schema": 1, "source": str(tmp_path / "old"), "target": str(active)}))
    environment["PATH"] += os.pathsep + str(active) + os.pathsep + str(prefix)
    paths.configure_windows_npm_environment()
    assert shutil.which("example.cmd", path=environment["PATH"]) == str(prefix / "example.cmd")
    assert shutil.which("node.exe", path=environment["PATH"]) == str(bundled / "node.exe")
    expected = environment["PATH"]
    paths.configure_windows_npm_environment()
    assert environment["PATH"] == expected


@pytest.fixture
def preservation_runner(tmp_path):
    if sys.platform != "win32":
        pytest.skip("requires Windows PowerShell")
    powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    # Run the real script with only its registry handle replaced by an in-memory
    # object. No test writes HKCU Environment or touches the real user prefix.
    fake_registry = """
$script:registry=[pscustomobject]@{value=$env:TEST_ORIGINAL_PATH}
$script:registry | Add-Member ScriptMethod GetValue { param($n,$d,$o) $this.value }
$script:registry | Add-Member ScriptMethod GetValueNames { @('Path') }
$script:registry | Add-Member ScriptMethod GetValueKind { param($n) [Microsoft.Win32.RegistryValueKind]::ExpandString }
$script:registry | Add-Member ScriptMethod SetValue { param($n,$v,$k) $this.value=$v; [IO.File]::WriteAllText($env:TEST_PATH_RESULT,$v) }
$script:registry | Add-Member ScriptMethod Dispose { }
"""
    text = SCRIPT.read_text(encoding="utf-8")
    text = text.replace("$ErrorActionPreference = 'Stop'", "$ErrorActionPreference = 'Stop'\n" + fake_registry)
    registry_call = "[Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')"
    assert text.count(registry_call) == 1
    text = text.replace(registry_call, "$script:registry")
    script = tmp_path / "preserve.ps1"
    script.write_text(text, encoding="utf-8")
    data = tmp_path / "user data 测试"
    report = tmp_path / "report.json"
    path_result = tmp_path / "user-path.txt"

    def run(app, *, mode="Preserve", original_path="%USERPROFILE%\\tools;;C:\\existing;C:\\existing", data_dir=None):
        env = os.environ.copy()
        env.pop("PSModulePath", None)
        env["JIUWENSWARM_DATA_DIR"] = str(data_dir or data)
        env["TEST_ORIGINAL_PATH"] = original_path
        env["TEST_PATH_RESULT"] = str(path_result)
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-InstallDir", str(app), "-Mode", mode, "-ReportPath", str(report)],
            env=env, capture_output=True, text=True, errors="replace", timeout=60,
        )
        return completed, report, data / "runtime" / "npm", path_result

    run.script = script
    return run


def _prefix(app, layout="runtime/node-runtime"):
    prefix = app / layout
    (prefix / "node_modules" / "npm" / "bin").mkdir(parents=True)
    (prefix / "node.exe").write_bytes(b"fixture node")
    (prefix / "node_modules" / "npm" / "bin" / "npm-cli.js").write_text("npm fixture")
    (prefix / "node_modules" / "npm" / "bin" / "npx-cli.js").write_text("npx fixture")
    (prefix / "node_modules" / "shared-dependency").mkdir()
    (prefix / "node_modules" / "shared-dependency" / "index.js").write_text("module.exports='dependency retained'")
    (prefix / "node_modules" / "@example" / "cli").mkdir(parents=True)
    (prefix / "node_modules" / "@example" / "cli" / "index.js").write_text("console.log(require('shared-dependency'))")
    (prefix / "node_modules" / ".bin").mkdir()
    (prefix / "node_modules" / ".bin" / "example.cmd").write_text(
        '@echo off\n"%~dp0..\\..\\node.exe" "%~dp0..\\@example\\cli\\index.js" %*\n'
    )
    (prefix / "example.cmd").write_text(
        '@echo off\n"%~dp0node.exe" "%~dp0node_modules\\@example\\cli\\index.js" %*\n'
    )
    (prefix / "unknown-user-file").write_text("must not be dropped")
    return prefix


@pytest.mark.parametrize("layout", ["runtime/external-cli", "resources/backend/runtime/external-cli"])
def test_sdk_preservation_is_complete_stable_and_repeatable(tmp_path, preservation_runner, layout):
    app = tmp_path / "old app"
    source = app / layout / "claude" / "site-packages"
    (source / "claude_agent_sdk" / "_bundled").mkdir(parents=True)
    (source / "claude_agent_sdk" / "_bundled" / "claude.exe").write_bytes(b"native fixture")
    (source / ".jiuwenswarm-runtime.json").write_text('{"agent":"claude"}')
    (source / "sdk.dist-info").mkdir()
    (source / "sdk.dist-info" / "METADATA").write_text("vendor metadata")
    result, report, npm, user_path = preservation_runner(app)
    assert result.returncode == 0, report.read_text(encoding="utf-8") + result.stderr
    record = json.loads(report.read_text(encoding="utf-8"))["sdk_mappings"][0]
    target = npm.parent / "external-cli" / "claude" / "site-packages"
    assert Path(record["target"]) == target
    assert (target / "sdk.dist-info" / "METADATA").read_text() == "vendor metadata"
    assert not user_path.exists(), "SDK imports must not modify user PATH"
    (target / "sdk.dist-info" / "METADATA").write_text("new stable installation")
    again, _, _, _ = preservation_runner(app)
    assert again.returncode == 0
    assert (target / "sdk.dist-info" / "METADATA").read_text() == "new stable installation"
    assert (Path(record["backup"]) / "sdk.dist-info" / "METADATA").read_text() == "vendor metadata"
    assert len(list((npm.parent / "external-cli-preserved").glob("*/manifest.json"))) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_upgrade_snapshot_rejects_invalid_ciphertext():
    import base64
    from jiuwenswarm.common import windows_upgrade_config

    with pytest.raises(ValueError):
        windows_upgrade_config._decrypt_snapshot("not valid base64!")
    with pytest.raises(OSError) as error:
        windows_upgrade_config._decrypt_snapshot(base64.b64encode(b"not a DPAPI blob").decode("ascii"))
    assert error.value.winerror != 0


@pytest.mark.parametrize("concurrent_change", [False, True])
def test_legacy_uninstaller_config_reset_is_recovered_without_overwriting_changes(tmp_path, preservation_runner, monkeypatch, concurrent_change):
    from jiuwenswarm.common import config, utils, windows_upgrade_config

    app = tmp_path / "old admin app"
    _, _, npm, _ = preservation_runner(app)
    data = npm.parent.parent
    config_file = data / "config/config.yaml"
    config_file.parent.mkdir()
    config_file.write_text(
        "# preserve user comments\nmodel:\n  api_key: fixture-secret-do-not-print\nmodes:\n  team:\n    jiuwen_team:\n      external_cli_agents:\n        claude:\n          enabled: true\n          cli_path: ''\n      external_transport:\n        port: 8765\n", encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", config_file)
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: data)
    original = config.load_yaml_round_trip(config_file)
    preserved, report, _, _ = preservation_runner(app, mode="PreserveConfig")
    assert preserved.returncode == 0, report.read_text(encoding="utf-8")
    pending = data / "runtime/upgrade-config/pending.json"
    assert "fixture-secret" not in pending.read_text(encoding="utf-8")
    repeated, report, _, _ = preservation_runner(app, mode="PreserveConfig")
    assert repeated.returncode == 0, report.read_text(encoding="utf-8")
    # Exercise the real old backend reset operation, without launching an app.
    config.reset_external_cli_agents_in_config()
    if concurrent_change:
        with config_file.open("a", encoding="utf-8") as stream:
            stream.write("new_setting: user change\n")
        before = config_file.read_bytes()
        with pytest.raises(RuntimeError, match="Configuration changed"):
            windows_upgrade_config.restore_legacy_upgrade_config()
        assert config_file.read_bytes() == before
        assert pending.is_file()
    else:
        windows_upgrade_config.restore_legacy_upgrade_config()
        assert config.load_yaml_round_trip(config_file) == original
        assert "# preserve user comments" in config_file.read_text(encoding="utf-8")
        assert not pending.exists()
        windows_upgrade_config.restore_legacy_upgrade_config()


def test_sdk_cli_path_mapping_requires_a_compatible_managed_target(windows_environment, tmp_path, monkeypatch):
    from jiuwenswarm.common import external_cli_runtime

    npm, _ = windows_environment
    source = tmp_path / "old/runtime/external-cli/claude/site-packages"
    target = npm.parent / "external-cli/claude/site-packages"
    executable = target / "claude_agent_sdk/_bundled/claude.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    record = npm.parent / "external-cli-preserved/snapshot/mapping.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"schema": 1, "source": str(source), "target": str(target)}))
    old = str(source / "claude_agent_sdk/_bundled/claude.exe")
    monkeypatch.setattr(external_cli_runtime, "get_external_cli_runtime_status", lambda _name: {"runtime_state": "current"})
    assert paths.resolve_preserved_cli_path(old) == str(executable)
    monkeypatch.setattr(external_cli_runtime, "get_external_cli_runtime_status", lambda _name: {"runtime_state": "update_required"})
    assert paths.resolve_preserved_cli_path(old) == old


@pytest.mark.parametrize("kind", ["npm", "sdk"])
def test_settings_detect_and_enable_a_migrated_explicit_cli(windows_environment, tmp_path, monkeypatch, kind):
    from copy import deepcopy
    from jiuwenswarm.common import custom_agent_runtime, external_cli_runtime
    from jiuwenswarm.common.config_panel import config_set_handlers as handlers

    npm, _ = windows_environment
    monkeypatch.setattr(custom_agent_runtime, "custom_agents_root", lambda: tmp_path / "custom-agents")
    if kind == "sdk":
        source = tmp_path / "old/runtime/external-cli/claude/site-packages"
        target = npm.parent / "external-cli/claude/site-packages"
        record = npm.parent / "external-cli-preserved/snapshot/mapping.json"
        monkeypatch.setattr(external_cli_runtime, "get_external_cli_runtime_status", lambda _name: {"runtime_state": "current"})
    else:
        source = tmp_path / "old/runtime/node-runtime"
        target = npm / "installed/snapshot/prefix"
        record = npm / "preserved/snapshot/mapping.json"
    executable = target / "claude.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"schema": 1, "source": str(source), "target": str(target)}))
    requested = str(source / "claude.exe")
    raw = {"modes": {"team": {"jiuwen_team": {"external_cli_agents": [{"cli_agent": "claude", "cli_path": requested}]}}}}
    original = deepcopy(raw)
    probes = []

    def version(_agent, executable_path):
        probes.append(executable_path)
        return "2.1.999", ""

    monkeypatch.setattr(handlers, "run_external_cli_version_command", version)
    flat = handlers.flatten_external_cli_agents_for_config_panel(raw)
    assert flat["external_cli_agent_claude_cli_path"] == requested
    detected = handlers.detect_external_cli_agent("claude", requested)
    assert detected["status"] == "ok"
    assert detected["path"] == str(executable)
    enabled = handlers.external_cli_agents_from_switches(raw, {"external_cli_agent_claude_enabled": "true"})
    assert enabled == [{"cli_agent": "claude", "cli_path": str(executable)}]
    assert probes == [str(executable), str(executable)]
    assert raw == original


@pytest.mark.parametrize("layout", ["runtime/external-cli", "resources/backend/runtime/external-cli"])
@pytest.mark.parametrize("process_kind", ["setup", "app", "sdk-installer"])
def test_sdk_busy_check_distinguishes_setup_target_from_runtime_use(tmp_path, preservation_runner, monkeypatch, layout, process_kind):
    app = tmp_path / "old app"
    packages = app / layout / "claude/site-packages"
    packages.mkdir(parents=True)
    (packages / "sdk.py").write_text("fixture")
    monkeypatch.setenv("TEST_APP_DIRECTORY", str(app))
    monkeypatch.setenv("TEST_SDK_PACKAGES", str(packages))
    monkeypatch.setenv("TEST_PROCESS_KIND", process_kind)
    process_fixture = r'''
function Get-CimInstance {
    param([string]$ClassName)
    $executable='C:\Downloads\setup.exe'
    $command='/VERYSILENT /DIR="'+$env:TEST_APP_DIRECTORY+'"'
    if ($env:TEST_PROCESS_KIND -eq 'app') {
        $executable=Join-Path $env:TEST_APP_DIRECTORY 'workswarm.exe'
        $command='--desktop-run-agent'
    } elseif ($env:TEST_PROCESS_KIND -eq 'sdk-installer') {
        $executable='C:\Python\python.exe'
        $command='pip install --target "'+$env:TEST_SDK_PACKAGES+'"'
    }
    [pscustomobject]@{ProcessId=($PID+10000); ExecutablePath=$executable; CommandLine=$command}
}
'''
    script = preservation_runner.script
    original = script.read_text(encoding="utf-8")
    script.write_text(original.replace("$safeRoot=$false", "$safeRoot=$false\n" + process_fixture), encoding="utf-8")
    result, report, _, _ = preservation_runner(app)
    if process_kind == "setup":
        assert result.returncode == 0, report.read_text(encoding="utf-8")
        assert len(json.loads(report.read_text(encoding="utf-8"))["sdk_mappings"]) == 1
    else:
        assert result.returncode != 0
        assert "Close the CLI process" in report.read_text(encoding="utf-8")
        assert (packages / "sdk.py").is_file()


def test_late_source_install_blocks_cleanup_after_working_copy(tmp_path, preservation_runner):
    app = tmp_path / "app"
    source = _prefix(app)
    script = preservation_runner.script
    original = script.read_text(encoding="utf-8")
    copy = "Copy-Tree $prefix (Join-Path $stage 'prefix')"
    assert original.count(copy) == 1
    script.write_text(original.replace(copy, copy + "\n[IO.File]::WriteAllText((Join-Path $Source 'late-install'),'new user install')"), encoding="utf-8")
    result, report, _, user_path = preservation_runner(app)
    assert result.returncode != 0
    assert "Source changed before cleanup" in report.read_text(encoding="utf-8")
    assert (source / "late-install").read_text() == "new user install"
    assert not user_path.exists()


def test_mcp_command_arguments_and_cwd_resolve_without_rewriting_config(windows_environment, tmp_path):
    from copy import deepcopy
    from jiuwenswarm.common.mcp_config import build_mcp_server_config

    root, _ = windows_environment
    source = tmp_path / "old/runtime/node-runtime"
    active = root / "installed/snapshot/prefix"
    active.mkdir(parents=True)
    (active / "codegraph.cmd").touch()
    record = root / "preserved/snapshot/mapping.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"schema": 1, "source": str(source), "target": str(active)}))
    entry = {"name": "codegraph", "transport": "stdio", "command": "cmd.exe", "args": ["/d", "/c", f'"{source / "codegraph.cmd"}"', "serve", "--mcp"], "cwd": str(source)}
    original = deepcopy(entry)
    built = build_mcp_server_config(entry)
    assert built.params["args"][2] == f'"{active / "codegraph.cmd"}"'
    assert built.params["cwd"] == str(active)
    entry["command"] = str(source / "codegraph.cmd")
    assert build_mcp_server_config(entry).params["command"] == str(active / "codegraph.cmd")
    entry["command"] = "cmd.exe"
    assert entry == original


@pytest.mark.parametrize("layout", ["runtime/node-runtime", "resources/backend/runtime/node-runtime"])
def test_preservation_retains_complete_prefix_and_is_repeatable(tmp_path, preservation_runner, layout):
    app = tmp_path / "app with spaces"
    source = _prefix(app, layout)
    result, report, root, path_result = preservation_runner(app, original_path=f"%USERPROFILE%\\tools;;{source};C:\\existing;C:\\existing")
    assert result.returncode == 0, report.read_text(encoding="utf-8") + result.stderr
    data = json.loads(report.read_text(encoding="utf-8"))
    target = Path(data["mappings"][0]["target"])
    assert (target / "unknown-user-file").read_text() == "must not be dropped"
    assert (target / "node_modules/shared-dependency/index.js").is_file()
    assert (target / "node_modules/.bin/example.cmd").is_file()
    assert (target / "node_modules/.bin/node.exe").read_bytes() == (source / "node.exe").read_bytes()
    assert not (Path(data["mappings"][0]["backup"]) / "node_modules/.bin/node.exe").exists()
    assert (source / "unknown-user-file").is_file()
    user_path = path_result.read_text(encoding="utf-8")
    assert user_path.startswith(f"%USERPROFILE%\\tools;;{target};C:\\existing;C:\\existing")
    assert str(target / "node_modules" / ".bin") in user_path
    again, _, _, _ = preservation_runner(app, original_path=user_path)
    assert again.returncode == 0
    assert len(list((root / "preserved").glob("*/manifest.json"))) == 1
    assert path_result.read_text(encoding="utf-8") == user_path


def test_corrupt_preservation_blocks_cleanup_without_overwriting(tmp_path, preservation_runner):
    app = tmp_path / "app"
    source = _prefix(app)
    first, report, _, _ = preservation_runner(app)
    assert first.returncode == 0
    mapping = json.loads(report.read_text(encoding="utf-8"))["mappings"][0]
    target = Path(mapping["backup"])
    (target / "unknown-user-file").write_text("user edited preserved copy")
    again, report, _, _ = preservation_runner(app)
    assert again.returncode != 0
    assert "incomplete or changed" in report.read_text(encoding="utf-8")
    assert (source / "unknown-user-file").read_text() == "must not be dropped"
    assert (target / "unknown-user-file").read_text() == "user edited preserved copy"


def test_cli_self_update_never_changes_recovery_copy(tmp_path, preservation_runner):
    app = tmp_path / "app"
    _prefix(app)
    first, report, _, _ = preservation_runner(app)
    assert first.returncode == 0
    mapping = json.loads(report.read_text(encoding="utf-8"))["mappings"][0]
    active = Path(mapping["target"])
    backup = Path(mapping["backup"])
    (active / "unknown-user-file").write_text("updated working installation")
    again, _, _, _ = preservation_runner(app)
    assert again.returncode == 0
    assert (active / "unknown-user-file").read_text() == "updated working installation"
    assert (backup / "unknown-user-file").read_text() == "must not be dropped"


def test_recovery_root_replaced_by_a_junction_is_not_accepted(tmp_path, preservation_runner):
    import _winapi

    app = tmp_path / "app"
    source = _prefix(app)
    first, report, _, _ = preservation_runner(app)
    assert first.returncode == 0
    backup = Path(json.loads(report.read_text(encoding="utf-8"))["mappings"][0]["backup"])
    assert backup.resolve().is_relative_to(tmp_path.resolve())
    backup.rename(backup.with_name("original-prefix"))
    _winapi.CreateJunction(str(source), str(backup))
    again, report, _, _ = preservation_runner(app)
    assert again.returncode != 0
    assert "Linked path" in report.read_text(encoding="utf-8")
    assert (source / "unknown-user-file").read_text() == "must not be dropped"


def test_later_source_installs_get_their_own_working_copy_and_path_priority(tmp_path, preservation_runner):
    app = tmp_path / "app"
    source = _prefix(app)
    first, report, root, path_result = preservation_runner(app, original_path=f"before;{source};after")
    assert first.returncode == 0
    first_target = Path(json.loads(report.read_text(encoding="utf-8"))["mappings"][0]["target"])
    (first_target / "working-only.cmd").write_text("self update must survive")
    first_path = path_result.read_text(encoding="utf-8")
    (source / "newly-installed.cmd").write_text("later user install")
    second, report, _, _ = preservation_runner(app, original_path=first_path)
    assert second.returncode == 0, report.read_text(encoding="utf-8")
    second_target = Path(json.loads(report.read_text(encoding="utf-8"))["mappings"][0]["target"])
    assert second_target != first_target
    assert (second_target / "newly-installed.cmd").read_text() == "later user install"
    assert (first_target / "working-only.cmd").read_text() == "self update must survive"
    second_path = path_result.read_text(encoding="utf-8")
    assert second_path.startswith(f"before;{second_target};{first_target};after")
    assert len(list((root / "preserved").glob("*/manifest.json"))) == 2
    again, _, _, _ = preservation_runner(app, original_path=second_path)
    assert again.returncode == 0
    assert path_result.read_text(encoding="utf-8") == second_path


def test_long_nested_dependencies_are_not_omitted(tmp_path, preservation_runner):
    app = tmp_path / "app"
    source = _prefix(app)
    relative = Path("node_modules") / ("deep-package-" * 7) / "node_modules" / ("nested-" * 10) / "payload.txt"
    extended = Path("\\\\?\\" + str(source / relative))
    extended.parent.mkdir(parents=True)
    extended.write_text("long path dependency")
    first, report, _, _ = preservation_runner(app)
    assert first.returncode == 0, report.read_text(encoding="utf-8")
    mapping = json.loads(report.read_text(encoding="utf-8"))["mappings"][0]
    for key in ("target", "backup"):
        copied = Path("\\\\?\\" + str(Path(mapping[key]) / relative))
        assert copied.read_text() == "long path dependency"


def test_no_runtime_does_not_change_path(tmp_path, preservation_runner):
    app = tmp_path / "fresh install"
    app.mkdir()
    result, _, _, path_result = preservation_runner(app)
    assert result.returncode == 0
    assert not path_result.exists()


def test_initialize_prioritizes_global_commands_over_prior_layout_and_retries(tmp_path, preservation_runner):
    app = tmp_path / "app"
    source = _prefix(app)
    first, report, _, path_result = preservation_runner(app, original_path=f"before;{source};after")
    assert first.returncode == 0
    old = Path(json.loads(report.read_text(encoding="utf-8"))["mappings"][0]["target"])
    original_path = path_result.read_text(encoding="utf-8")
    # Exercise the real layout transition: Initialize only discovers the new
    # source, while user PATH still includes an older source's working prefix.
    source.rename(app / "removed-old-runtime")
    _prefix(app, "resources/backend/runtime/node-runtime")
    result, report, root, _ = preservation_runner(app, mode="Initialize", original_path=original_path)
    assert result.returncode == 0, report.read_text(encoding="utf-8")
    user_path = path_result.read_text(encoding="utf-8")
    entries = user_path.split(";")
    assert entries.index(str(root / "global")) < entries.index(str(old))
    assert entries.index("before") < entries.index("after")
    again, report, _, _ = preservation_runner(app, mode="Initialize", original_path=user_path)
    assert again.returncode == 0, report.read_text(encoding="utf-8")
    assert path_result.read_text(encoding="utf-8") == user_path


def test_backup_inside_installation_is_rejected(tmp_path, preservation_runner):
    app = tmp_path / "app"
    source = _prefix(app)
    result, report, _, _ = preservation_runner(app, data_dir=app / "resources" / "user-data")
    assert result.returncode != 0
    assert "outside" in report.read_text(encoding="utf-8")
    assert (source / "node.exe").is_file()


def test_junction_requires_manual_preservation(tmp_path, preservation_runner):
    import _winapi

    app = tmp_path / "app"
    source = _prefix(app)
    external = tmp_path / "outside"
    external.mkdir()
    (external / "keep.txt").write_text("outside source")
    _winapi.CreateJunction(str(external), str(source / "node_modules" / "linked"))
    result, report, _, _ = preservation_runner(app)
    assert result.returncode != 0
    assert "Linked content" in report.read_text(encoding="utf-8")
    assert (external / "keep.txt").read_text() == "outside source"


def test_source_change_during_copy_blocks_publication(tmp_path, preservation_runner):
    app = tmp_path / "app"
    source = _prefix(app)
    text = preservation_runner.script.read_text(encoding="utf-8")
    text = text.replace(
        "Copy-Tree $Source $copy\n",
        "Copy-Tree $Source $copy\n        [IO.File]::AppendAllText((Join-Path $Source 'unknown-user-file'),' concurrent update')\n",
    )
    preservation_runner.script.write_text(text, encoding="utf-8")
    result, report, root, path_result = preservation_runner(app)
    assert result.returncode != 0
    assert "Source changed" in report.read_text(encoding="utf-8")
    assert "concurrent update" in (source / "unknown-user-file").read_text()
    assert not list((root / "preserved").glob("*/manifest.json"))
    assert not path_result.exists()


def test_failed_copy_keeps_source_and_incomplete_staging_for_retry(tmp_path, preservation_runner):
    app = tmp_path / "app"
    source = _prefix(app)
    text = preservation_runner.script.read_text(encoding="utf-8")
    preservation_runner.script.write_text(text.replace(
        "Copy-Tree $Source $copy\n",
        "Copy-Tree $Source $copy\n        throw 'simulated disk write failure'\n",
    ), encoding="utf-8")
    result, report, root, path_result = preservation_runner(app)
    assert result.returncode != 0
    assert "simulated disk write failure" in report.read_text(encoding="utf-8")
    assert (source / "unknown-user-file").read_text() == "must not be dropped"
    pending = list((root / "preserved").glob(".pending-*"))
    assert len(pending) == 1
    (pending[0] / "mapping.json").write_text("incomplete record must not be activated")
    assert not path_result.exists()
    preservation_runner.script.write_text(text, encoding="utf-8")
    result, report, _, _ = preservation_runner(app)
    assert result.returncode == 0, report.read_text(encoding="utf-8")
    assert (pending[0] / "prefix" / "unknown-user-file").is_file()


def test_busy_cli_blocks_upgrade_without_stopping_it(tmp_path, preservation_runner):
    import time

    node = shutil.which("node")
    if not node:
        pytest.skip("requires Node")
    app = tmp_path / "app"
    source = _prefix(app)
    shutil.copyfile(node, source / "node.exe")
    ready = tmp_path / "ready.txt"
    process = subprocess.Popen([
        str(source / "node.exe"), "-e",
        "require('fs').writeFileSync(process.argv[1],'ready');setInterval(()=>{},1000)", str(ready),
    ])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        result, report, _, path_result = preservation_runner(app)
        assert result.returncode != 0
        assert "Close the CLI process" in report.read_text(encoding="utf-8")
        assert process.poll() is None
        assert not path_result.exists()
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_pinned_runtime_and_migrated_dependency_work_after_old_app_is_removed(tmp_path, preservation_runner):
    node = shutil.which("node")
    if not node:
        pytest.skip("requires Node for end-to-end launcher verification")
    app = tmp_path / "app"
    source = _prefix(app)
    shutil.copyfile(node, source / "node.exe")
    (source / "node_modules/npm/bin/npm-cli.js").write_text("require('fs').writeFileSync(process.env.TEST_NPM_RESULT,JSON.stringify({prefix:process.env.NPM_CONFIG_PREFIX,node:process.execPath}))")
    result, report, root, _ = preservation_runner(app, mode="Initialize")
    assert result.returncode == 0, report.read_text(encoding="utf-8")
    target = Path(json.loads(report.read_text(encoding="utf-8"))["mappings"][0]["target"])
    # Delete only this test's synthetic installation, after checking containment.
    assert app.resolve().is_relative_to(tmp_path.resolve())
    shutil.rmtree(app)
    run = subprocess.run([str(target / "node.exe"), str(target / "node_modules/@example/cli/index.js")], capture_output=True, text=True, timeout=15)
    assert run.returncode == 0 and "dependency retained" in run.stdout
    env = os.environ.copy()
    env["NPM_CONFIG_PREFIX"] = str(root / "global")
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    env["TEST_NPM_RESULT"] = str(tmp_path / "npm-result.json")
    for launcher in (target / "example.cmd", target / "node_modules/.bin/example.cmd"):
        env["TEST_CLI_LAUNCHER"] = str(launcher)
        run = subprocess.run(
            f'"{os.environ["COMSPEC"]}" /d /c call "%TEST_CLI_LAUNCHER%"',
            env=env, capture_output=True, text=True, timeout=15,
        )
        assert run.returncode == 0 and "dependency retained" in run.stdout, run.stderr
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env["PATH"] = str(root / "bin") + os.pathsep + env["PATH"]
    run = subprocess.run([
        str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Restricted", "-Command",
        "$ErrorActionPreference='Stop'; npm --version; exit $LASTEXITCODE",
    ], env=env, capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    actual = json.loads((tmp_path / "npm-result.json").read_text(encoding="utf-8"))
    assert actual["prefix"] == str(root / "global")
    assert Path(actual["node"]) == root / "global" / "node.exe"


def test_installers_preserve_before_delete_and_old_uninstall():
    for name in ("installer.iss", "installer-electron.iss"):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert '#include "windows-cli-preservation.iss"' in source
        assert 'Source: ' in source and 'preserve-windows-cli.ps1"; Flags: dontcopy' in source
        assert "PreserveWindowsCli(ExpandConstant('{app}'), 'Preserve')" in source
        assert "PreserveWindowsCli(ExpandConstant('{app}'), 'Initialize')" in source
        last_uninstall = source.index("if not ShellExec('runas'")
        assert source.rfind("'Preserve')", 0, last_uninstall) > source.rfind("MsgBox(", 0, last_uninstall)
        assert 'Name: "{app}\\resources"' in source
        assert "ChangesEnvironment=yes" in source


@pytest.mark.skipif(sys.platform != "win32", reason="requires the Windows installer compiler")
@pytest.mark.parametrize("name", ["installer.iss", "installer-electron.iss"])
def test_inno_compiles_preservation_hooks(tmp_path, monkeypatch, name):
    compiler = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Inno Setup 6/ISCC.exe"
    if not compiler.is_file():
        pytest.skip("Inno Setup is not installed")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    import platform

    assert platform.machine() == "AMD64"
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    for filename in (name, "windows-cli-preservation.iss", "preserve-windows-cli.ps1"):
        shutil.copyfile(ROOT / "scripts" / filename, scripts / filename)
    icon = project / "jiuwenswarm/channels/web/frontend/public/logo.ico"
    icon.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "jiuwenswarm/channels/web/frontend/public/logo.ico", icon)
    for dist in ("workswarm", "WorkSwarm-Electron"):
        payload = project / "dist" / dist
        payload.mkdir(parents=True)
        (payload / "fixture.txt").write_text("compile-only fixture; never installed")
    result = subprocess.run(
        [str(compiler), "/Qp", f"/O{tmp_path / 'compiled'}", "/DBuildDisplayName=WorkSwarm", "/DBuildVersion=0.0.0", "/DBuildExecutableNameWindows=workswarm.exe", "/DBuildDistDirName=workswarm", "/DBuildSetupBaseName=issue5338-test", str(scripts / name)],
        cwd=scripts, capture_output=True, text=True, errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list((tmp_path / "compiled").glob("*.exe"))
