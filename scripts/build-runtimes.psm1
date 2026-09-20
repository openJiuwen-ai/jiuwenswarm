# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# node/uv 运行时的解析与绑定函数，build-exe.ps1 与 build-electron-exe.ps1 的
# 单一来源。两条打包链路禁止在各自脚本内复制这些实现，否则会再次漂移
# （此前 Electron 打包漏绑 Node，导致依赖 node 的 Agent 技能如 ppt-creation
# 报"环境没有 node"）。契约测试 tests/unit_tests/test_desktop_electron_contract.py
# 钉住：共享模块 → 打包脚本调用 → 落地路径 → 冻结入口查找 → 安装器递归打包。

function Test-Truthy {
    param([string]$Value)

    $normalized = $Value.Trim().ToLowerInvariant()
    return $normalized -in @("1", "true", "yes", "on")
}

# BUNDLE_NODE / BUNDLE_UV / NODE_VERSION 的默认值只允许在这里出现一次，
# 两个打包脚本通过 Get-RuntimeBundleSettings 读取，保证取值不会漂移。
function Get-RuntimeBundleSettings {
    $bundleNode = if ($env:BUNDLE_NODE) { $env:BUNDLE_NODE } else { "1" }
    $bundleUv = if ($env:BUNDLE_UV) { $env:BUNDLE_UV } else { "1" }
    $nodeVersion = if ($env:NODE_VERSION) { $env:NODE_VERSION } else { "v22.11.0" }
    return @{
        BundleNode  = $bundleNode
        BundleUv    = $bundleUv
        NodeVersion = $nodeVersion
    }
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

function Resolve-UvRuntimeDir {
    param([string]$ProjectRoot)

    # uv 是项目 pip 依赖（pyproject dependencies）.
    $venvUv = Join-Path $ProjectRoot ".venv\Scripts\uv.exe"
    if (Test-Path -LiteralPath $venvUv) {
        return (Resolve-Path -LiteralPath $venvUv).Path
    }
    throw 'uv not found in .venv\Scripts; run uv sync first'
}

function Copy-UvRuntime {
    param(
        [string]$UvExePath,
        [string]$DistDir
    )

    if (-not $UvExePath) {
        return
    }

    $distResolved = (Resolve-Path -LiteralPath $DistDir -ErrorAction Stop).Path
    $runtimeDir = Join-Path $distResolved "runtime"
    $target = Join-Path $runtimeDir "uv-runtime"
    if (Test-Path -LiteralPath $target) {
        $targetResolved = (Resolve-Path -LiteralPath $target).Path
        if (-not $targetResolved.StartsWith($distResolved, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove uv runtime outside dist: $targetResolved"
        }
        Remove-Item -LiteralPath $targetResolved -Recurse -Force
    }
    New-Item -ItemType Directory -Path $target -Force | Out-Null

    # uvx.exe 是 shim，运行时 exec 同目录的 uv.exe，两者必须一起 bundle。
    $sourceDir = Split-Path -Parent $UvExePath
    foreach ($file in @("uv.exe", "uvx.exe")) {
        $source = Join-Path $sourceDir $file
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination $target -Force
        }
    }

    if (-not (Test-Path -LiteralPath (Join-Path $target "uvx.exe"))) {
        throw "Bundled uv runtime is missing uvx.exe: $target"
    }

    $uvVersion = & (Join-Path $target "uv.exe") --version
    Write-Host "[runtime] Bundled $uvVersion into $target" -ForegroundColor Green
}

Export-ModuleMember -Function @(
    "Test-Truthy",
    "Get-RuntimeBundleSettings",
    "Get-NodeArch",
    "Download-NodeRuntime",
    "Resolve-NodeRuntimeDir",
    "Use-NodeRuntime",
    "Copy-NodeRuntime",
    "Resolve-UvRuntimeDir",
    "Copy-UvRuntime"
)
