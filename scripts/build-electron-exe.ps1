# JiuwenSwarm Electron 打包脚本 (Windows)
# 用法:
#   正式版:  .\scripts\build-electron-exe.ps1
#   测试版:  .\scripts\build-electron-exe.ps1 -Test
#   前端测试: .\scripts\build-electron-exe.ps1 -Test -FrontendOnly
#
# 打包流程:
# 1. 安装 Python 依赖 (uv sync --extra dev --extra cua) [跳过: FrontendOnly]
# 2. 构建前端 (vite build with ELECTRON=true)
# 3. 安装 Electron 桌面端依赖 (npm install in channels/desktop/electron)
# 4. PyInstaller 打包后端 (jiuwenswarm.spec)           [跳过: FrontendOnly]
# 5. 组装最终产物: Electron shell + 前端 dist + 后端 exe + resources
# 6. Inno Setup 打成 build_config 定义的正式安装包
#    （应用名 / exe 名 / 版本号均来自 build_config，与 Python 打包一致）

param(
    [string]$ElectronDir = $(if ($env:ELECTRON_DIR) { $env:ELECTRON_DIR } else { "" }),
    [string]$NodeDir = "",
    [string]$SigningCertificateThumbprint = "",
    [string]$SignToolPath = "",
    [string]$TimestampUrl = "http://time.certum.pl",
    [switch]$NoSign,
    [switch]$RequireSigning,
    [switch]$Test,
    [switch]$FrontendOnly
)

$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$PreviousProcessorArchitecture = $env:PROCESSOR_ARCHITECTURE
$env:PROCESSOR_ARCHITECTURE = "AMD64"

