# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Contracts for the shared Inno Setup compiler bootstrap used by Windows builds."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "scripts" / "build-inno-setup.psm1"
BUILD_EXE = ROOT / "scripts" / "build-exe.ps1"
BUILD_ELECTRON = ROOT / "scripts" / "build-electron-exe.ps1"


def test_shared_module_prefers_inno_setup_7_for_extended_length_paths() -> None:
    """Inno Setup 7 removes the MAX_PATH limit; version 6 stays a fallback."""
    source = MODULE.read_text(encoding="utf-8")
    resolver_start = source.index("function Resolve-InnoSetupCompiler")
    resolver_end = source.index("function Install-InnoSetup7", resolver_start)
    resolver = source[resolver_start:resolver_end]

    assert resolver.index(r"Inno Setup 7\ISCC.exe") < resolver.index(r"Inno Setup 6\ISCC.exe")
    # PATH is checked between the 7 and 6 default locations so a compiler from
    # PATH is not silently downgraded to an installed Inno Setup 6.
    assert resolver.index(r"Inno Setup 7\ISCC.exe") < resolver.index("Get-Command iscc")
    assert resolver.index("Get-Command iscc") < resolver.index(r"Inno Setup 6\ISCC.exe")
    assert "Export-ModuleMember" in source
    assert "function Get-InnoSetupMajorVersion" in source
    assert "function Test-InnoSetup7OrNewer" in source

    for script in (BUILD_EXE, BUILD_ELECTRON):
        text = script.read_text(encoding="utf-8")
        assert 'Import-Module (Join-Path $PSScriptRoot "build-inno-setup.psm1") -Force' in text
        assert "Resolve-InnoSetupCompiler" in text
        # The functions live only in the shared module.
        assert "function Resolve-InnoSetupCompiler" not in text
        assert "function Install-InnoSetup7" not in text
        # Version detection must not rely on the install directory name, which
        # misreports Inno Setup 7 exposed from a custom PATH location.
        assert "Test-InnoSetup7OrNewer -IsccPath $Iscc" in text
        assert "Split-Path -Leaf (Split-Path -Parent $Iscc)" not in text

    electron = BUILD_ELECTRON.read_text(encoding="utf-8")
    assert "Install-InnoSetup7" in electron
    assert "winget" not in electron.lower()
    assert "Inno Setup 7 was not detected" in electron


