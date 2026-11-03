"""Windows Electron frozen validation and upgrade ordering regressions."""

import ast
import logging
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "win32", reason="executes PowerShell installer discovery")
@pytest.mark.parametrize("scenario, expected", [
    ("existing", "compiler ready"),
    ("install", "compiler ready"),
    ("missing", "winget is unavailable"),
    ("failed", "winget exit code: 7"),
    ("absent", "ISCC.exe was not found"),
])
def test_inno_setup_bootstrap_fails_closed(tmp_path, scenario, expected):
    source = (ROOT / "scripts/build-electron-exe.ps1").read_text(encoding="utf-8")
    start = source.index("$IsccPaths = @(")
    fragment = source[start:source.index('Write-Host "Using ISCC: $Iscc"', start)]
    assert "Invoke-WebRequest" not in fragment
    assert "Start-Process" not in fragment
    script = tmp_path / "bootstrap.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"$scenario = '{scenario}'\n"
        "$script:installed = $scenario -eq 'existing'\n"
        "$script:called = $false\n"
        "function Test-Path { param($Path) return $script:installed }\n"
        "function Get-Command {\n"
        "param($Name, $CommandType, $ErrorAction)\n"
        "if ($Name -eq 'winget' -and $scenario -ne 'missing') {\n"
        "[pscustomobject]@{ Source = 'Invoke-TestWinget' }\n"
        "}\n}\n"
        "function Invoke-TestWinget {\n"
        "$script:called = $true\n"
        "$expected = 'install --id JRSoftware.InnoSetup --exact --version 6.7.1 --source winget --scope user --silent --disable-interactivity --accept-package-agreements --accept-source-agreements'\n"
        "if (($args -join ' ') -cne $expected) { throw 'unexpected installer arguments' }\n"
        "$script:installed = $scenario -ne 'absent'\n"
        "$global:LASTEXITCODE = if ($scenario -eq 'failed') { 7 } else { 0 }\n"
        "}\ntry {\n" + fragment +
        "\nif (-not $Iscc) { throw 'missing compiler' }\n"
        "if (($scenario -eq 'existing') -and $script:called) { throw 'unnecessary install' }\n"
        "if (($scenario -eq 'install') -and -not $script:called) { throw 'installer not invoked' }\n"
        "Write-Output 'compiler ready'\n"
        "} catch { Write-Output $_; exit 1 }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [shutil.which("pwsh") or "powershell", "-NoProfile", "-File", str(script)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == (0 if scenario in {"existing", "install"} else 1), result.stdout + result.stderr
    assert expected in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="executes a PowerShell build gate")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_gateway_build_gate_runs_frozen_exe_and_stops_on_failure(tmp_path, exit_code):
    source = (ROOT / "scripts/build-electron-exe.ps1").read_text(encoding="utf-8")
    start = source.index("    $GatewayVerifier =")
    end = source.index("    $A2UIVerifier =", start)
    assert source.index("if (-not $FrontendOnly) {", source.index("$ShellExeName =")) < start
    assert end < source.index("# ── 5.")
    script = tmp_path / "verify.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()\n"
        "$ProjectRoot = 'C:\\build with spaces'\n"
        "$FrozenExe = 'C:\\frozen backend\\workswarm.exe'\n"
        "$BuildErrorLogName = 'error.log'\n"
        "function Start-Process {\n"
        "param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru, [switch]$NoNewWindow)\n"
        "if ($FilePath -ne $FrozenExe -or -not $Wait -or -not $PassThru) { throw 'bad invocation' }\n"
        "if ($ArgumentList[0] -cne '\"C:\\build with spaces\\scripts\\verify_gateway_adapter_bundle.py\"') { throw 'unquoted verifier' }\n"
        f"[pscustomobject]@{{ ExitCode = {exit_code} }}\n"
        "}\ntry {\n" + source[start:end] +
        "\nWrite-Output 'gate passed'\n} catch { Write-Output $_; exit 1 }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [shutil.which("pwsh") or "powershell", "-NoProfile", "-File", str(script)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == (1 if exit_code else 0), result.stdout + result.stderr
    assert ("Frozen Gateway adapter bundle verification failed" if exit_code else "gate passed") in result.stdout