try {

function Resolve-SignToolPath {
    param([string]$ExplicitPath)

    $Candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        $Candidates += $ExplicitPath
    }
    if (-not [string]::IsNullOrWhiteSpace($env:WORKSWARM_SIGNTOOL_PATH)) {
        $Candidates += $env:WORKSWARM_SIGNTOOL_PATH
    }

    $WindowsKitsBin = "C:\Program Files (x86)\Windows Kits\10\bin"
    if (Test-Path -LiteralPath $WindowsKitsBin) {
        $Candidates += Get-ChildItem $WindowsKitsBin -Filter signtool.exe -Recurse -File `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
            Sort-Object FullName -Descending |
            Select-Object -ExpandProperty FullName
    }

    $Command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($Command) {
        $Candidates += $Command.Source
    }

    $Resolved = $Candidates |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and (Test-Path -LiteralPath $_) } |
        Select-Object -First 1
    if (-not $Resolved) {
        throw "signtool.exe was not found. Install Windows SDK Signing Tools or pass -SignToolPath."
    }
    return [System.IO.Path]::GetFullPath($Resolved)
}

function Resolve-CodeSigningCertificate {
    param([string]$ExplicitThumbprint)

    $RequestedThumbprint = $ExplicitThumbprint
    if ([string]::IsNullOrWhiteSpace($RequestedThumbprint)) {
        $RequestedThumbprint = $env:WORKSWARM_SIGNING_CERTIFICATE_THUMBPRINT
    }

    if (-not [string]::IsNullOrWhiteSpace($RequestedThumbprint)) {
        $RequestedThumbprint = ($RequestedThumbprint -replace '\s', '').ToUpperInvariant()
        $Certificate = Get-Item `
            -LiteralPath "Cert:\CurrentUser\My\$RequestedThumbprint" `
            -ErrorAction SilentlyContinue
        if (-not $Certificate) {
            throw "Configured code-signing certificate '$RequestedThumbprint' is not available in Cert:\CurrentUser\My."
        }
        $Candidates = @($Certificate)
    } else {
        $Now = Get-Date
        $Candidates = @(
            Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.HasPrivateKey -and ($Now -ge $_.NotBefore) -and ($Now -le $_.NotAfter)
                }
        )
        if ($Candidates.Count -gt 1) {
            $Available = ($Candidates | ForEach-Object { "$($_.Thumbprint) ($($_.Subject))" }) -join '; '
            throw "Multiple code-signing certificates are available. Pass -SigningCertificateThumbprint or set WORKSWARM_SIGNING_CERTIFICATE_THUMBPRINT. Available: $Available"
        }
    }

    if ($Candidates.Count -eq 0) {
        return $null
    }

    $Certificate = $Candidates[0]
    if (-not $Certificate.HasPrivateKey) {
        throw "Code-signing certificate '$($Certificate.Thumbprint)' does not expose a private key."
    }
    if ((Get-Date) -lt $Certificate.NotBefore -or (Get-Date) -gt $Certificate.NotAfter) {
        throw "Code-signing certificate '$($Certificate.Thumbprint)' is outside its validity period."
    }
    return $Certificate
}

function Test-PortableExecutable {
    param([Parameter(Mandatory)][string]$Path)

    try {
        $Stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        try {
            if ($Stream.Length -lt 64) { return $false }
            $Reader = [System.IO.BinaryReader]::new($Stream)
            if ($Reader.ReadUInt16() -ne 0x5A4D) { return $false }
            $Stream.Position = 0x3C
            $PeOffset = $Reader.ReadInt32()
            if (($PeOffset -lt 0) -or (($PeOffset + 4) -gt $Stream.Length)) { return $false }
            $Stream.Position = $PeOffset
            return $Reader.ReadUInt32() -eq 0x00004550
        } finally {
            $Stream.Dispose()
        }
    } catch {
        throw "Unable to inspect PE file '$Path': $($_.Exception.Message)"
    }
}

function Test-EmbeddedAuthenticodeSignature {
    param([Parameter(Mandatory)][string]$Path)

    $Stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    try {
        $Reader = [System.IO.BinaryReader]::new($Stream)
        $Stream.Position = 0x3C
        $PeOffset = $Reader.ReadInt32()
        $OptionalHeaderOffset = $PeOffset + 24
        $Stream.Position = $OptionalHeaderOffset
        $Magic = $Reader.ReadUInt16()
        if ($Magic -eq 0x10B) {
            $NumberOfDirectoriesOffset = $OptionalHeaderOffset + 92
            $DataDirectoriesOffset = $OptionalHeaderOffset + 96
        } elseif ($Magic -eq 0x20B) {
            $NumberOfDirectoriesOffset = $OptionalHeaderOffset + 108
            $DataDirectoriesOffset = $OptionalHeaderOffset + 112
        } else {
            throw "Unsupported PE optional header in '$Path'."
        }

        $Stream.Position = $NumberOfDirectoriesOffset
        if ($Reader.ReadUInt32() -le 4) { return $false }

        # IMAGE_DIRECTORY_ENTRY_SECURITY (index 4) stores a file offset, not an RVA.
        $Stream.Position = $DataDirectoriesOffset + (4 * 8)
        $CertificateOffset = $Reader.ReadUInt32()
        $CertificateSize = $Reader.ReadUInt32()
        if (($CertificateOffset -eq 0) -or ($CertificateSize -eq 0)) { return $false }
        if ([uint64]$CertificateOffset + [uint64]$CertificateSize -gt [uint64]$Stream.Length) {
            throw "Invalid PE certificate table bounds in '$Path'."
        }
        return $true
    } finally {
        $Stream.Dispose()
    }
}

function Assert-ValidEmbeddedSignature {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$ResolvedSignTool,
        [string]$ExpectedThumbprint = "",
        [switch]$RequireTimestamp
    )

    if (-not (Test-EmbeddedAuthenticodeSignature -Path $Path)) {
        throw "No embedded Authenticode signature was found in '$Path'."
    }

    # Get-AuthenticodeSignature prefers a local catalog signature even when an
    # embedded signature exists. SignTool without /a verifies the embedded one.
    $VerifyArguments = @('verify', '/pa', '/all')
    if ($RequireTimestamp) {
        $VerifyArguments += '/tw'
    }
    $VerifyArguments += $Path
    & $ResolvedSignTool @VerifyArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Embedded Authenticode verification failed for '$Path' (SignTool exit code $LASTEXITCODE)."
    }

    if (-not [string]::IsNullOrWhiteSpace($ExpectedThumbprint)) {
        $EmbeddedCertificate = [System.Security.Cryptography.X509Certificates.X509Certificate]::CreateFromSignedFile($Path)
        $ActualThumbprint = $EmbeddedCertificate.GetCertHashString()
        if ($ActualThumbprint -ne $ExpectedThumbprint) {
            throw "Unexpected embedded signing certificate for '$Path': $ActualThumbprint"
        }
    }
}

function Invoke-SignToolBatchVerification {
    param(
        [Parameter(Mandatory)][string[]]$Paths,
        [Parameter(Mandatory)][string]$ResolvedSignTool,
        [switch]$RequireTimestamp
    )

    $BatchSize = 20
    for ($Offset = 0; $Offset -lt $Paths.Count; $Offset += $BatchSize) {
        $LastIndex = [Math]::Min($Offset + $BatchSize - 1, $Paths.Count - 1)
        $BatchPaths = @($Paths[$Offset..$LastIndex])
        $VerifyArguments = @('verify', '/pa', '/all')
        if ($RequireTimestamp) {
            $VerifyArguments += '/tw'
        }
        $VerifyArguments += $BatchPaths
        & $ResolvedSignTool @VerifyArguments
        if ($LASTEXITCODE -ne 0) {
            throw "Embedded Authenticode verification failed for PE batch beginning with '$($BatchPaths[0])'."
        }
    }
}

function Invoke-CodeSigning {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$ResolvedSignTool,
        [Parameter(Mandatory)][string]$CertificateThumbprint,
        [Parameter(Mandatory)][string]$TimestampServer,
        [string[]]$ForceResignPaths = @()
    )

    $PortableExecutables = @(
        Get-ChildItem -LiteralPath $Root -Recurse -File |
            Where-Object { Test-PortableExecutable -Path $_.FullName } |
            Sort-Object FullName
    )
    if ($PortableExecutables.Count -eq 0) {
        throw "No PE files were found under '$Root'."
    }

    $ForceResignFullPaths = @(
        $ForceResignPaths | ForEach-Object { [System.IO.Path]::GetFullPath($_) }
    )
    $UnsignedFiles = [System.Collections.Generic.List[System.IO.FileInfo]]::new()
    $AlreadySignedFiles = [System.Collections.Generic.List[System.IO.FileInfo]]::new()
    foreach ($File in $PortableExecutables) {
        $FileFullPath = [System.IO.Path]::GetFullPath($File.FullName)
        $ForceResign = $ForceResignFullPaths | Where-Object {
            $_.Equals($FileFullPath, [System.StringComparison]::OrdinalIgnoreCase)
        }
        if ($ForceResign -or -not (Test-EmbeddedAuthenticodeSignature -Path $File.FullName)) {
            $UnsignedFiles.Add($File)
        } else {
            $AlreadySignedFiles.Add($File)
        }
    }

    if ($AlreadySignedFiles.Count -gt 0) {
        Invoke-SignToolBatchVerification `
            -Paths @($AlreadySignedFiles | ForEach-Object { $_.FullName }) `
            -ResolvedSignTool $ResolvedSignTool
    }

    $BatchSize = 20
    for ($Offset = 0; $Offset -lt $UnsignedFiles.Count; $Offset += $BatchSize) {
        $LastIndex = [Math]::Min($Offset + $BatchSize - 1, $UnsignedFiles.Count - 1)
        $BatchPaths = @($UnsignedFiles[$Offset..$LastIndex] | ForEach-Object { $_.FullName })
        & $ResolvedSignTool sign `
            /sha1 $CertificateThumbprint `
            /fd SHA256 `
            /tr $TimestampServer `
            /td SHA256 `
            /v `
            @BatchPaths
        if ($LASTEXITCODE -ne 0) {
            throw "SignTool failed for PE batch beginning with '$($BatchPaths[0])'."
        }
    }

    if ($UnsignedFiles.Count -gt 0) {
        Invoke-SignToolBatchVerification `
            -Paths @($UnsignedFiles | ForEach-Object { $_.FullName }) `
            -ResolvedSignTool $ResolvedSignTool `
            -RequireTimestamp
        foreach ($File in $UnsignedFiles) {
            $EmbeddedCertificate = [System.Security.Cryptography.X509Certificates.X509Certificate]::CreateFromSignedFile($File.FullName)
            if ($EmbeddedCertificate.GetCertHashString() -ne $CertificateThumbprint) {
                throw "Unexpected embedded signing certificate for '$($File.FullName)': $($EmbeddedCertificate.GetCertHashString())"
            }
        }
    }

    Write-Host (
        "[sign] PE files: {0}; newly signed: {1}; existing valid signatures: {2}" -f `
            $PortableExecutables.Count, $UnsignedFiles.Count, $AlreadySignedFiles.Count
    ) -ForegroundColor Gray
}

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

# Keep the architecture override process-local, verify the value Python-backed
# dependencies will observe, and let every packaging child process inherit it.
$MachineOutput = & uv run --no-project --python 3.11 python -c "import platform; print(platform.machine())"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to verify Windows packaging architecture with Python"
}
$DetectedMachine = ([string]($MachineOutput | Select-Object -Last 1)).Trim()
if ($DetectedMachine -cne "AMD64") {
    throw "Windows Electron packaging requires platform.machine() == AMD64; got '$DetectedMachine'"
}
Write-Host "Packaging architecture: $DetectedMachine" -ForegroundColor Gray

$NoSignRequested = $NoSign -or ($env:NOSIGN -eq "1")
$RequireSigningRequested = $RequireSigning -or ($env:REQUIRE_SIGNING -eq "1")
if ($NoSignRequested -and $RequireSigningRequested) {
    throw "Signing cannot be both disabled and required."
}

$DoSign = $false
$SigningCertificate = $null
$ResolvedSignToolPath = $null
if (-not $NoSignRequested) {
    $SigningCertificate = Resolve-CodeSigningCertificate `
        -ExplicitThumbprint $SigningCertificateThumbprint
    if ($SigningCertificate) {
        $DoSign = $true
        $SigningCertificateThumbprint = $SigningCertificate.Thumbprint
        $ResolvedSignToolPath = Resolve-SignToolPath -ExplicitPath $SignToolPath
    } elseif ($RequireSigningRequested) {
        throw "A release signature is required, but no usable code-signing certificate was found. Sign in to the signing provider or configure a certificate thumbprint."
    }
}

if ($DoSign) {
    Write-Host "SignTool: $ResolvedSignToolPath" -ForegroundColor Gray
    Write-Host "Signing certificate: $($SigningCertificate.Subject)" -ForegroundColor Gray
} elseif ($NoSignRequested) {
    Write-Host "Signing: disabled explicitly; output is for local testing only" -ForegroundColor Yellow
} else {
    Write-Host "Signing: skipped because no usable code-signing certificate was found; output is for local testing only" -ForegroundColor Yellow
}

# ── Node/uv 运行时解析（与 build-exe.ps1 同一契约，共享 build-runtimes.psm1）──
# 1) 构建前把解析到的 Node 前置到 PATH，前端/Electron/MCP 的 npm 构建与
#    Python 打包链路使用同一个 Node 工具链；
# 2) 组装阶段（步骤 5）把 node-runtime / uv-runtime 绑进后端 exe 目录
#    （resources\backend），冻结入口 jiuwenswarm_exe_entry.py 会在
#    <后端 exe 目录>\runtime\ 下查找并前置 PATH，Agent 技能（ppt-creation
#    等 node 脚本）因此不依赖用户机器的 Node.js。
Import-Module (Join-Path $PSScriptRoot "build-runtimes.psm1") -Force
$ElectronMinimumNodeVersion = [version]"22.12.0"
$RuntimeSettings = Get-RuntimeBundleSettings -DefaultNodeVersion "v22.12.0"
$BundleNode = $RuntimeSettings.BundleNode
$BundleUv = $RuntimeSettings.BundleUv
$NodeVersion = $RuntimeSettings.NodeVersion
$NodeSource = Resolve-NodeRuntimeDir `
    -ProjectRoot $ProjectRoot `
    -ExplicitNodeDir $NodeDir `
    -NodeVersion $NodeVersion `
    -MinimumVersion $ElectronMinimumNodeVersion
Use-NodeRuntime -SourceDir $NodeSource

$FrontendDir = Join-Path $ProjectRoot "jiuwenswarm\channels\web\frontend"
$DesktopDir = Join-Path $ProjectRoot "jiuwenswarm\channels\desktop\electron"

# Resolve and synchronize the canonical build metadata before any dependency
# synchronization or npm installation, matching scripts/build-exe.ps1. The
# FrontendOnly path also needs these values for package.json and installer names.
$BuildConfigJson = uv run --no-project --python 3.11 python `
    "$ProjectRoot\scripts\build_config.py" --sync --emit-json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$BuildConfig = $BuildConfigJson | ConvertFrom-Json
if (-not $FrontendOnly) {
    uv run --no-project --python 3.11 python "$ProjectRoot\scripts\generate_build_info.py" --require-agent-core-branch
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$BuildDistDirName = [string]$BuildConfig.dist_dir_name
$BuildExecutableNameWindows = [string]$BuildConfig.executable_name_windows
$BuildVersion = [string]$BuildConfig.version
$BuildErrorLogName = [string]$BuildConfig.error_log_name
$BuildSetupBaseName = [string]$BuildConfig.setup_base_name
$BuildSetupFilename = [string]$BuildConfig.setup_filename
# The shell exe uses the canonical display name (WorkSwarm.exe), while the
# backend executable keeps its configured name (workswarm.exe).
$BuildDisplayName = [string]$BuildConfig.display_name
$ShellExeName = "$BuildDisplayName.exe"

# ── Resolve Electron runtime ─────────────────────────────────────────────────
# The Electron runtime is shipped inside channels/desktop/electron/node_modules
# after `npm install`. If the user provides -ElectronDir, use that instead.
Write-Host "=== WorkSwarm Electron Build ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot" -ForegroundColor Gray
Write-Host "Test: $Test  FrontendOnly: $FrontendOnly`n" -ForegroundColor Gray

# ── 1. Install Python dependencies ────────────────────────────────────────────
if (-not $FrontendOnly) {
    Write-Host "`n[1/6] Installing Python dependencies for Windows x64..." -ForegroundColor Yellow
    uv sync --locked --extra dev --extra cua --python-platform x86_64-pc-windows-msvc
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
        Write-Host "[build] node_modules missing, running npm ci..." -ForegroundColor Gray
        npm ci --prefer-offline --no-audit --no-fund
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
$DesktopInstallExit = 0
$LocalElectronExe = Join-Path $DesktopDir "node_modules\electron\dist\electron.exe"
$Rcedit = Join-Path $DesktopDir "node_modules\rcedit\bin\rcedit-x64.exe"
$NeedLocalElectron = [string]::IsNullOrWhiteSpace($ElectronDir)
$NeedDesktopInstall = (-not (Test-Path -LiteralPath $Rcedit -PathType Leaf)) -or `
    ($NeedLocalElectron -and -not (Test-Path -LiteralPath $LocalElectronExe -PathType Leaf))
if ($NeedDesktopInstall) {
    Push-Location $DesktopDir
    try {
        Write-Host "[desktop] locked Electron build dependencies missing, running npm ci..." -ForegroundColor Gray
        npm ci --prefer-offline --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { $DesktopInstallExit = $LASTEXITCODE }
        if ($DesktopInstallExit -eq 0 -and $NeedLocalElectron -and -not (Test-Path -LiteralPath $LocalElectronExe -PathType Leaf)) {
            $ElectronInstaller = Join-Path $DesktopDir "node_modules\electron\install.js"
            if (-not (Test-Path -LiteralPath $ElectronInstaller -PathType Leaf)) {
                throw "Electron binary installer not found after npm ci: $ElectronInstaller"
            }
            Write-Host "[desktop] Downloading the Electron binary..." -ForegroundColor Gray
            & node $ElectronInstaller
            if ($LASTEXITCODE -ne 0) { $DesktopInstallExit = $LASTEXITCODE }
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "[desktop] Locked Electron build dependencies already installed" -ForegroundColor Gray
}
if ($DesktopInstallExit -ne 0) { exit $DesktopInstallExit }
if ($NeedLocalElectron) {
    $ElectronDir = Join-Path $DesktopDir "node_modules\electron\dist"
}
if (-not (Test-Path -LiteralPath $Rcedit -PathType Leaf)) {
    throw "Pinned rcedit executable is missing after npm ci: $Rcedit"
}

$ElectronExe = Join-Path $ElectronDir "electron.exe"
if (-not (Test-Path -LiteralPath $ElectronExe -PathType Leaf)) {
    throw "electron.exe not found after installing desktop dependencies: $ElectronExe"
}
Write-Host "  Electron: $ElectronExe" -ForegroundColor Gray

# ── 4. PyInstaller: build backend exe ─────────────────────────────────────────
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
    $GatewayVerifier = Join-Path $ProjectRoot "scripts\verify_gateway_adapter_bundle.py"
    $GatewayVerifyProcess = Start-Process `
        -FilePath $FrozenExe `
        -ArgumentList @("`"$GatewayVerifier`"") `
        -Wait `
        -PassThru `
        -NoNewWindow
    if ($GatewayVerifyProcess.ExitCode -ne 0) {
        throw "Frozen Gateway adapter bundle verification failed. See ~/.jiuwenswarm/logs/$BuildErrorLogName"
    }

    $A2UIVerifier = Join-Path $ProjectRoot "scripts\verify_a2ui_bundle.py"
    $VerifyProcess = Start-Process `
        -FilePath $FrozenExe `
        -ArgumentList @("`"$A2UIVerifier`"") `
        -Wait `
        -PassThru `
        -NoNewWindow
    if ($VerifyProcess.ExitCode -ne 0) {
        throw "Frozen A2UI bundle verification failed. See ~/.jiuwenswarm/logs/$BuildErrorLogName"
    }

    $RsiVerifier = Join-Path $ProjectRoot "scripts\verify_rsi_bundle.py"
    $RsiVerifyProcess = Start-Process `
        -FilePath $FrozenExe `
        -ArgumentList @("`"$RsiVerifier`"") `
        -Wait `
        -PassThru `
        -NoNewWindow
    if ($RsiVerifyProcess.ExitCode -ne 0) {
        throw "Frozen RSI bundle verification failed. See ~/.jiuwenswarm/logs/$BuildErrorLogName"
    }

    # The PyInstaller spec intentionally allows platforms for which gitcode-cli
    # publishes no native binary (currently including Windows) and falls back to
    # the connector's pip-based initialization. Only run the frozen CLI verifier
    # when the binary was actually staged; otherwise the unconditional verifier
    # would make every Windows Electron build fail after PyInstaller succeeded.
    $BundledGitCodeExe = Join-Path $BackendDist "_internal\gitcode.exe"
    if (Test-Path -LiteralPath $BundledGitCodeExe -PathType Leaf) {
        $GitCodeVerifier = Join-Path $ProjectRoot "scripts\verify_gitcode_cli_bundle.py"
        $GitCodeVerifyProcess = Start-Process `
            -FilePath $FrozenExe `
            -ArgumentList @("`"$GitCodeVerifier`"") `
            -Wait `
            -PassThru `
            -NoNewWindow
        if ($GitCodeVerifyProcess.ExitCode -ne 0) {
            throw "Frozen GitCode CLI bundle verification failed. See ~/.jiuwenswarm/logs/$BuildErrorLogName"
        }
    } else {
        Write-Warning "gitcode-cli has no bundled Windows binary; the GitCode connector will use its existing pip fallback"
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

# Set the executable icon and Windows version resources before signing.
$LogoIco = Join-Path $FrontendDir "public\logo.ico"
if (-not (Test-Path -LiteralPath $LogoIco -PathType Leaf)) {
    throw "Required Windows icon is missing: $LogoIco"
}
$ShellExePath = Join-Path $ElectronAppDir $ShellExeName
Write-Host "  Setting exe icon and version resources via pinned rcedit..." -ForegroundColor Gray
& $Rcedit $ShellExePath `
    --set-icon $LogoIco `
    --set-version-string FileDescription $BuildDisplayName `
    --set-version-string ProductName $BuildDisplayName `
    --set-version-string InternalName $ShellExeName `
    --set-version-string OriginalFilename $ShellExeName `
    --set-file-version $BuildVersion `
    --set-product-version $BuildVersion
if ($LASTEXITCODE -ne 0) {
    throw "rcedit failed to set the application icon or version resources (exit code $LASTEXITCODE)."
}
if (-not (Test-PortableExecutable -Path $ShellExePath)) {
    throw "rcedit produced an invalid PE executable: $ShellExePath"
}

# Create resources/app directory for our Electron main code
$AppDir = Join-Path $ElectronAppDir "resources\app"
New-Item -ItemType Directory -Path $AppDir -Force | Out-Null

# Copy the mature Electron desktop shell (main.cjs, preload.cjs, launch.cjs, target_mcp_wrapper.cjs)
Write-Host "  Copying Electron desktop shell..." -ForegroundColor Gray
Copy-Item -Path (Join-Path $DesktopDir "main.cjs") -Destination $AppDir -Force
Copy-Item -Path (Join-Path $DesktopDir "browser_panels.cjs") -Destination $AppDir -Force
Copy-Item -Path (Join-Path $DesktopDir "share_image_export.cjs") -Destination $AppDir -Force
Copy-Item -Path (Join-Path $DesktopDir "display_media_picker.cjs") -Destination $AppDir -Force
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
            -ArgumentList @("`"$PlaywrightVerifier`"") `
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

$RequiredFiles = @(
    (Join-Path $ElectronAppDir $ShellExeName),
    (Join-Path $AppDir "main.cjs"),
    (Join-Path $AppDir "display_media_picker.cjs"),
    (Join-Path $AppDir "preload.cjs"),
    (Join-Path $AppDir "dist\index.html"),
    (Join-Path $AppDir "node_modules\@playwright\mcp\package.json"),
    (Join-Path $AppDir "node_modules\@playwright\mcp\cli.js")
)
if (-not $FrontendOnly) {
    $RequiredFiles += Join-Path $BackendDir $BuildExecutableNameWindows
    if (Test-Truthy $BundleNode) {
        $RequiredFiles += Join-Path $BackendDir "runtime\node-runtime\node.exe"
    }
    if (Test-Truthy $BundleUv) {
        $RequiredFiles += Join-Path $BackendDir "runtime\uv-runtime\uv.exe"
        $RequiredFiles += Join-Path $BackendDir "runtime\uv-runtime\uvx.exe"
    }
}
foreach ($RequiredFile in $RequiredFiles) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required assembled file is missing: $RequiredFile"
    }
}

# Sign only after icon/resource mutation and runtime assembly are complete.
# Existing valid vendor signatures are preserved; the renamed Electron shell is
# deliberately re-signed with the WorkSwarm release certificate.
if ($DoSign) {
    Write-Host "  Signing assembled Electron PE payloads..." -ForegroundColor Yellow
    Invoke-CodeSigning `
        -Root $ElectronAppDir `
        -ResolvedSignTool $ResolvedSignToolPath `
        -CertificateThumbprint $SigningCertificateThumbprint `
        -TimestampServer $TimestampUrl `
        -ForceResignPaths @($ShellExePath)
} else {
    Write-Host "  Skipping PE signing (unsigned local build)." -ForegroundColor Yellow
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
    $Winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw "Inno Setup was not found and winget is unavailable. Install Inno Setup 6.7.1 and rerun the build."
    }
    # Use the versioned winget manifest and its installer SHA-256 verification.
    # Never bypass a hash failure or execute an unverified direct download.
    Write-Host "Installing Inno Setup 6.7.1 through winget..." -ForegroundColor Yellow
    & $Winget.Source install --id JRSoftware.InnoSetup --exact --version 6.7.1 `
        --source winget --scope user --silent --disable-interactivity `
        --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup installation failed (winget exit code: $LASTEXITCODE)."
    }
    $Iscc = $IsccPaths | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Iscc) {
        $Iscc = Get-Command iscc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
    }
    if (-not $Iscc) {
        throw "winget completed but ISCC.exe was not found. Install Inno Setup 6.7.1 and rerun the build."
    }
}

Write-Host "Using ISCC: $Iscc" -ForegroundColor Gray

# Test/FrontendOnly build uses a different installer filename to distinguish from release
# 应用名 / 壳 exe 名 / 版本号 / 后端 exe 名经 /D 传入，iss 内不再硬编码
# （与 build_config 单一来源；后端 exe 名供卸载时的 --desktop-reset-external-cli-config 使用）
if ($FrontendOnly) {
    $InstallerFileName = "$BuildDisplayName-frontend-test-$BuildVersion.exe"
} elseif ($Test) {
    $InstallerFileName = "$BuildDisplayName-test-$BuildVersion.exe"
} else {
    $InstallerFileName = $BuildSetupFilename
}
$InstallerPath = Join-Path $ProjectRoot "dist\$InstallerFileName"
if (Test-Path -LiteralPath $InstallerPath) {
    Remove-Item -LiteralPath $InstallerPath -Force
}

$IsccArguments = @(
    "/DMyAppName=$BuildDisplayName",
    "/DMyAppExeName=$ShellExeName",
    "/DMyAppVersion=$BuildVersion",
    "/DBackendExecutableName=$BuildExecutableNameWindows",
    "/DBuildSetupBaseName=$BuildSetupBaseName"
)
if ($FrontendOnly) { $IsccArguments += "/DELECTRON_FRONTEND_ONLY" }
elseif ($Test) { $IsccArguments += "/DELECTRON_TEST_BUILD" }
if ($DoSign) {
    $IsccArguments += "/DBuildSignToolName=certumsha256"
    $InnoSignCommand = '$q' + $ResolvedSignToolPath + '$q sign' +
        " /sha1 $SigningCertificateThumbprint /fd SHA256" +
        " /tr $TimestampUrl /td SHA256 /v " + '$f'
    $IsccArguments += "/Scertumsha256=$InnoSignCommand"
}
$IsccArguments += "$ProjectRoot\scripts\installer-electron.iss"
& $Iscc @IsccArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
    throw "Inno Setup completed without creating the expected installer: $InstallerPath"
}

if ($DoSign) {
    Assert-ValidEmbeddedSignature `
        -Path $InstallerPath `
        -ResolvedSignTool $ResolvedSignToolPath `
        -ExpectedThumbprint $SigningCertificateThumbprint `
        -RequireTimestamp
}

Write-Host "`n=== Build complete ===" -ForegroundColor Green
Write-Host "Installer: $InstallerPath" -ForegroundColor Green
Write-Host "Size: $([math]::Round((Get-Item $InstallerPath).Length / 1MB, 1)) MB" -ForegroundColor Green
Write-Host "SHA-256: $((Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash)" -ForegroundColor Green
if ($DoSign) {
    Write-Host "Signature: valid and timestamped" -ForegroundColor Green
} else {
    Write-Host "Signature: not applied; this installer is for local testing only" -ForegroundColor Yellow
}
} finally {
    if ($null -eq $PreviousProcessorArchitecture) {
        Remove-Item Env:\PROCESSOR_ARCHITECTURE -ErrorAction SilentlyContinue
    } else {
        $env:PROCESSOR_ARCHITECTURE = $PreviousProcessorArchitecture
    }
}
