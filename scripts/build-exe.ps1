# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# Windows 打包 exe 脚本
# 用法: .\scripts\build-exe.ps1  或  pwsh -File scripts\build-exe.ps1

param(
    [string]$NodeDir = "",
    [string]$SigningCertificateThumbprint = "",
    [string]$SignToolPath = "",
    [string]$TimestampUrl = "http://time.certum.pl",
    [switch]$NoSign,
    [switch]$RequireSigning
)

$ErrorActionPreference = "Stop"

# 控制台 UTF-8，避免中文 echo 乱码（PowerShell 5.1 默认编码易乱码）
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

function Assert-ValidAuthenticodeSignature {
    param(
        [Parameter(Mandatory)][string]$Path,
        [string]$ExpectedThumbprint = "",
        [switch]$RequireTimestamp
    )

    $Signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "Authenticode verification failed for '$Path': $($Signature.Status) $($Signature.StatusMessage)"
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedThumbprint)) {
        $ActualThumbprint = [string]$Signature.SignerCertificate.Thumbprint
        if ($ActualThumbprint -ne $ExpectedThumbprint) {
            throw "Unexpected signing certificate for '$Path': $ActualThumbprint"
        }
    }
    if ($RequireTimestamp -and -not $Signature.TimeStamperCertificate) {
        throw "The Authenticode signature for '$Path' does not contain a timestamp."
    }
}

function Invoke-CodeSigning {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$ResolvedSignTool,
        [Parameter(Mandatory)][string]$CertificateThumbprint,
        [Parameter(Mandatory)][string]$TimestampServer
    )

    $PortableExecutables = @(
        Get-ChildItem -LiteralPath $Root -Recurse -File |
            Where-Object { Test-PortableExecutable -Path $_.FullName } |
            Sort-Object FullName
    )
    if ($PortableExecutables.Count -eq 0) {
        throw "No PE files were found under '$Root'."
    }

    $UnsignedFiles = [System.Collections.Generic.List[System.IO.FileInfo]]::new()
    $AlreadySignedCount = 0
    foreach ($File in $PortableExecutables) {
        $Signature = Get-AuthenticodeSignature -LiteralPath $File.FullName
        if ($Signature.Status -eq [System.Management.Automation.SignatureStatus]::Valid) {
            $AlreadySignedCount++
        } elseif ($Signature.Status -eq [System.Management.Automation.SignatureStatus]::NotSigned) {
            $UnsignedFiles.Add($File)
        } else {
            throw "Refusing to replace the invalid signature on '$($File.FullName)': $($Signature.Status)"
        }
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

    foreach ($File in $PortableExecutables) {
        Assert-ValidAuthenticodeSignature -Path $File.FullName
    }
    foreach ($File in $UnsignedFiles) {
        Assert-ValidAuthenticodeSignature `
            -Path $File.FullName `
            -ExpectedThumbprint $CertificateThumbprint `
            -RequireTimestamp
    }

    Write-Host (
        "[sign] PE files: {0}; newly signed: {1}; existing valid signatures: {2}" -f `
            $PortableExecutables.Count, $UnsignedFiles.Count, $AlreadySignedCount
    ) -ForegroundColor Gray
}

# 项目根 = 脚本所在目录的上一层，基于脚本自身位置推导，换路径不坏
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

$DetectedMachine = uv run --no-project --python 3.11 python -c "import platform; print(platform.machine())"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$DetectedMachine = ([string]$DetectedMachine).Trim()
if ($DetectedMachine -ne "AMD64") {
    throw "Windows x64 packaging requires platform.machine() == AMD64; detected '$DetectedMachine'."
}

Write-Host "Architecture: $DetectedMachine" -ForegroundColor Gray

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

# Sign every PE payload only after all bundled runtimes are in place. Existing
# valid vendor signatures are preserved; unsigned PE files receive our release
# signature and timestamp before Inno Setup compresses them.
if ($DoSign) {
    Write-Host "`n[3.7/4] Signing frozen PE payloads..." -ForegroundColor Yellow
    Invoke-CodeSigning `
        -Root $FrozenDir `
        -ResolvedSignTool $ResolvedSignToolPath `
        -CertificateThumbprint $SigningCertificateThumbprint `
        -TimestampServer $TimestampUrl
} else {
    Write-Host "`n[3.7/4] Skipping PE signing (unsigned local build)." -ForegroundColor Yellow
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
$InnoArguments = @($InnoDefines)
if ($DoSign) {
    $InnoArguments += "/DBuildSignToolName=certumsha256"
    $InnoSignCommand = '$q' + $ResolvedSignToolPath + '$q sign' +
        " /sha1 $SigningCertificateThumbprint /fd SHA256" +
        " /tr $TimestampUrl /td SHA256 /v " + '$f'
    $InnoArguments += "/Scertumsha256=$InnoSignCommand"
}
$InnoArguments += "$ProjectRoot\scripts\installer.iss"
& $Iscc @InnoArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$InstallerPath = Join-Path $ProjectRoot "dist\$BuildSetupFilename"
if (-not (Test-Path -LiteralPath $InstallerPath)) {
    throw "Installer was not created at the configured path: $InstallerPath"
}

if ($DoSign) {
    Assert-ValidAuthenticodeSignature `
        -Path $InstallerPath `
        -ExpectedThumbprint $SigningCertificateThumbprint `
        -RequireTimestamp
    & $ResolvedSignToolPath verify /pa /all /v $InstallerPath
    if ($LASTEXITCODE -ne 0) {
        throw "SignTool verification failed for installer '$InstallerPath'."
    }
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
        Remove-Item Env:PROCESSOR_ARCHITECTURE -ErrorAction SilentlyContinue
    } else {
        $env:PROCESSOR_ARCHITECTURE = $PreviousProcessorArchitecture
    }
}
