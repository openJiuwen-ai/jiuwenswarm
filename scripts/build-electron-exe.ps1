# JiuwenSwarm Electron 打包脚本 (Windows)
# 用法:
#   正式版:  .\scripts\build-electron-exe.ps1
#   测试版:  .\scripts\build-electron-exe.ps1 -Test
#   前端测试: .\scripts\build-electron-exe.ps1 -Test -FrontendOnly
#
# 打包流程:
# 1. 安装 Python 依赖 (uv sync --extra dev --extra claude --extra codex) [跳过: FrontendOnly]
# 2. 构建前端 (vite build with ELECTRON=true)
# 3. 安装 Electron 桌面端依赖 (npm install in channels/desktop/electron)
# 4. PyInstaller 打包后端 (jiuwenswarm.spec)           [跳过: FrontendOnly]
# 5. 组装最终产物: Electron shell + 前端 dist + 后端 exe + resources
# 6. Inno Setup 打成单个安装包 WorkSwarm-setup-*.exe
#    （应用名 / exe 名 / 版本号均来自 build_config，与 Python 打包一致）

param(
    [string]$ElectronDir = $(if ($env:ELECTRON_DIR) { $env:ELECTRON_DIR } else { "" }),
    [string]$NodeDir = "",
    [switch]$Test,
    [switch]$FrontendOnly
)

$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

# ── Node/uv 运行时解析（与 build-exe.ps1 同一契约，共享 build-runtimes.psm1）──
# 1) 构建前把解析到的 Node 前置到 PATH，前端/Electron/MCP 的 npm 构建与
#    Python 打包链路使用同一个 Node 工具链；
# 2) 组装阶段（步骤 5）把 node-runtime / uv-runtime 绑进后端 exe 目录
#    （resources\backend），冻结入口 jiuwenswarm_exe_entry.py 会在
#    <后端 exe 目录>\runtime\ 下查找并前置 PATH，Agent 技能（ppt-creation
#    等 node 脚本）因此不依赖用户机器的 Node.js。
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

$FrontendDir = Join-Path $ProjectRoot "jiuwenswarm\channels\web\frontend"
$DesktopDir = Join-Path $ProjectRoot "jiuwenswarm\channels\desktop\electron"

# ── Resolve Electron runtime ─────────────────────────────────────────────────
# The Electron runtime is shipped inside channels/desktop/electron/node_modules
# after `npm install`. If the user provides -ElectronDir, use that instead.
if (-not $ElectronDir) {
    $LocalElectron = Join-Path $DesktopDir "node_modules\electron\dist"
    if (Test-Path (Join-Path $LocalElectron "electron.exe")) {
        $ElectronDir = $LocalElectron
    }
}

if (-not $ElectronDir -or -not (Test-Path (Join-Path $ElectronDir "electron.exe"))) {
    Write-Host "ERROR: electron.exe not found." -ForegroundColor Red
    Write-Host "  Run 'npm install' in jiuwenswarm\channels\desktop\electron first," -ForegroundColor Gray
    Write-Host "  or pass -ElectronDir pointing to an Electron dist directory." -ForegroundColor Gray
    exit 1
}

$ElectronExe = Join-Path $ElectronDir "electron.exe"

Write-Host "=== WorkSwarm Electron Build ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot" -ForegroundColor Gray
Write-Host "Electron: $ElectronExe" -ForegroundColor Gray
Write-Host "Test: $Test  FrontendOnly: $FrontendOnly`n" -ForegroundColor Gray

# ── 1. Install Python dependencies ────────────────────────────────────────────
if (-not $FrontendOnly) {
    Write-Host "`n[1/6] Installing Python dependencies (uv sync --extra dev --extra claude --extra codex)..." -ForegroundColor Yellow
    uv sync --extra dev --extra claude --extra codex
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    Write-Host "`n[1/6] Skipping Python dependencies (FrontendOnly)" -ForegroundColor Gray
}

