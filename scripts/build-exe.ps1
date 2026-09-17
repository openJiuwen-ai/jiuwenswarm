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

$BundleNode = if ($env:BUNDLE_NODE) { $env:BUNDLE_NODE } else { "1" }
$NodeVersion = if ($env:NODE_VERSION) { $env:NODE_VERSION } else { "v22.11.0" }
$NodeSource = $null

function Test-Truthy {
    param([string]$Value)

    $normalized = $Value.Trim().ToLowerInvariant()
    return $normalized -in @("1", "true", "yes", "on")
}

function Get-NodeArch {
    $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture
    if ($arch -eq [System.Runtime.InteropServices.Architecture]::Arm64) {
        return "arm64"
    }
    return "x64"
}

function Download-NodeRuntime {
    param(
        [string]$ProjectRoot,
        [string]$NodeVersion
    )

    $arch = Get-NodeArch
    $nodeName = "node-$NodeVersion-win-$arch"
    $nodeUrl = "https://nodejs.org/dist/$NodeVersion/$nodeName.zip"
    $vendorRoot = Join-Path $ProjectRoot "vendor"
    $target = Join-Path $vendorRoot "node"
    $downloadDir = Join-Path $ProjectRoot ".build\node-download"
    $zipPath = Join-Path $downloadDir "$nodeName.zip"

    Write-Host "[runtime] Downloading Node.js $NodeVersion ($arch)..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $vendorRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    Invoke-WebRequest -Uri $nodeUrl -OutFile $zipPath -UseBasicParsing

    $extractRoot = Join-Path $downloadDir "extract"
    if (Test-Path -LiteralPath $extractRoot) {
        Remove-Item -LiteralPath $extractRoot -Recurse -Force
    }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractRoot -Force

    $extracted = Join-Path $extractRoot $nodeName
    if (-not (Test-Path -LiteralPath (Join-Path $extracted "node.exe"))) {
        throw "Downloaded Node archive does not contain node.exe: $nodeUrl"
    }

    if (Test-Path -LiteralPath $target) {
        $projectResolved = (Resolve-Path -LiteralPath $ProjectRoot).Path
        $targetResolved = (Resolve-Path -LiteralPath $target).Path
        if (-not $targetResolved.StartsWith($projectResolved, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove Node cache outside project: $targetResolved"
        }
        Remove-Item -LiteralPath $targetResolved -Recurse -Force
    }
    Move-Item -LiteralPath $extracted -Destination $target
    return (Resolve-Path -LiteralPath $target).Path
}

function Resolve-NodeRuntimeDir {
    param(
        [string]$ProjectRoot,
        [string]$ExplicitNodeDir,
        [string]$NodeVersion
    )

    if ($ExplicitNodeDir) {
        $resolved = (Resolve-Path -LiteralPath $ExplicitNodeDir -ErrorAction Stop).Path
        if (-not (Test-Path -LiteralPath (Join-Path $resolved "node.exe"))) {
            throw "NodeDir must contain node.exe: $resolved"
        }
        return $resolved
    }

    if ($env:NODE_DIR) {
        $resolved = (Resolve-Path -LiteralPath $env:NODE_DIR -ErrorAction Stop).Path
        if (-not (Test-Path -LiteralPath (Join-Path $resolved "node.exe"))) {
            throw "NODE_DIR must contain node.exe: $resolved"
        }
        return $resolved
    }

    $vendorNode = Join-Path $ProjectRoot "vendor\node"
    if (Test-Path -LiteralPath (Join-Path $vendorNode "node.exe")) {
        return (Resolve-Path -LiteralPath $vendorNode).Path
    }

    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($nodeCommand) {
        return Split-Path -Parent $nodeCommand.Source
    }

    return Download-NodeRuntime -ProjectRoot $ProjectRoot -NodeVersion $NodeVersion
}

function Use-NodeRuntime {
    param([string]$SourceDir)

    if (-not $SourceDir) {
        return
    }
    $env:PATH = "$SourceDir$([System.IO.Path]::PathSeparator)$env:PATH"
}

function Copy-NodeRuntime {
    param(
        [string]$SourceDir,
        [string]$DistDir
    )

    if (-not $SourceDir) {
        return
    }

    $distResolved = (Resolve-Path -LiteralPath $DistDir -ErrorAction Stop).Path
    $runtimeDir = Join-Path $distResolved "runtime"
    $target = Join-Path $runtimeDir "node-runtime"
    if (Test-Path -LiteralPath $target) {
        $targetResolved = (Resolve-Path -LiteralPath $target).Path
        if (-not $targetResolved.StartsWith($distResolved, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove Node runtime outside dist: $targetResolved"
        }
        Remove-Item -LiteralPath $targetResolved -Recurse -Force
    }
    New-Item -ItemType Directory -Path $target -Force | Out-Null

    $files = @("node.exe", "npm.cmd", "npx.cmd", "corepack.cmd", "nodevars.bat")
    foreach ($file in $files) {
        $source = Join-Path $SourceDir $file
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination $target -Force
        }
    }

    $npmModules = Join-Path $SourceDir "node_modules\npm"
    if (Test-Path -LiteralPath $npmModules) {
        $modulesTarget = Join-Path $target "node_modules"
        New-Item -ItemType Directory -Path $modulesTarget -Force | Out-Null
        Copy-Item -LiteralPath $npmModules -Destination $modulesTarget -Recurse -Force
    }

    if (-not (Test-Path -LiteralPath (Join-Path $target "npx.cmd"))) {
        throw "Bundled Node runtime is missing npx.cmd: $target"
    }

    $nodeVersion = & (Join-Path $target "node.exe") --version
    Write-Host "[runtime] Bundled Node $nodeVersion into $target" -ForegroundColor Green
}

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
Write-Host "[1/4] Installing Python dependencies (uv sync --extra dev --extra claude --extra codex)..." -ForegroundColor Yellow
uv sync --extra dev --extra claude --extra codex
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

# 3.5 Bundle Node.js runtime for browser tools
if (Test-Truthy $BundleNode) {
    Write-Host "`n[3.5/4] Bundling Node.js runtime..." -ForegroundColor Yellow
    Copy-NodeRuntime -SourceDir $NodeSource -DistDir $FrozenDir
} else {
    Write-Host "`n[3.5/4] Skipping bundled Node.js runtime (BUNDLE_NODE=$BundleNode)" -ForegroundColor Yellow
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