def test_only_legacy_admin_migration_waits_until_install_confirmation():
    source = (ROOT / "scripts/installer-electron.iss").read_text(encoding="utf-8")
    assert "function InitializeSetup" not in source
    prepare = source[source.index("function PrepareToInstall"):]
    assert "RunLegacyAdminUninstaller(AdminUninstaller)" in prepare
    assert "GetLegacyUserUninstaller" not in source
    assert "UserUninstaller" not in source
    assert "Result := '';" in prepare
    assert "Result := False" not in prepare
    admin = source[source.index("function RunLegacyAdminUninstaller("):source.index("function PrepareToInstall(")]
    assert admin.index("ShellExec('runas', ExePath") < admin.index("WaitForLegacyAdminUninstall(ExePath)")
    wait = source[source.index("function WaitForLegacyAdminUninstall("):source.index("function RunLegacyAdminUninstaller(")]
    assert "LegacyAdminUninstallEntryExists()" in wait
    assert "not FileExists(ExePath)" in wait


def test_legacy_user_upgrade_reuses_app_id_and_previous_install_location():
    old = (ROOT / "scripts/installer.iss").read_text(encoding="utf-8")
    electron = (ROOT / "scripts/installer-electron.iss").read_text(encoding="utf-8")
    old_app_id = next(line for line in old.splitlines() if line.startswith("AppId="))
    electron_app_id = next(line for line in electron.splitlines() if line.startswith("AppId="))
    assert electron_app_id == old_app_id
    assert "DefaultDirName={localappdata}\\Programs\\{#MyAppName}" in electron
    assert "UsePreviousAppDir=yes" in electron
    assert "LegacyUserInstallDir" not in electron


def test_electron_upgrade_replaces_stale_resources_and_matches_legacy_close_policy():
    old = (ROOT / "scripts/installer.iss").read_text(encoding="utf-8")
    electron = (ROOT / "scripts/installer-electron.iss").read_text(encoding="utf-8")
    assert 'Type: filesandordirs; Name: "{app}\\resources"' in old
    for app_path in ("resources", "_internal", "runtime"):
        assert (
            f'Type: filesandordirs; Name: "{{app}}\\{app_path}"; '
            "Check: IsExistingPerUserInstallDir"
        ) in electron
    cleanup_check = electron[
        electron.index("function IsExistingPerUserInstallDir("):
        electron.index("function TryFallbackUninstaller(")
    ]
    assert "TryRegUninstaller(HKCU" in cleanup_check
    assert "'InstallLocation'" in cleanup_check
    assert "ExpandConstant('{app}')" in cleanup_check
    assert "HKLM" not in cleanup_check
    assert "CloseApplications=yes" in old and "CloseApplications=yes" in electron


def test_normal_upgrade_does_not_run_uninstall_config_cleanup():
    old = (ROOT / "scripts/installer.iss").read_text(encoding="utf-8")
    electron = (ROOT / "scripts/installer-electron.iss").read_text(encoding="utf-8")
    assert '--desktop-reset-external-cli-config' in old
    assert "BackupLegacyUserConfig" not in electron
    assert "RestoreLegacyUserConfig" not in electron
    assert "WaitForLegacyUserUninstall" not in electron
    uninstall_run = electron[electron.index("[UninstallRun]"):electron.index("[Icons]")]
    old_uninstall_run = old[old.index("[UninstallRun]"):old.index("[Icons]")]
    assert 'Filename: "{app}\\resources\\backend\\{#BackendExecutableName}"' in uninstall_run
    assert 'Filename: "{app}\\{#MyAppExeName}"' not in uninstall_run
    assert '--desktop-reset-external-cli-config' in uninstall_run
    assert 'RunOnceId: "ResetExternalCliConfig"' in old_uninstall_run
    assert 'RunOnceId: "ResetExternalCliConfig"' in uninstall_run
    prepare = electron[electron.index("function PrepareToInstall"):]
    assert '--desktop-reset-external-cli-config' not in prepare


def test_windows_update_helper_waits_then_launches_interactive_installer(tmp_path):
    # Isolate the real helper function from optional GUI imports.
    source = (ROOT / "jiuwenswarm/channels/desktop/desktop_app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_launch_windows_installer_helper")
    events = []
    env = {
        "Path": Path, "logger": logging.getLogger(__name__),
        "BACKEND_PORT": 19000, "FRONTEND_PORT": 5173,
        "BACKEND_HOST": "127.0.0.1", "FRONTEND_HOST": "127.0.0.1",
        "wait_for_pid_exit": lambda pid: events.append(("parent", pid)),
        "_wait_for_port_release": lambda host, port, **kw: events.append(("port", port)),
        "subprocess": SimpleNamespace(DEVNULL=-3, Popen=lambda args, **kw: events.append(("launch", args, kw))),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "helper", "exec"), env)
    target = tmp_path / "WorkSwarm setup.exe"
    env[function.name](str(target), "WorkSwarm.exe", 123, 19001, 5174)
    assert events[:3] == [("parent", 123), ("port", 19001), ("port", 5174)]
    assert events[3] == ("launch", [str(target.resolve())], {"stdout": -3, "stderr": -3})
