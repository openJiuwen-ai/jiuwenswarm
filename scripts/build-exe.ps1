# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# Windows 打包 exe 脚本
# 用法: .\scripts\build-exe.ps1  或  pwsh -File scripts\build-exe.ps1

param(
    [string]$NodeDir = ""
)

$ErrorActionPreference = "Stop"

# 控制台 UTF-8，避免中文 echo 乱码（PowerShell 5.1 默认编码易乱码）
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# 项目根 = 脚本所在目录的上一层，基于脚本自身位置推导，换路径不坏
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

# node/uv 运行时的解析与绑定函数在共享模块 build-runtimes.psm1，与
# build-electron-exe.ps1 单一来源；契约测试 test_desktop_electron_contract.py 钉住两侧同步。
Import-Module (Join-Path $PSScriptRoot "build-runtimes.psm1") -Force
$RuntimeSettings = Get-RuntimeBundleSettings
$BundleNode = $RuntimeSettings.BundleNode
$BundleUv = $RuntimeSettings.BundleUv
$NodeVersion = $RuntimeSettings.NodeVersion
$NodeSource = $null

if (Test-Truthy $BundleNode) {
    $NodeSource = Resolve-NodeRuntimeDir `
        -ProjectRoot $ProjectRoot `
        -ExplicitNodeDir $NodeDir `
        -NodeVersion $NodeVersion
    Use-NodeRuntime -SourceDir $NodeSource
}

Write-Host "=== Windows Build Exe ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot`n" -ForegroundColor Gray

# Synchronize tracked consumers before the project environment resolves the new metadata.
$BuildConfigJson = uv run --no-project --python 3.11 python `
    "$ProjectRoot\scripts\build_config.py" --sync --emit-json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$BuildConfig = $BuildConfigJson | ConvertFrom-Json
$BuildDisplayName = [string]$BuildConfig.display_name
$BuildVersion = [string]$BuildConfig.version
$BuildExecutableNameWindows = [string]$BuildConfig.executable_name_windows
$BuildDistDirName = [string]$BuildConfig.dist_dir_name
$BuildErrorLogName = [string]$BuildConfig.error_log_name
$BuildSetupBaseName = [string]$BuildConfig.setup_base_name
$BuildSetupFilename = [string]$BuildConfig.setup_filename
Write-Host "Build identity: $BuildDisplayName $BuildVersion" -ForegroundColor Gray

# 1. Install dependencies
Write-Host "[1/4] Installing Python dependencies (uv sync --extra dev)..." -ForegroundColor Yellow
uv sync --extra dev
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 2. Build frontend
Write-Host "`n[2/4] Building frontend (jiuwenswarm/channels/web/frontend)..." -ForegroundColor Yellow
Push-Location (Join-Path $ProjectRoot "jiuwenswarm\channels\web\frontend")
$WebDist = Join-Path $ProjectRoot "jiuwenswarm\channels\web\dist"
if (Test-Path $WebDist) { Remove-Item $WebDist -Recurse -Force }
if (Test-Path "node_modules") {
    Write-Host "[build] node_modules exists, skip npm install" -ForegroundColor Gray
} else {
    Write-Host "[build] node_modules missing, running npm install..." -ForegroundColor Gray
    npm install
    if ($LASTEXITCODE -ne 0) { Pop-Location; exit $LASTEXITCODE }
}
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; exit $LASTEXITCODE }
Pop-Location

# 3. Run PyInstaller
Write-Host "`n[3/4] Running PyInstaller..." -ForegroundColor Yellow
uv run pyinstaller scripts\jiuwenswarm.spec --noconfirm
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Verify the actual frozen runtime, not only the PyInstaller source configuration.
$FrozenDir = Join-Path $ProjectRoot "dist\$BuildDistDirName"
$FrozenExe = Join-Path $FrozenDir $BuildExecutableNameWindows
$A2UIVerifier = Join-Path $ProjectRoot "scripts\verify_a2ui_bundle.py"
$VerifyProcess = Start-Process `
    -FilePath $FrozenExe `
    -ArgumentList @($A2UIVerifier) `
    -Wait `
    -PassThru `
    -NoNewWindow
if ($VerifyProcess.ExitCode -ne 0) {
    throw "Frozen A2UI bundle verification failed. See ~/.jiuwenswarm/logs/$BuildErrorLogName"
}

$RsiVerifier = Join-Path $ProjectRoot "scripts\verify_rsi_bundle.py"
$RsiVerifyProcess = Start-Process `
    -FilePath $FrozenExe `
    -ArgumentList @($RsiVerifier) `
    -Wait `
    -PassThru `
    -NoNewWindow