# ── 2. Build frontend (vite build with ELECTRON=true) ─────────────────────────
Write-Host "`n[2/6] Building frontend..." -ForegroundColor Yellow
Push-Location $FrontendDir
# 保存并在结束后恢复 ELECTRON，避免污染调用方 shell 会话（失败退出也要恢复）
$PreviousElectronEnv = $env:ELECTRON
$env:ELECTRON = "true"
$FrontendBuildExit = 0
try {
    if (-not (Test-Path "node_modules")) {
        Write-Host "[build] node_modules missing, running npm install..." -ForegroundColor Gray
        npm install
        if ($LASTEXITCODE -ne 0) { $FrontendBuildExit = $LASTEXITCODE; throw "frontend npm install failed" }
    }
    npm run build
    if ($LASTEXITCODE -ne 0) { $FrontendBuildExit = $LASTEXITCODE; throw "vite build failed" }
} finally {
    Pop-Location
    if ($null -ne $PreviousElectronEnv) { $env:ELECTRON = $PreviousElectronEnv } else { Remove-Item Env:\ELECTRON -ErrorAction SilentlyContinue }
}
if ($FrontendBuildExit -ne 0) { exit $FrontendBuildExit }

# ── 3. Install Electron desktop dependencies ──────────────────────────────────
Write-Host "`n[3/6] Installing Electron desktop dependencies..." -ForegroundColor Yellow
Push-Location $DesktopDir
if (-not (Test-Path "node_modules")) {
    Write-Host "[desktop] node_modules missing, running npm install..." -ForegroundColor Gray
    npm install
    if ($LASTEXITCODE -ne 0) { Pop-Location; exit $LASTEXITCODE }
} else {
    Write-Host "[desktop] node_modules exists, skip npm install" -ForegroundColor Gray
}
Pop-Location

# ── 4. PyInstaller: build backend exe ─────────────────────────────────────────
# COLLECT 输出目录、exe 名与版本号均来自 build_config（改名后会变化），不能写死，
# 否则可能命中 dist 下改名前的陈旧目录（如 dist\jiuwenswarm），把旧后端打进包里；
# 版本号与 pyproject 保持单一来源。--sync 先同步 _build_config.py（对齐
# build-exe.ps1；PyInstaller spec 对漂移会直接硬失败）。FrontendOnly 也读取
# （后续组装 package.json / 安装器 define 需要）。
$BuildConfigJson = uv run --no-project --python 3.11 python `
    "$ProjectRoot\scripts\build_config.py" --sync --emit-json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$BuildConfig = $BuildConfigJson | ConvertFrom-Json
$BuildDistDirName = [string]$BuildConfig.dist_dir_name
$BuildExecutableNameWindows = [string]$BuildConfig.executable_name_windows
$BuildVersion = [string]$BuildConfig.version
$BuildErrorLogName = [string]$BuildConfig.error_log_name
# 显示名同样来自 build_config（与 installer.iss / Python 打包单一来源）；
# 壳 exe 用显示名派生（WorkSwarm.exe），与后端 exe（workswarm.exe，小写）
# 在任务管理器中可区分，同时保持产品名一致。
$BuildDisplayName = [string]$BuildConfig.display_name
$ShellExeName = "$BuildDisplayName.exe"

if (-not $FrontendOnly) {
    Write-Host "`n[4/6] Running PyInstaller (backend)..." -ForegroundColor Yellow
    uv run pyinstaller scripts\jiuwenswarm.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $BackendDist = Join-Path $ProjectRoot "dist\$BuildDistDirName"
    if (-not (Test-Path (Join-Path $BackendDist $BuildExecutableNameWindows))) {
        throw "PyInstaller output not found: $BackendDist\$BuildExecutableNameWindows"
    }

    # Verify the actual frozen runtime, not only the source configuration
    # (same self-check as the Python packaging build in build-exe.ps1).
    $FrozenExe = Join-Path $BackendDist $BuildExecutableNameWindows
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
} else {
    Write-Host "`n[4/6] Skipping PyInstaller (FrontendOnly)" -ForegroundColor Gray
}

