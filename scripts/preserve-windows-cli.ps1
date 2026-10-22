#requires -Version 5.1
# Preserve complete Node prefixes before an installer replaces application files.
# Never execute npm or package code, merge into user npm, or remove source data.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [ValidateSet('Preserve','Initialize','PreserveConfig')][string]$Mode = 'Preserve',
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
function Bytes-Hash([byte[]]$Value) {
    $sha=[Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash($Value))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function Text-Hash([string]$Value) { Bytes-Hash ($utf8.GetBytes($Value)) }
function Inventory([string]$Root) {
    $rows=@(foreach ($file in @(Tree-Files $Root | Sort-Object FullName)) {
        [pscustomobject]@{path=$file.FullName.Substring($Root.Length+1); length=$file.Length; sha256=(File-Hash $file.FullName)}
    })
    ConvertTo-Json -InputObject $rows -Depth 4 -Compress
}
function Write-Json([string]$Path, $Value) {
    [IO.File]::WriteAllText((Long-Path $Path),(ConvertTo-Json -InputObject $Value -Depth 8),$utf8)
}
function Preserve-UpgradeConfig() {
    # Legacy uninstallers reset agent configuration. Keep a user-bound encrypted
    # snapshot only around that uninstall; normal upgrades/uninstalls do not use it.
    $root=Join-Path $dataRoot 'runtime\upgrade-config'
    Check-Parents $root
    [IO.Directory]::CreateDirectory((Long-Path $root)) | Out-Null
    $pending=Join-Path $root 'pending.json'
    Check-Parents $pending
    $config=Join-Path $dataRoot 'config\config.yaml'
    Check-Parents $config
    if (-not [IO.File]::Exists((Long-Path $config))) { return }
    $bytes=[IO.File]::ReadAllBytes((Long-Path $config))
    $before=Bytes-Hash $bytes
    if ([IO.File]::Exists((Long-Path $pending))) {
        $saved=[IO.File]::ReadAllText((Long-Path $pending)) | ConvertFrom-Json
        if ($saved.before_sha256 -ne $before) { throw 'A previous administrator upgrade configuration needs recovery first.' }
    }
    Add-Type -AssemblyName System.Security
    $encrypted=[Security.Cryptography.ProtectedData]::Protect($bytes,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
    $record=@{schema=1; config=$config; before_sha256=$before; encrypted=[Convert]::ToBase64String($encrypted)}
    if ((File-Hash $config) -ne $before) { throw 'Configuration changed while preserving it.' }
    $stage=Join-Path $root ('.pending-'+[guid]::NewGuid().ToString('N'))
    Write-Json $stage $record
    if ([IO.File]::Exists((Long-Path $pending))) { [IO.File]::Replace((Long-Path $stage),(Long-Path $pending),[NullString]::Value) }
    else { [IO.File]::Move((Long-Path $stage),(Long-Path $pending)) }
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
function Assert-PrefixIdle([string]$Source, [switch]$ExecutableOnly) {
    foreach ($process in @(Get-CimInstance -ClassName Win32_Process)) {
        if ($process.ProcessId -eq $PID) { continue }
        $executable=[string]$process.ExecutablePath
        $command=([string]$process.CommandLine).Replace('/','\')
        if (($executable -and (Inside $executable $Source)) -or
            (-not $ExecutableOnly -and $command.IndexOf($Source,[StringComparison]::OrdinalIgnoreCase) -ge 0)) {
            throw "Close the CLI process using $Source before upgrading (PID $($process.ProcessId))."
        }
    }
}
function Preserve-Prefix([string]$Source, [string]$RuntimeName = '') {
    $Source=Full-Path $Source
    # Read attributes instead of suppressing access/long-path errors as absence.
    try { $attributes=[IO.File]::GetAttributes((Long-Path $Source)) }
    catch [IO.FileNotFoundException] { return $null }
    catch [IO.DirectoryNotFoundException] { return $null }
    if (-not ($attributes -band [IO.FileAttributes]::Directory)) { throw "Expected a runtime directory: $Source" }
    Check-Parents $Source
    Assert-PrefixIdle $Source
    $before=Inventory $Source
    $digest=Text-Hash $before
    $id=(Text-Hash $Source.ToLowerInvariant()).Substring(0,8)+'-'+$digest.Substring(0,16)
    $sdk=[bool]$RuntimeName
    $installed=Join-Path $npmRoot 'installed'
    if ($sdk) {
        $store=Join-Path $dataRoot 'runtime\external-cli-preserved'
        $installed=Join-Path $dataRoot 'runtime\external-cli'
        Check-Parents $store
        Check-Parents $installed
        [IO.Directory]::CreateDirectory((Long-Path $store)) | Out-Null
    }
    $target=Join-Path $store $id
    $prefix=Join-Path $target 'prefix'
    Check-Parents $prefix
    $activeDirectory=Join-Path $installed $id
    $active=Join-Path $activeDirectory 'prefix'
    if ($sdk) {
        $activeDirectory=Join-Path $installed $RuntimeName
        $active=Join-Path $activeDirectory 'site-packages'
        if (-not (Inside $active $installed)) { throw 'SDK destination escapes the user runtime directory.' }
    }
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
    if ($sdk) {
        [IO.Directory]::CreateDirectory((Long-Path $activeDirectory)) | Out-Null
        $agentLock=[IO.File]::Open((Join-Path $activeDirectory '.install.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        try {
            # Keep a runtime already installed in the stable user location.
            # Python validates its pins against the new manifest before import.
            if (-not [IO.Directory]::Exists((Long-Path $active)) -or
                [IO.Directory]::GetFileSystemEntries((Long-Path $active)).Count -eq 0) {
                $stage=Join-Path $activeDirectory ('.migration-'+[guid]::NewGuid().ToString('N'))
                Copy-Tree $prefix $stage
                if ((Inventory $stage) -ne $before) { throw "SDK working copy verification failed: $Source" }
                if ([IO.Directory]::Exists((Long-Path $active))) { [IO.Directory]::Delete((Long-Path $active),$false) }
                [IO.Directory]::Move((Long-Path $stage),(Long-Path $active))
            }
        } finally { $agentLock.Dispose() }
    } elseif (Test-Path -LiteralPath $activeDirectory) {
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
    if (-not $sdk) {
        # npm's local .bin shims look beside themselves for node.exe before
        # falling back to PATH. Keep their original ABI when the app upgrades Node.
        $localBin=Join-Path $active 'node_modules\.bin'
        $localNode=Join-Path $localBin 'node.exe'
        if ([IO.Directory]::Exists((Long-Path $localBin)) -and
            [IO.File]::Exists((Long-Path (Join-Path $active 'node.exe'))) -and
            -not [IO.File]::Exists((Long-Path $localNode))) {
            Check-Parents $localNode
            $nodeStage=Join-Path $localBin ('.node-pending-'+[guid]::NewGuid().ToString('N'))
            $nodeSource=Join-Path $active 'node.exe'
            [IO.File]::Copy((Long-Path $nodeSource),(Long-Path $nodeStage),$false)
            if ((File-Hash $nodeStage) -ne (File-Hash $nodeSource)) { throw 'Local shim Node verification failed.' }
            [IO.File]::Move((Long-Path $nodeStage),(Long-Path $localNode))
        }
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
    if (-not [IO.Path]::IsPathRooted($data)) { throw 'The user data directory must be an absolute path for upgrade preservation.' }
    $dataRoot=Full-Path $data
    $npmRoot=Full-Path (Join-Path $dataRoot 'runtime\npm')
    if (Inside $dataRoot $InstallDir) { throw 'The CLI preservation directory must be outside the installation directory.' }
    Check-Parents $npmRoot
    $safeRoot=$true
    $store=Join-Path $npmRoot 'preserved'
    New-Item -ItemType Directory -Path $store -Force | Out-Null
    $lockPath=Join-Path $npmRoot '.migration.lock'
    $lock=[IO.File]::Open($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    try {
        if ($Mode -eq 'PreserveConfig') {
            Preserve-UpgradeConfig
            Write-Json $ReportPath @{success=$true; mode=$Mode}
            exit 0
        }
        $mappings=@()
        foreach ($relative in @('runtime\node-runtime','resources\backend\runtime\node-runtime')) {
            $mapping=Preserve-Prefix (Join-Path $InstallDir $relative)
            if ($mapping) { $mappings += $mapping }
        }
        $sdkMappings=@()
        foreach ($relative in @('runtime\external-cli','resources\backend\runtime\external-cli')) {
            $sdkSource=Join-Path $InstallDir $relative
            try { $sdkAttributes=[IO.File]::GetAttributes((Long-Path $sdkSource)) }
            catch [IO.FileNotFoundException] { continue }
            catch [IO.DirectoryNotFoundException] { continue }
            if (-not ($sdkAttributes -band [IO.FileAttributes]::Directory)) { throw "Expected an SDK directory: $sdkSource" }
            Check-Parents $sdkSource
            # Setup's /DIR argument names the application too. At this level
            # only an executable inside the app proves that it is in use;
            # individual SDK/Node checks still inspect command-line references.
            Assert-PrefixIdle $InstallDir -ExecutableOnly
            foreach ($agentDirectory in [IO.Directory]::GetDirectories((Long-Path $sdkSource))) {
                Check-Parents (Normal-Path $agentDirectory)
                $name=[IO.Path]::GetFileName($agentDirectory)
                if ($name -notmatch '^[a-z0-9][a-z0-9_-]*$') { throw "Unrecognized SDK directory requires manual preservation: $agentDirectory" }
                $packages=Join-Path (Normal-Path $agentDirectory) 'site-packages'
                $sourceLock=$null
                $sourceLockPath=Join-Path (Normal-Path $agentDirectory) '.install.lock'
                if ([IO.File]::Exists((Long-Path $sourceLockPath))) {
                    $sourceLock=[IO.File]::Open((Long-Path $sourceLockPath),[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::None)
                }
                try {
                    $mapping=Preserve-Prefix $packages $name
                    if ($mapping) { $sdkMappings += $mapping }
                } finally { if ($sourceLock) { $sourceLock.Dispose() } }
            }
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
        # Working/global copies can take time. Recheck every old source once all
        # copies finish so a late install cannot be erased by the next cleanup.
        foreach ($mapping in @($mappings)+@($sdkMappings)) {
            Check-Parents $mapping.source
            if ((Text-Hash (Inventory $mapping.source)) -ne $mapping.digest) { throw "Source changed before cleanup: $($mapping.source)" }
            Assert-PrefixIdle $mapping.source
        }
        if ($sdkMappings.Count) { Assert-PrefixIdle $InstallDir -ExecutableOnly }
        Add-UserPaths $mappings $paths
        Write-Json $ReportPath @{success=$true; mode=$Mode; mappings=$mappings; sdk_mappings=$sdkMappings; npm_root=$npmRoot}
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
