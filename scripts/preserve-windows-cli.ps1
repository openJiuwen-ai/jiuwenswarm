#requires -Version 5.1
# Preserve complete Node prefixes before an installer replaces application files.
# Never execute npm or package code, merge into user npm, or remove source data.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [ValidateSet('Preserve','Initialize')][string]$Mode = 'Preserve',
    [Parameter(Mandatory=$true)][string]$ReportPath
)
$ErrorActionPreference = 'Stop'
# Use this interpreter's built-in modules even when Setup was launched from pwsh.
$env:PSModulePath=Join-Path $PSHOME 'Modules'
$utf8 = New-Object Text.UTF8Encoding($false)
$safeRoot=$false

function Full-Path([string]$Path) {
    [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path)).TrimEnd('\')
}
function Long-Path([string]$Path) {
    if ($Path.StartsWith('\\?\')) { return $Path }
    $full=[IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\')) { return '\\?\UNC\'+$full.Substring(2) }
    '\\?\'+$full
}
function Normal-Path([string]$Path) {
    if ($Path.StartsWith('\\?\UNC\')) { return '\\'+$Path.Substring(8) }
    if ($Path.StartsWith('\\?\')) { return $Path.Substring(4) }
    $Path
}
function Inside([string]$Path, [string]$Root) {
    $a=Full-Path $Path; $b=Full-Path $Root
    $a.Equals($b,[StringComparison]::OrdinalIgnoreCase) -or $a.StartsWith($b+'\',[StringComparison]::OrdinalIgnoreCase)
}
function Check-Parents([string]$Path) {
    $current=Full-Path $Path
    while ($current) {
        $extended=Long-Path $current
        if ([IO.Directory]::Exists($extended) -or [IO.File]::Exists($extended)) {
            if ([IO.File]::GetAttributes($extended) -band [IO.FileAttributes]::ReparsePoint) { throw "Linked path requires manual preservation: $current" }
        }
        $parent=[IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current=$parent
    }
}
function Tree-Files([string]$Root) {
    foreach ($entry in [IO.Directory]::GetFileSystemEntries((Long-Path $Root))) {
        $attributes=[IO.File]::GetAttributes($entry)
        if ($attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked content requires manual preservation: $entry" }
        if ($attributes -band [IO.FileAttributes]::Directory) { Tree-Files (Normal-Path $entry) }
        else { [pscustomobject]@{FullName=(Normal-Path $entry); Length=(New-Object IO.FileInfo($entry)).Length} }
    }
}
function File-Hash([string]$Path) {
    $stream=[IO.File]::Open((Long-Path $Path),[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $sha=[Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}
function Text-Hash([string]$Value) {
    $sha=[Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash($utf8.GetBytes($Value)))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function Inventory([string]$Root) {
    $rows=@(foreach ($file in @(Tree-Files $Root | Sort-Object FullName)) {
        [pscustomobject]@{path=$file.FullName.Substring($Root.Length+1); length=$file.Length; sha256=(File-Hash $file.FullName)}
    })
    ConvertTo-Json -InputObject $rows -Depth 4 -Compress
}
function Write-Json([string]$Path, $Value) {
    [IO.File]::WriteAllText((Long-Path $Path),(ConvertTo-Json -InputObject $Value -Depth 8),$utf8)
}
function Copy-Tree([string]$Source, [string]$Target) {
    [IO.Directory]::CreateDirectory((Long-Path $Target)) | Out-Null
    foreach ($entry in [IO.Directory]::GetFileSystemEntries((Long-Path $Source))) {
        $attributes=[IO.File]::GetAttributes($entry)
        if ($attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked content requires manual preservation: $entry" }
        $destination=Join-Path $Target ([IO.Path]::GetFileName($entry))
        if ($attributes -band [IO.FileAttributes]::Directory) { Copy-Tree (Normal-Path $entry) $destination }
        else { [IO.File]::Copy($entry,(Long-Path $destination),$false) }
    }
}
function Assert-PrefixIdle([string]$Source) {
    foreach ($process in @(Get-CimInstance -ClassName Win32_Process)) {
        $executable=[string]$process.ExecutablePath
        $command=([string]$process.CommandLine).Replace('/','\')
        if (($executable -and (Inside $executable $Source)) -or
            $command.IndexOf($Source,[StringComparison]::OrdinalIgnoreCase) -ge 0) {
            throw "Close the CLI process using $Source before upgrading (PID $($process.ProcessId))."
        }
    }
}
function Preserve-Prefix([string]$Source) {
    $Source=Full-Path $Source
    # Read attributes instead of suppressing access/long-path errors as absence.
    try { $attributes=[IO.File]::GetAttributes((Long-Path $Source)) }
    catch [IO.FileNotFoundException] { return $null }
    catch [IO.DirectoryNotFoundException] { return $null }
    if (-not ($attributes -band [IO.FileAttributes]::Directory)) { throw "Expected a Node directory: $Source" }
    Check-Parents $Source
    Assert-PrefixIdle $Source
    $before=Inventory $Source
    $digest=Text-Hash $before
    $id=(Text-Hash $Source.ToLowerInvariant()).Substring(0,8)+'-'+$digest.Substring(0,16)
    $target=Join-Path $store $id
    $prefix=Join-Path $target 'prefix'
    Check-Parents $prefix
    $installed=Join-Path $npmRoot 'installed'
    $activeDirectory=Join-Path $installed $id
    $active=Join-Path $activeDirectory 'prefix'
    $previous=@(foreach ($directory in [IO.Directory]::GetDirectories((Long-Path $store))) {
        if ([IO.Path]::GetFileName($directory).StartsWith('.')) { continue }
        $record=Join-Path (Normal-Path $directory) 'mapping.json'
        Check-Parents $record
        $saved=[IO.File]::ReadAllText((Long-Path $record)) | ConvertFrom-Json
        if ($saved.schema -ne 1 -or -not $saved.source -or -not $saved.target) { throw "Invalid CLI mapping: $record" }
        if ($saved.source -eq $Source -and $saved.target -ne $active -and (Inside $saved.target $installed)) { [string]$saved.target }
    })
    if (Test-Path -LiteralPath $target) {
        $manifest=Get-Content -LiteralPath (Join-Path $target 'manifest.json') -Raw | ConvertFrom-Json
        if ($manifest.source -ne $Source -or $manifest.digest -ne $digest -or (Inventory $prefix) -ne $before) { throw "Existing preservation is incomplete or changed: $target" }
    } else {
        $stage=Join-Path $store ('.pending-'+[guid]::NewGuid().ToString('N'))
        $copy=Join-Path $stage 'prefix'
        # Pending/failed copies are intentionally retained for diagnosis/recovery.
        Copy-Tree $Source $copy
        if ((Inventory $copy) -ne $before -or (Inventory $Source) -ne $before) { throw "Source changed or preservation verification failed: $Source" }
        Assert-PrefixIdle $Source
        $manifest=[ordered]@{schema=1; source=$Source; target=$active; backup=$prefix; digest=$digest; files=($before | ConvertFrom-Json); created_utc=[DateTime]::UtcNow.ToString('o')}
        Write-Json (Join-Path $stage 'manifest.json') $manifest
        Write-Json (Join-Path $stage 'mapping.json') @{schema=1; source=$Source; target=$active}
        [IO.Directory]::Move((Long-Path $stage),(Long-Path $target))
    }
    # The verified backup is never placed on PATH. A separate working copy may
    # be updated by the CLI without damaging the recovery material.
    Check-Parents $active
    if (Test-Path -LiteralPath $activeDirectory) {
        $origin=Get-Content -LiteralPath (Join-Path $activeDirectory 'origin.json') -Raw | ConvertFrom-Json
        if ($origin.source -ne $Source -or $origin.initial_digest -ne $digest) { throw "Conflicting CLI destination: $activeDirectory" }
        if (-not [IO.Directory]::Exists((Long-Path $active)) -or
            ([IO.File]::Exists((Long-Path (Join-Path $Source 'node.exe'))) -and
             -not [IO.File]::Exists((Long-Path (Join-Path $active 'node.exe'))))) {
            throw "The existing working CLI prefix is incomplete: $active"
        }
    } else {
        [IO.Directory]::CreateDirectory((Long-Path $installed)) | Out-Null
        $stage=Join-Path $installed ('.pending-'+[guid]::NewGuid().ToString('N'))
        Copy-Tree $prefix (Join-Path $stage 'prefix')
        if ((Inventory (Join-Path $stage 'prefix')) -ne $before) { throw "Working copy verification failed: $Source" }
        Write-Json (Join-Path $stage 'origin.json') @{source=$Source; initial_digest=$digest}
        [IO.Directory]::Move((Long-Path $stage),(Long-Path $activeDirectory))
    }
    [pscustomobject]@{source=$Source; target=$active; backup=$prefix; digest=$digest; previous=$previous}
}
function Add-UserPaths($Mappings, $Additional) {
    if (-not @($Mappings).Count -and -not @($Additional).Count) { return }
    $key=[Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try {
        $raw=[string]$key.GetValue('Path','',[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $kind=[Microsoft.Win32.RegistryValueKind]::ExpandString
        if ($key.GetValueNames() -contains 'Path') { $kind=$key.GetValueKind('Path') }
        $parts=New-Object 'Collections.Generic.List[string]'
        $seen=@{}
        $existing=@{}
        foreach ($part in @($raw -split ';')) {
            $identity=[Environment]::ExpandEnvironmentVariables($part.Trim().Trim('"')).TrimEnd('\').ToLowerInvariant()
            $existing[$identity]=$true
        }
        foreach ($part in @($raw -split ';')) {
            $value=$part
            $expanded=[Environment]::ExpandEnvironmentVariables($part.Trim().Trim('"')).TrimEnd('\')
            foreach ($mapping in @($Mappings)) {
                foreach ($suffix in @('','\node_modules\.bin')) {
                    if ($expanded.Equals($mapping.source+$suffix,[StringComparison]::OrdinalIgnoreCase)) { $value=$mapping.target+$suffix }
                    foreach ($old in @($mapping.previous)) {
                        $newPath=$mapping.target+$suffix
                        $identity=$newPath.ToLowerInvariant()
                        if ($expanded.Equals($old+$suffix,[StringComparison]::OrdinalIgnoreCase) -and
                            -not $existing.ContainsKey($identity) -and -not $seen.ContainsKey($identity)) {
                            $parts.Add($newPath); $seen[$identity]=$true
                        }
                    }
                }
            }
            $identity=[Environment]::ExpandEnvironmentVariables($value.Trim().Trim('"')).TrimEnd('\').ToLowerInvariant()
            $parts.Add($value); $seen[$identity]=$true
        }
        foreach ($part in @($Additional)) {
            if (-not $part) { continue }
            $identity=(Full-Path $part).ToLowerInvariant()
            if (-not $seen.ContainsKey($identity)) { $parts.Add($part); $seen[$identity]=$true }
        }
        # npm installs write into global; activate those commands ahead of all
        # preserved generations, including an old layout absent after cleanup.
        $managed=Join-Path $npmRoot 'global'
        if (Test-Path -LiteralPath (Join-Path $managed '.workswarm-prefix.json') -PathType Leaf) {
            $ordered=New-Object 'Collections.Generic.List[string]'
            $inserted=$false
            $owned=(Join-Path $npmRoot 'installed')+'\'
            foreach ($part in $parts) {
                $expanded=[Environment]::ExpandEnvironmentVariables($part.Trim().Trim('"')).Replace('/','\').TrimEnd('\')
                if ($expanded.Equals($managed,[StringComparison]::OrdinalIgnoreCase)) {
                    if (-not $inserted) { $ordered.Add($managed); $inserted=$true }
                    continue
                }
                if (-not $inserted -and $expanded.StartsWith($owned,[StringComparison]::OrdinalIgnoreCase)) {
                    $ordered.Add($managed); $inserted=$true
                }
                $ordered.Add($part)
            }
            if (-not $inserted) { $ordered.Add($managed) }
            $parts=$ordered
        }
        $updated=$parts -join ';'
        if ($updated -ne $raw) {
            # Save the unexpanded original before changing only HKCU Path.
            $journal=Join-Path $npmRoot ('path-before-'+[guid]::NewGuid().ToString('N')+'.json')
            Write-Json $journal @{raw=$raw; kind=[string]$kind; updated=$updated}
            $key.SetValue('Path',$updated,$kind)
        }
    } finally { $key.Dispose() }
}
try {
    $InstallDir=Full-Path $InstallDir
    $data=$env:JIUWENSWARM_DATA_DIR
    if (-not $data) {
        $homeDirectory=$env:JIUWENSWARM_HOME
        if (-not $homeDirectory) { $homeDirectory=$env:USERPROFILE }
        $data=Join-Path $homeDirectory '.jiuwenswarm'
    }
    $npmRoot=Full-Path (Join-Path $data 'runtime\npm')
    if (Inside $npmRoot $InstallDir) { throw 'The CLI preservation directory must be outside the installation directory.' }
    Check-Parents $npmRoot
    $safeRoot=$true
    $store=Join-Path $npmRoot 'preserved'
    New-Item -ItemType Directory -Path $store -Force | Out-Null
    $lockPath=Join-Path $npmRoot '.migration.lock'
    $lock=[IO.File]::Open($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    try {
        $mappings=@()
        foreach ($relative in @('runtime\node-runtime','resources\backend\runtime\node-runtime')) {
            $mapping=Preserve-Prefix (Join-Path $InstallDir $relative)
            if ($mapping) { $mappings += $mapping }
        }
        $paths=@()
        if ($Mode -eq 'Initialize') {
            $globalPrefix=Join-Path $npmRoot 'global'
            Check-Parents $globalPrefix
            if (-not (Test-Path -LiteralPath $globalPrefix)) {
                $nodeSource=@($mappings | Where-Object { Test-Path -LiteralPath (Join-Path $_.target 'node.exe') -PathType Leaf } | Select-Object -Last 1)
                if ($nodeSource.Count) {
                    $stage=Join-Path $npmRoot ('.global-pending-'+[guid]::NewGuid().ToString('N'))
                    Copy-Tree $nodeSource[0].backup $stage
                    if ((Inventory $stage) -ne (Inventory $nodeSource[0].backup)) { throw 'Managed npm prefix verification failed.' }
                    Write-Json (Join-Path $stage '.workswarm-prefix.json') @{schema=1; source=$nodeSource[0].source; node_sha256=(File-Hash (Join-Path $stage 'node.exe'))}
                    [IO.Directory]::Move((Long-Path $stage),(Long-Path $globalPrefix))
                }
            }
            if (Test-Path -LiteralPath $globalPrefix) {
                foreach ($required in @('node.exe','node_modules\npm\bin\npm-cli.js','node_modules\npm\bin\npx-cli.js','.workswarm-prefix.json')) {
                    if (-not (Test-Path -LiteralPath (Join-Path $globalPrefix $required) -PathType Leaf)) { throw 'Managed npm prefix is incomplete; preserving it for repair.' }
                }
                $bin=Join-Path $npmRoot 'bin'
                Check-Parents $bin
                New-Item -ItemType Directory -Path $bin -Force | Out-Null
                foreach ($command in @('npm','npx')) {
                    $cli=if ($command -eq 'npm') {'npm-cli.js'} else {'npx-cli.js'}
                    $body='@"%~dp0..\global\node.exe" "%~dp0..\global\node_modules\npm\bin\'+$cli+'" %*'+"`r`n"
                    [IO.File]::WriteAllText((Join-Path $bin ($command+'.cmd')),$body,$utf8)
                    $sh='#!/bin/sh'+"`n"+'basedir=$(dirname "$0")'+"`n"+'exec "$basedir/../global/node.exe" "$basedir/../global/node_modules/npm/bin/'+$cli+'" "$@"'+"`n"
                    [IO.File]::WriteAllText((Join-Path $bin $command),$sh,$utf8)
                }
                $paths += $globalPrefix
            }
        }
        foreach ($mapping in $mappings) {
            $paths += $mapping.target
            $localBin=Join-Path $mapping.target 'node_modules\.bin'
            if (Test-Path -LiteralPath $localBin -PathType Container) { $paths += $localBin }
            # Keep earlier working copies as fallbacks for packages independently
            # installed/self-updated there, after the newest source generation.
            foreach ($previous in @($mapping.previous)) {
                if ([IO.Directory]::Exists((Long-Path $previous))) { $paths += $previous }
                $previousBin=Join-Path $previous 'node_modules\.bin'
                if ([IO.Directory]::Exists((Long-Path $previousBin))) { $paths += $previousBin }
            }
        }
        Add-UserPaths $mappings $paths
        Write-Json $ReportPath @{success=$true; mode=$Mode; mappings=$mappings; npm_root=$npmRoot}
    } finally { $lock.Dispose() }
} catch {
    $message=$_.Exception.Message+"`r`n"+$_.ScriptStackTrace
    [IO.File]::WriteAllText($ReportPath,$message,$utf8)
    if ($safeRoot -and [IO.Directory]::Exists((Long-Path $npmRoot))) {
        $failure=Join-Path $npmRoot ('failure-'+[guid]::NewGuid().ToString('N')+'.log')
        [IO.File]::WriteAllText((Long-Path $failure),$message,$utf8)
    }
    exit 1
}