# ── 5. Assemble final Electron app ────────────────────────────────────────────
Write-Host "`n[5/6] Assembling Electron desktop app..." -ForegroundColor Yellow

$ElectronAppDir = Join-Path $ProjectRoot "dist\$BuildDisplayName-Electron"
if (Test-Path $ElectronAppDir) {
    Remove-Item $ElectronAppDir -Recurse -Force
}
New-Item -ItemType Directory -Path $ElectronAppDir -Force | Out-Null

# Copy Electron runtime (electron.exe + DLLs)
Write-Host "  Copying Electron runtime..." -ForegroundColor Gray
Copy-Item -Path (Join-Path $ElectronDir "*") -Destination $ElectronAppDir -Recurse -Force
# Remove default_app.asar so our main.cjs is used instead
Remove-Item (Join-Path $ElectronAppDir "resources\default_app.asar") -Force -ErrorAction SilentlyContinue

# Rename electron.exe to the product shell exe (WorkSwarm.exe, from build_config)
Rename-Item (Join-Path $ElectronAppDir "electron.exe") $ShellExeName -Force

# Set exe icon via rcedit so the title bar / taskbar / file explorer shows logo.ico
$LogoIco = Join-Path $FrontendDir "public\logo.ico"
$NpmGlobalRoot = & { $ErrorActionPreference = 'SilentlyContinue'; (npm root -g) }
$RceditCandidates = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\..\rcedit.exe",
    "C:\Program Files (x86)\Windows Kits\10\bin\*\x64\rcedit.exe",
    "$NpmGlobalRoot\rcedit\bin\rcedit.exe"
)
$Rcedit = $RceditCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Rcedit) {
    $Rcedit = Get-Command rcedit -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
}
if ($Rcedit) {
    Write-Host "  Setting exe icon via rcedit..." -ForegroundColor Gray
    & $Rcedit (Join-Path $ElectronAppDir $ShellExeName) --set-icon $LogoIco
} else {
    Write-Host "  WARNING: rcedit not found, exe icon will use Electron default" -ForegroundColor Yellow
}

# Create resources/app directory for our Electron main code
$AppDir = Join-Path $ElectronAppDir "resources\app"
New-Item -ItemType Directory -Path $AppDir -Force | Out-Null

# Copy the mature Electron desktop shell (main.cjs, preload.cjs, launch.cjs, target_mcp_wrapper.cjs)
Write-Host "  Copying Electron desktop shell..." -ForegroundColor Gray
Copy-Item -Path (Join-Path $DesktopDir "main.cjs") -Destination $AppDir -Force
Copy-Item -Path (Join-Path $DesktopDir "browser_panels.cjs") -Destination $AppDir -Force
Copy-Item -Path (Join-Path $DesktopDir "preload.cjs") -Destination $AppDir -Force
Copy-Item -Path (Join-Path $DesktopDir "target_mcp_wrapper.cjs") -Destination $AppDir -Force

# Copy logo for window icon
Copy-Item -Path $LogoIco -Destination (Join-Path $AppDir "logo.ico") -Force

# Copy logo.svg for loading page
$LogoSvg = Join-Path $FrontendDir "public\logo.svg"
if (Test-Path $LogoSvg) {
    Copy-Item -Path $LogoSvg -Destination (Join-Path $AppDir "logo.svg") -Force
    Write-Host "  Copied logo.svg for loading page" -ForegroundColor Gray
}

# Copy frontend dist into resources/app/dist
$FrontendDistSrc = Join-Path $FrontendDir "dist"
$FrontendDistDst = Join-Path $AppDir "dist"
Copy-Item -Path $FrontendDistSrc -Destination $FrontendDistDst -Recurse -Force

