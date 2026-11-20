"""Exercise the actual PowerShell signing entry point without a release certificate."""

from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "win32", reason="executes Windows PowerShell signing orchestration")
@pytest.mark.parametrize("scenario", [
    "healthy", "timestamp", "unsigned", "exhausted", "certificate", "unknown",
    "mixed_errors", "invalid", "wrong_certificate", "warning", "forced", "completed_error",
    "vendor", "recovery_invalid", "recovery_certificate", "recovery_warning",
    "unsigned_exhausted", "timestamp_certificate", "recovery_timestamp_certificate",
    "forced_retry", "forced_exhausted", "forced_changed_wrong", "forced_timestamp",
])
def test_signing_recovers_only_unfinished_files(tmp_path, scenario):
    script = tmp_path / "signing.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"$scenario = '{scenario}'\n"
        f"$source = '{(ROOT / 'scripts/build-electron-exe.ps1').as_posix()}'\n"
        + r'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors -join "`n") }
$names = @('Invoke-CodeSigning', 'Invoke-PESigningCommand', 'Repair-PESigningBatch',
    'Get-PESigningState', 'Invoke-SignToolBatchVerification')
foreach ($name in $names) {
    $definition = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    Invoke-Expression $definition.Extent.Text
}
$script:states = @{}
$script:signs = @{}
$script:stamps = @{}
$script:delays = @()
$script:verifies = 0
$script:failedOnce = $false
$root = Join-Path $PSScriptRoot 'payload'
New-Item -ItemType Directory $root | Out-Null
foreach ($index in 1..20) {
    $path = Join-Path $root ('{0:d2}.exe' -f $index)
    New-Item -ItemType File $path | Out-Null
    $script:states[$path] = 'unsigned'
    $script:signs[$path] = 0
    $script:stamps[$path] = 0
}
$last = Join-Path $root '20.exe'
if ($scenario -like 'forced*' -or $scenario -eq 'vendor') { $script:states[$last] = 'vendor' }
function Start-Sleep { param($Seconds) $script:delays += $Seconds }
function Test-PortableExecutable { param($Path) return $true }
function Test-EmbeddedAuthenticodeSignature { param($Path) return $script:states[$Path] -ne 'unsigned' }
function Get-EmbeddedSigningThumbprint {
    param($Path)
    if ($scenario -eq 'wrong_certificate' -or
        ($scenario -eq 'recovery_certificate' -and $Path -eq $last)) { return 'wrong' }
    if ($script:states[$Path] -eq 'vendor') { return 'vendor' }
    return 'expected'
}
function Mock-SignTool {
    $command = $args[0]
    $paths = @($args | Where-Object { $_ -like '*.exe' })
    $code = 0
    if ($command -eq 'verify') {
        $script:verifies++
        foreach ($path in $paths) {
            if ($script:states[$path] -eq 'unsigned' -or $scenario -eq 'invalid') { $code = 1; break }
            if ($scenario -eq 'warning') { $code = 2; break }
            if ($path -eq $last -and $scenario -eq 'recovery_invalid') { $code = 1; break }
            if ($path -eq $last -and $scenario -eq 'recovery_warning') { $code = 2; break }
            if ('/tw' -in $args -and $script:states[$path] -ne 'complete') { $code = 2 }
        }
    } elseif ($command -eq 'sign') {
        foreach ($path in $paths) {
            $script:signs[$path]++
            if ($path -eq $last -and
                ($scenario -in @('unsigned_exhausted', 'forced_exhausted') -or
                ($scenario -in @('unsigned', 'certificate', 'unknown', 'mixed_errors', 'forced_retry', 'forced_changed_wrong') -and -not $script:failedOnce))) {
                $script:failedOnce = $true
                $code = 1
                switch ($scenario) {
                    'unsigned' { Write-Output 'SignTool Error: 0x800705B4' }
                    'unsigned_exhausted' { Write-Output 'SignTool Error: 0x800705B4' }
                    'certificate' { Write-Output 'SignTool Error: 0x800B0101' }
                    'unknown' { Write-Output 'SignTool Error: unknown failure' }
                    'mixed_errors' { Write-Output '0x800705B4 0x800B0101' }
                    { $_ -like 'forced*' } {
                        if ($scenario -eq 'forced_changed_wrong') { Add-Content -LiteralPath $path -Value 'changed signature' }
                        Write-Output 'SignTool Error: 0x800705B4'
                    }
                }
                break
            }
            if ($script:signs[$path] -gt 1 -and $script:states[$path] -notin @('unsigned', 'vendor')) {
                throw "Repeated signing: $path"
            }
            $script:states[$path] = 'timestamp'
            Add-Content -LiteralPath $path -Value 'release signature'
        }
    } elseif ($command -eq 'timestamp') {
        foreach ($path in $paths) {
            if ($script:states[$path] -eq 'complete') { throw "Repeated timestamp: $path" }
            $script:stamps[$path]++
            if ($path -eq $last -and ($scenario -eq 'timestamp_certificate' -or
                ($scenario -eq 'recovery_timestamp_certificate' -and $script:stamps[$path] -gt 1))) {
                $code = 1
                Write-Output 'SignTool Error: 0x800B0101'
                continue
            }
            if ($path -eq $last -and ($scenario -eq 'exhausted' -or
                $scenario -like 'recovery_*' -or
                ($scenario -in @('timestamp', 'forced_timestamp') -and $script:stamps[$path] -lt 2))) {
                $code = 1
                Write-Output 'Timestamp service unavailable'
            } else {
                $script:states[$path] = 'complete'
                if ($scenario -eq 'completed_error' -and $path -eq $last) { $code = 1 }
            }
        }
    } else { throw "Unexpected command: $command" }
    Set-Variable -Name LASTEXITCODE -Value $code -Scope 1
}
$failed = $false
try {
    $options = @{}
    if ($scenario -like 'forced*') { $options.ForceResignPaths = @($last) }
    Invoke-CodeSigning -Root $root -ResolvedSignTool 'Mock-SignTool' -CertificateThumbprint 'expected' `
        -TimestampServer 'test-server' @options
} catch { $failed = $true; Write-Host $_ }
$shouldFail = $scenario -like 'recovery_*' -or $scenario -in @('exhausted', 'unsigned_exhausted', 'timestamp_certificate', 'certificate', 'unknown', 'mixed_errors', 'invalid', 'wrong_certificate', 'warning', 'forced_exhausted', 'forced_changed_wrong')
if ($failed -ne $shouldFail) { throw "Unexpected failure status: $failed" }
$expectedDelays = switch ($scenario) {
    'unsigned' { '5' }
    'timestamp' { '5' }
    'completed_error' { '5' }
    'exhausted' { '5,15' }
    'unsigned_exhausted' { '5,15' }
    'forced_retry' { '5' }
    'forced_exhausted' { '5,15' }
    'forced_changed_wrong' { '5' }
    'forced_timestamp' { '5' }
    { $_ -like 'recovery_*' } { '5' }
    default { '' }
}
if (($script:delays -join ',') -ne $expectedDelays) { throw "Wrong delays: $script:delays" }
if ($scenario -eq 'unsigned' -and $script:signs[$last] -ne 2) { throw 'Unsigned file not retried' }
if ($scenario -eq 'timestamp' -and $script:stamps[$last] -ne 2) { throw 'Timestamp not retried' }
if ($scenario -eq 'exhausted' -and $script:stamps[$last] -ne 3) { throw 'Wrong retry limit' }
if ($scenario -eq 'unsigned_exhausted' -and $script:signs[$last] -ne 3) { throw 'Wrong signing retry limit' }
if ($scenario -eq 'forced_retry' -and $script:signs[$last] -ne 2) { throw 'Forced file was not retried' }
if ($scenario -eq 'forced_exhausted' -and $script:signs[$last] -ne 3) { throw 'Wrong forced retry limit' }
if ($scenario -eq 'forced_changed_wrong' -and $script:signs[$last] -ne 1) { throw 'Changed wrong certificate accepted' }
if ($scenario -eq 'forced_timestamp' -and ($script:signs[$last] -ne 1 -or $script:stamps[$last] -ne 2)) {
    throw 'Forced file was signed again after signature completion'
}
foreach ($path in $script:signs.Keys) {
    if ($path -ne $last -and $script:signs[$path] -ne 1) { throw 'Completed file was signed again' }
}
if ($scenario -eq 'healthy' -and $script:verifies -ne 1) { throw 'Redundant happy-path verification' }
if ($scenario -eq 'vendor' -and ($script:signs[$last] -ne 0 -or $script:stamps[$last] -ne 0)) {
    throw 'Vendor signature was changed'
}
Write-Output 'PASS'
''',
        encoding="utf-8",
    )
    result = subprocess.run(
        [shutil.which("pwsh") or "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="executes native Windows timestamp recovery")
@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.parametrize("native_error_preference", [True, False])
def test_native_timestamp_warning_can_be_recovered(tmp_path, shell, native_error_preference):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is unavailable")
    tool = tmp_path / "timestamp-tool.cmd"
    tool.write_text(
        '@echo off\nif "%1"=="timestamp" (\n'
        '  echo complete > "%~dp0complete.marker"\n  exit /b 0\n)\n'
        'if "%4"=="/tw" if not exist "%~dp0complete.marker" (\n'
        '  echo Warning: missing timestamp 1>&2\n  exit /b 2\n)\nexit /b 0\n',
        encoding="ascii",
    )
    script = tmp_path / "recover.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"$PSNativeCommandUseErrorActionPreference = ${str(native_error_preference).lower()}\n"
        f"$source = '{(ROOT / 'scripts/build-electron-exe.ps1').as_posix()}'\n"
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$null, [ref]$null)\n"
        "foreach ($name in @('Get-PESigningState', 'Repair-PESigningBatch', 'Invoke-PESigningCommand')) {\n"
        "$definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] "
        "-and $node.Name -eq $name }, $true)\nInvoke-Expression $definition.Extent.Text\n}\n"
        "function Test-EmbeddedAuthenticodeSignature { return $true }\n"
        "function Get-EmbeddedSigningThumbprint { return 'expected' }\n"
        "function Start-Sleep {}\n"
        f"$tool = '{tool.as_posix()}'\n"
        "$state = Get-PESigningState -Path 'payload.exe' -ResolvedSignTool $tool -CertificateThumbprint 'expected'\n"
        "if ($state -ne 'timestamp') { throw 'warning was not classified' }\n"
        "Repair-PESigningBatch -Paths @('payload.exe') -ResolvedSignTool $tool -CertificateThumbprint 'expected' -TimestampServer 'test'\n"
        "$state = Get-PESigningState -Path 'payload.exe' -ResolvedSignTool $tool -CertificateThumbprint 'expected'\n"
        "if ($state -ne 'complete') { throw 'timestamp was not recovered' }\n"
        f"if ($PSNativeCommandUseErrorActionPreference -ne ${str(native_error_preference).lower()} "
        "-or $ErrorActionPreference -ne 'Stop') { throw 'preferences leaked' }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="executes native Windows stderr capture")
@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.parametrize("error_code, retryable", [("800705B4", True), ("800B0101", False)])
def test_native_signing_errors_are_captured(tmp_path, shell, error_code, retryable):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is unavailable")
    tool = tmp_path / "fake-sign-tool.cmd"
    tool.write_text(f"@echo off\necho SignTool Error: 0x{error_code} 1>&2\nexit /b 1\n", encoding="ascii")
    script = tmp_path / "capture.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$PSNativeCommandUseErrorActionPreference = $true\n"
        f"$source = '{(ROOT / 'scripts/build-electron-exe.ps1').as_posix()}'\n"
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$null, [ref]$null)\n"
        "$definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] "
        "-and $node.Name -eq 'Invoke-PESigningCommand' }, $true)\n"
        "Invoke-Expression $definition.Extent.Text\n"
        f"$result = Invoke-PESigningCommand -ResolvedSignTool '{tool.as_posix()}' -Arguments @('sign')\n"
        f"if ($result.ExitCode -ne 1 -or $result.Retryable -ne ${str(retryable).lower()}) {{ throw 'bad capture' }}\n"
        "if ($ErrorActionPreference -ne 'Stop' -or -not $PSNativeCommandUseErrorActionPreference) { throw 'preferences leaked' }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
