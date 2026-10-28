# SkillsBench: score the FULL task set once (no train/val/test cut, no training).
# Agent on host; verifier in Docker.
#
# Usage:
#   .\.venv\Scripts\Activate.ps1
#   $env:SKILLSBENCH_ROOT = "<parent-of-tasks>"   # no built-in default
#   # Gateway already up: jiuwenswarm-start app
#   powershell -ExecutionPolicy Bypass -File .\scripts\skillsbench_docker_full_eval.ps1

param(
    [int]$ExecTimeout = 1800,
    [int]$Workers = 1,
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
$runner = Join-Path $Root "scripts\skillsbench_docker_score_all.py"
if (-not (Test-Path $venvPython)) { throw "missing $venvPython" }
if (-not (Test-Path $runner)) { throw "missing $runner" }

$env:SKILLSBENCH_VERIFIER_MODE = "docker"
if (-not $env:SKILLSBENCH_ROOT) {
    throw "SKILLSBENCH_ROOT is not set; point it at the SkillsBench checkout (the parent of tasks/)"
}

# Gateway check
& $venvPython -c @"
from jiuwenswarm.cli.skill_train import check_gateway
from jiuwenswarm.cli.chat import _build_default_gateway_url
u = _build_default_gateway_url()
ok = check_gateway(u)
print(u, ok)
raise SystemExit(0 if ok else 1)
"@
if ($LASTEXITCODE -ne 0) {
    throw "Gateway unreachable. Start with: .\.venv\Scripts\jiuwenswarm-start.exe app"
}

$args = @($runner, "--exec-timeout", "$ExecTimeout", "--workers", "$Workers")
if ($OutputDir) { $args += @("--output-dir", $OutputDir) }

Write-Host "SKILLSBENCH_VERIFIER_MODE=$($env:SKILLSBENCH_VERIFIER_MODE) (full dataset score, no split, no training)"
& $venvPython @args
exit $LASTEXITCODE