@pytest.mark.skipif(sys.platform != "win32", reason="executes PowerShell compiler discovery")
@pytest.mark.parametrize(
    "existing, path_iscc, expected",
    [
        # A compiler from PATH must not downgrade an installed Inno Setup 7.
        (
            [r"C:\Program Files\Inno Setup 7\ISCC.exe", r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"],
            r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
            r"C:\Program Files\Inno Setup 7\ISCC.exe",
        ),
        # Inno Setup 6 remains a supported fallback.
        (
            [r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"],
            "",
            r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        ),
        # A compiler exposed only through PATH is still used.
        ([], r"D:\tools\Inno Setup 7\ISCC.exe", r"D:\tools\Inno Setup 7\ISCC.exe"),
        ([], "", ""),
    ],
)
def test_resolve_inno_setup_compiler_supports_6_and_7(tmp_path, existing, path_iscc, expected) -> None:
    source = MODULE.read_text(encoding="utf-8")
    start = source.index("function Resolve-InnoSetupCompiler")
    fragment = source[start:source.index("function Install-InnoSetup7", start)]
    existing_literal = ", ".join(f"'{path}'" for path in existing)
    script = tmp_path / "resolve.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"$script:existing = @({existing_literal})\n"
        f"$script:pathIscc = '{path_iscc}'\n"
        "function Test-Path {\n"
        "param($LiteralPath)\n"
        "return ($script:existing -contains $LiteralPath) -or ($script:pathIscc -eq $LiteralPath)\n"
        "}\n"
        "function Get-Command {\n"
        "param($Name, $ErrorAction)\n"
        "if ($script:pathIscc) { [pscustomobject]@{ Source = $script:pathIscc } }\n"
        "}\n"
        + fragment
        + "\n$result = Resolve-InnoSetupCompiler\n"
        "if ($result) { Write-Output $result }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [shutil.which("pwsh") or "powershell", "-NoProfile", "-File", str(script)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.skipif(sys.platform != "win32", reason="executes PowerShell version detection")
@pytest.mark.parametrize(
    "output, expected_major, expected_is7",
    [
        # Inno Setup 7 supports --version and prints the bare version.
        ("7.1.1", "7", "True"),
        # Inno Setup 6 does not support --version and prints its banner.
        ("Inno Setup 6 Command-Line Compiler", "6", "False"),
        ("unparsable output", "null", "False"),
    ],
)
def test_inno_setup_version_detection_does_not_depend_on_install_path(
    tmp_path, output, expected_major, expected_is7
) -> None:
    source = MODULE.read_text(encoding="utf-8")
    start = source.index("function Get-InnoSetupMajorVersion")
    fragment = source[start:source.index("Export-ModuleMember", start)]

    compiler = tmp_path / "iscc.cmd"
    compiler.write_text(f"@echo {output}\r\n", encoding="utf-8")
    script = tmp_path / "version.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()\n"
        + fragment
        + f"\n$compiler = '{compiler}'\n"
        "$major = Get-InnoSetupMajorVersion -IsccPath $compiler\n"
        "if ($null -eq $major) { Write-Output 'major=null' } else { Write-Output ('major=' + $major) }\n"
        "Write-Output ('is7=' + (Test-InnoSetup7OrNewer -IsccPath $compiler))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [shutil.which("pwsh") or "powershell", "-NoProfile", "-File", str(script)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.strip().splitlines()
    assert f"major={expected_major}" in lines
    assert f"is7={expected_is7}" in lines


@pytest.mark.parametrize(
    "script, defines, invocation_end",
    [
        (BUILD_EXE, "$InnoDefines = @(", "& $Iscc @InnoArguments"),
        (BUILD_ELECTRON, "$IsccArguments = @(", "& $Iscc @IsccArguments"),
    ],
    ids=["build-exe", "build-electron-exe"],
)
def test_build_scripts_use_compiler_arguments_accepted_by_inno_setup_6_and_7(script, defines, invocation_end) -> None:
    """Inno Setup 7 prefers --define/--signtool, but /D and /S still work in both."""
    source = script.read_text(encoding="utf-8")
    invocation = source[source.index(defines):source.index(invocation_end)]

    assert "/D" in invocation
    assert '"/Scertumsha256=$InnoSignCommand"' in invocation
    assert "--define=" not in invocation
    assert "--signtool=" not in invocation


def test_shared_bootstrap_download_is_pinned_and_hash_verified_before_execution() -> None:
    """A missing compiler must not become an unverified download-and-run."""
    source = MODULE.read_text(encoding="utf-8")
    bootstrap = source[source.index("function Install-InnoSetup7"):]

    download = bootstrap.index("Invoke-WebRequest")
    verification = bootstrap.index("Get-FileHash")
    execution = bootstrap.index("Start-Process")
    assert download < verification < execution
    assert "-TimeoutSec 300" in bootstrap
    assert "Unable to download Inno Setup" in bootstrap
    assert "$ActualSha256 -ne $ExpectedSha256" in bootstrap
    assert "innosetup-7.1.1-x64.exe" in bootstrap
    assert "8a5a6711c3766a6e5fb74f280ba0e3551e719ce94538d901fdbd1f8f9920b3f0" in bootstrap
    assert "download.php/is.exe" not in source
    for script in (BUILD_EXE, BUILD_ELECTRON):
        assert "download.php/is.exe" not in script.read_text(encoding="utf-8")