# Create resources/backend directory with PyInstaller output (skip if FrontendOnly)
if (-not $FrontendOnly) {
    Write-Host "  Copying PyInstaller backend..." -ForegroundColor Gray
    $BackendDir = Join-Path $ElectronAppDir "resources\backend"
    New-Item -ItemType Directory -Path $BackendDir -Force | Out-Null
    Copy-Item -Path (Join-Path $BackendDist "*") -Destination $BackendDir -Recurse -Force

    # Node/uv 运行时绑进后端 exe 目录（resources\backend），与 Python 独立包
    # （dist\<包名>\runtime\...）同一契约：冻结入口统一在 <后端 exe 目录>\runtime\
    # 下查找 node-runtime / uv-runtime。Electron 安装包递归打包
    # resources\backend\*（installer-electron.iss），安装器无需额外改动。
    if (Test-Truthy $BundleNode) {
        Write-Host "  Bundling Node.js runtime into backend..." -ForegroundColor Gray
        Copy-NodeRuntime -SourceDir $NodeSource -DistDir $BackendDir

        # 与 build-exe.ps1 同款自检：此时 runtime\node-runtime 已就位，冻结入口会
        # 把它前置到 PATH，验证包内 Playwright MCP + Node 真能跑起来。
        $BackendFrozenExe = Join-Path $BackendDir $BuildExecutableNameWindows
        $PlaywrightVerifier = Join-Path $ProjectRoot "scripts\verify_playwright_mcp_bundle.py"
        $PlaywrightVerifyProcess = Start-Process `
            -FilePath $BackendFrozenExe `
            -ArgumentList @($PlaywrightVerifier) `
            -Wait `
            -PassThru `
            -NoNewWindow
        if ($PlaywrightVerifyProcess.ExitCode -ne 0) {
            throw "Frozen Playwright MCP bundle verification failed. See ~/.jiuwenswarm/logs/$BuildErrorLogName"
        }
    } else {
        Write-Host "  Skipping bundled Node.js runtime (BUNDLE_NODE=$BundleNode)" -ForegroundColor Yellow
    }
    if (Test-Truthy $BundleUv) {
        Write-Host "  Bundling uv runtime into backend..." -ForegroundColor Gray
        $UvExePath = Resolve-UvRuntimeDir -ProjectRoot $ProjectRoot
        Copy-UvRuntime -UvExePath $UvExePath -DistDir $BackendDir
    } else {
        Write-Host "  Skipping bundled uv runtime (BUNDLE_UV=$BundleUv)" -ForegroundColor Yellow
    }
}

# Create package.json for the Electron app (main.cjs is CommonJS)
# 版本号来自 build_config（与 pyproject 单一来源）。
$AppPackageJsonObject = @{
    name = "jiuwenswarm-electron"
    version = $BuildVersion
    main = "main.cjs"
}
# 内置 MCP 运行时：@playwright/mcp 及其依赖装进 resources/app/node_modules，
# wrapper 与其同级（require.resolve 命中），打包版浏览器 Agent 不依赖用户
# 机器的 Node.js/npx，也无需首启联网下载。版本从 main.cjs 的固定 pin 解析，
# 避免两处漂移。FrontendOnly 同样需要：外部后端经发现文件绑定 Electron 壳
# 后，会以本 exe 的 Node 模式运行 wrapper（与完整包同一契约）。
$McpPackageMatch = Select-String -Path (Join-Path $DesktopDir "main.cjs") -Pattern "PLAYWRIGHT_MCP_PACKAGE = '([^']+)'"
if (-not $McpPackageMatch) { throw "PLAYWRIGHT_MCP_PACKAGE not found in main.cjs" }
$McpPackageSpec = $McpPackageMatch.Matches[0].Groups[1].Value
$McpParts = $McpPackageSpec -split '@'
$McpPackageName = "@$($McpParts[1])"
$McpPackageVersion = $McpParts[2]
if ([string]::IsNullOrWhiteSpace($McpPackageVersion)) { throw "Failed to parse MCP package version from: $McpPackageSpec" }
$AppPackageJsonObject.dependencies = @{ $McpPackageName = $McpPackageVersion }
$AppPackageJson = $AppPackageJsonObject | ConvertTo-Json -Depth 5
Set-Content -Path (Join-Path $AppDir "package.json") -Value $AppPackageJson -Encoding UTF8

Write-Host "  Installing bundled MCP runtime ($McpPackageName@$McpPackageVersion)..." -ForegroundColor Gray
Push-Location $AppDir
npm install --omit=dev --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { Pop-Location; exit $LASTEXITCODE }
Pop-Location

# Test build: write .test marker file so main.cjs can enable debug features
if ($Test) {
    Set-Content -Path (Join-Path $AppDir ".test") -Value "test" -Encoding UTF8
    Write-Host "  Test build: .test marker written" -ForegroundColor Yellow
}

# FrontendOnly build: write .frontend-only marker
if ($FrontendOnly) {
    Set-Content -Path (Join-Path $AppDir ".frontend-only") -Value "frontend-only" -Encoding UTF8
    Write-Host "  FrontendOnly build: no backend, loads local dist" -ForegroundColor Yellow
}

$TotalSize = [math]::Round((Get-ChildItem $ElectronAppDir -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "  Assembled app size: $TotalSize MB" -ForegroundColor Gray

# ── 6. Build installer (Inno Setup) ───────────────────────────────────────────
Write-Host "`n[6/6] Building installer (Inno Setup)..." -ForegroundColor Yellow

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
    Write-Host "Inno Setup not found, installing via winget..." -ForegroundColor Yellow
    winget install JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements
    $Iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $Iscc)) {
        $Iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    }
    if (-not (Test-Path $Iscc)) {
        Write-Host "ERROR: Inno Setup installation failed" -ForegroundColor Red
        exit 1
    }
}

