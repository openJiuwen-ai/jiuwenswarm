# Score every SkillsBench task once on its original skills. No rewrite.
#
# Usage:
#   .\.venv\Scripts\Activate.ps1
#   $env:SKILLSBENCH_ROOT = "<parent-of-tasks>"   # no built-in default
#   # Gateway already up: .\.venv\Scripts\jiuwenswarm-start.exe app
#   powershell -ExecutionPolicy Bypass -File .\scripts\skillsbench_baseline_all.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\skillsbench_baseline_all.ps1 -OutputDir .\outputs\skillsbench_baseline

param(
    [int]$ExecTimeout = 1800,
    [int]$Workers = 1,
    [string]$OutputDir = "",
    [ValidateSet("local", "docker")]
    [string]$VerifierMode = "local",
    [switch]$IncludeExtra
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
$runner = Join-Path $Root "scripts\skillsbench_baseline_all.py"
if (-not (Test-Path $venvPython)) { throw "missing $venvPython" }
if (-not (Test-Path $runner)) { throw "missing $runner" }

$env:SKILLSBENCH_VERIFIER_MODE = $VerifierMode
if (-not $env:SKILLSBENCH_ROOT) {
    throw "SKILLSBENCH_ROOT is not set; point it at the SkillsBench checkout (the parent of tasks/)"
}

$args = @(
    $runner,
    "--exec-timeout", "$ExecTimeout",
    "--workers", "$Workers",
    "--verifier-mode", $VerifierMode
)
if ($OutputDir) { $args += @("--output-dir", $OutputDir) }
if ($IncludeExtra) { $args += "--include-extra" }

Write-Host "SkillsBench baseline: original skills, one rollout, verifier=$VerifierMode"
& $venvPython @args
exit $LASTEXITCODE
