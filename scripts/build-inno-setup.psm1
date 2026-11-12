# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# Inno Setup compiler discovery and verified bootstrap.
# Shared by build-exe.ps1 and build-electron-exe.ps1 so the pinned installer
# version and SHA-256 have a single source.

$ErrorActionPreference = "Stop"

function Resolve-InnoSetupCompiler {
    # Prefer Inno Setup 7: it removes the MAX_PATH limit for both source and
    # install paths. Inno Setup 6 remains a fallback for existing build hosts.
    $Candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
        "C:\Program Files\Inno Setup 7\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 7\ISCC.exe"
    )
    # Check PATH before the Inno Setup 6 default locations so a compiler
    # exposed through PATH is not downgraded to an installed Inno Setup 6.
    $Command = Get-Command iscc -ErrorAction SilentlyContinue
    if ($Command) {
        $Candidates += $Command.Source
    }
    $Candidates += @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    )

    return $Candidates |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and (Test-Path -LiteralPath $_) } |
        Select-Object -First 1
}

function Install-InnoSetup7 {
    # Pin the installer and verify its SHA-256 before execution so a missing
    # compiler never turns into an unverified download-and-run.
    $Version = "7.1.1"
    $InstallerUri = "https://github.com/jrsoftware/issrc/releases/download/is-7_1_1/innosetup-7.1.1-x64.exe"
    $ExpectedSha256 = "8a5a6711c3766a6e5fb74f280ba0e3551e719ce94538d901fdbd1f8f9920b3f0"

    $DownloadDirectory = Join-Path $env:TEMP ("jiuwenswarm-inno-setup-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $DownloadDirectory -Force | Out-Null
    try {
        $InstallerPath = Join-Path $DownloadDirectory "innosetup-$Version-x64.exe"
        Write-Host "Downloading Inno Setup $Version (x64)..." -ForegroundColor Yellow
        try {
            Invoke-WebRequest -Uri $InstallerUri -OutFile $InstallerPath -UseBasicParsing -TimeoutSec 300
        } catch {
            throw (
                "Unable to download Inno Setup $Version from '$InstallerUri' within 300 seconds: " +
                $_.Exception.Message
            )
        }

        $ActualSha256 = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash
        if ($ActualSha256 -ne $ExpectedSha256) {
            throw (
                "Inno Setup $Version download failed SHA-256 verification " +
                "(expected $ExpectedSha256, got $ActualSha256)."
            )
        }

        Write-Host "Installing Inno Setup $Version (silent)..." -ForegroundColor Yellow
        $InstallProcess = Start-Process `
            -FilePath $InstallerPath `
            -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/SP-" `
            -Wait `
            -PassThru `
            -NoNewWindow
        if ($InstallProcess.ExitCode -ne 0) {
            throw "Inno Setup $Version installation failed (exit code $($InstallProcess.ExitCode))."
        }
    } finally {
        Remove-Item -LiteralPath $DownloadDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Get-InnoSetupMajorVersion {
    param([Parameter(Mandatory)][string]$IsccPath)

    # ISCC.exe carries no version resource (ProductVersion is 0.0.0.0), so ask
    # the compiler itself. Inno Setup 7 prints the bare version; version 6 does
    # not support --version, prints its banner, and reports "Unknown option" on
    # stderr. Capture that without tripping the module's Stop preference.
    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $Output = @(& $IsccPath --version 2>&1)
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }

    foreach ($Line in $Output) {
        $Text = [string]$Line
        if ($Text -match '^\s*(\d+)\.') {
            return [int]$Matches[1]
        }
        if ($Text -match 'Inno Setup\s+(\d+)') {
            return [int]$Matches[1]
        }
    }
    return $null
}

function Test-InnoSetup7OrNewer {
    param([Parameter(Mandatory)][string]$IsccPath)

    $MajorVersion = Get-InnoSetupMajorVersion -IsccPath $IsccPath
    if ($null -ne $MajorVersion) {
        return $MajorVersion -ge 7
    }
    # Fall back to the install layout when the compiler output cannot be parsed.
    return (Split-Path -Leaf (Split-Path -Parent $IsccPath)) -like "Inno Setup 7*"
}

Export-ModuleMember -Function Resolve-InnoSetupCompiler, Install-InnoSetup7, Get-InnoSetupMajorVersion, Test-InnoSetup7OrNewer