Write-Host "Using ISCC: $Iscc" -ForegroundColor Gray

# Test/FrontendOnly build uses a different installer filename to distinguish from release
# 应用名 / 壳 exe 名 / 版本号 / 后端 exe 名经 /D 传入，iss 内不再硬编码
# （与 build_config 单一来源；后端 exe 名供卸载时的 --desktop-reset-external-cli-config 使用）
if ($FrontendOnly) {
    & $Iscc "$ProjectRoot\scripts\installer-electron.iss" "/DMyAppName=$BuildDisplayName" "/DMyAppExeName=$ShellExeName" "/DMyAppVersion=$BuildVersion" "/DBackendExecutableName=$BuildExecutableNameWindows" /DELECTRON_FRONTEND_ONLY
} elseif ($Test) {
    & $Iscc "$ProjectRoot\scripts\installer-electron.iss" "/DMyAppName=$BuildDisplayName" "/DMyAppExeName=$ShellExeName" "/DMyAppVersion=$BuildVersion" "/DBackendExecutableName=$BuildExecutableNameWindows" /DELECTRON_TEST_BUILD
} else {
    & $Iscc "$ProjectRoot\scripts\installer-electron.iss" "/DMyAppName=$BuildDisplayName" "/DMyAppExeName=$ShellExeName" "/DMyAppVersion=$BuildVersion" "/DBackendExecutableName=$BuildExecutableNameWindows"
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($FrontendOnly) {
    $InstallerPath = (
        Get-ChildItem "$ProjectRoot\dist\$BuildDisplayName-frontend-test-*.exe" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    ).FullName
} elseif ($Test) {
    $InstallerPath = (
        Get-ChildItem "$ProjectRoot\dist\$BuildDisplayName-test-*.exe" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    ).FullName
} else {
    $InstallerPath = (
        Get-ChildItem "$ProjectRoot\dist\$BuildDisplayName-setup-*.exe" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    ).FullName
}

Write-Host "`n=== Build complete ===" -ForegroundColor Green
Write-Host "Installer: $InstallerPath" -ForegroundColor Green
Write-Host "Size: $([math]::Round((Get-Item $InstallerPath).Length / 1MB, 1)) MB" -ForegroundColor Green