if ($RsiVerifyProcess.ExitCode -ne 0) {
    throw "Frozen RSI bundle verification failed. See ~/.jiuwenswarm/logs/$BuildErrorLogName"
}

$GitCodeVerifier = Join-Path $ProjectRoot "scripts\verify_gitcode_cli_bundle.py"
$GitCodeVerifyProcess = Start-Process `
    -FilePath $FrozenExe `
    -ArgumentList @($GitCodeVerifier) `
    -Wait `
    -PassThru `
    -NoNewWindow
if ($GitCodeVerifyProcess.ExitCode -ne 0) {
    throw "Frozen GitCode CLI bundle verification failed. See ~/.jiuwenswarm/logs/$BuildErrorLogName"
}

# 3.5 Bundle Node.js runtime for browser tools
if (Test-Truthy $BundleNode) {
    Write-Host "`n[3.5/4] Bundling Node.js runtime..." -ForegroundColor Yellow
    Copy-NodeRuntime -SourceDir $NodeSource -DistDir $FrozenDir

    $PlaywrightVerifier = Join-Path $ProjectRoot "scripts\verify_playwright_mcp_bundle.py"
    $PlaywrightVerifyProcess = Start-Process `
        -FilePath $FrozenExe `
        -ArgumentList @($PlaywrightVerifier) `
        -Wait `
        -PassThru `
        -NoNewWindow
    if ($PlaywrightVerifyProcess.ExitCode -ne 0) {
        throw "Frozen Playwright MCP bundle verification failed. See ~/.jiuwenswarm/logs/$BuildErrorLogName"
    }
} else {
    Write-Host "`n[3.5/4] Skipping bundled Node.js runtime (BUNDLE_NODE=$BundleNode)" -ForegroundColor Yellow
}

if (Test-Truthy $BundleUv) {
    Write-Host "`n[3.6/4] Bundling uv runtime..." -ForegroundColor Yellow
    $UvExePath = Resolve-UvRuntimeDir -ProjectRoot $ProjectRoot
    Copy-UvRuntime -UvExePath $UvExePath -DistDir $FrozenDir
} else {
    Write-Host "`n[3.6/4] Skipping bundled uv runtime (BUNDLE_UV=$BundleUv)" -ForegroundColor Yellow
}

# 4. Build installer (Inno Setup)
Write-Host "`n[4/4] Building installer (Inno Setup)..." -ForegroundColor Yellow
$IsccPaths = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$Iscc = $IsccPaths | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    $Iscc = Get-Command iscc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
}
if (-not $Iscc) {
    Write-Host "Downloading Inno Setup 6..." -ForegroundColor Yellow
    $InnoUrl = "https://jrsoftware.org/download.php/is.exe"
    $InnoExe = "$env:TEMP\innosetup-6.7.1.exe"
    Invoke-WebRequest -Uri $InnoUrl -OutFile $InnoExe -UseBasicParsing
    Write-Host "Installing Inno Setup 6 (silent)..." -ForegroundColor Yellow
    Start-Process `
        -FilePath $InnoExe `
        -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/SP-" `
        -Wait `
        -NoNewWindow
    $Iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $Iscc)) {
        Write-Host "ERROR: Inno Setup installation failed" -ForegroundColor Red
        exit 1
    }
}
$InnoDefines = @(
    "/DBuildDisplayName=$BuildDisplayName",
    "/DBuildVersion=$BuildVersion",
    "/DBuildExecutableNameWindows=$BuildExecutableNameWindows",
    "/DBuildDistDirName=$BuildDistDirName",
    "/DBuildSetupBaseName=$BuildSetupBaseName"
)
& $Iscc @InnoDefines "$ProjectRoot\scripts\installer.iss"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$InstallerPath = Join-Path $ProjectRoot "dist\$BuildSetupFilename"
if (-not (Test-Path -LiteralPath $InstallerPath)) {
    throw "Installer was not created at the configured path: $InstallerPath"
}

Write-Host "`n=== Build complete ===" -ForegroundColor Green
Write-Host "Installer: $InstallerPath" -ForegroundColor Green
Write-Host "Size: $([math]::Round((Get-Item $InstallerPath).Length / 1MB, 1)) MB" -ForegroundColor Green
