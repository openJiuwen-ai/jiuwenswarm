# JiuwenClaw 打包脚本
# 1. 编译前端 (jiuwenclaw/web)
# 2. 构建 wheel 包（包含前端 dist）

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent

Write-Host "[build] 项目根目录: $ProjectRoot" -ForegroundColor Cyan

# 1. 编译前端
$WebDir = Join-Path (Join-Path $ProjectRoot "jiuwenclaw") "web"
if (-not (Test-Path $WebDir)) {
    Write-Error "前端目录不存在: $WebDir"
}

Write-Host "[build] 正在编译前端..." -ForegroundColor Yellow
Push-Location $WebDir
try {
    if (-not (Test-Path "node_modules")) {
        Write-Host "[build] 安装 npm 依赖..." -ForegroundColor Gray
        npm install
    }
    npm run build
    if ($LASTEXITCODE -ne 0) {
        throw "前端编译失败"
    }
} finally {
    Pop-Location
}

$DistDir = Join-Path $WebDir "dist"
if (-not (Test-Path $DistDir)) {
    Write-Error "前端编译输出不存在: $DistDir"
}
Write-Host "[build] 前端编译完成: $DistDir" -ForegroundColor Green

# 临时移走 node_modules，避免被打包进 wheel
$NodeModules = Join-Path $WebDir "node_modules"
$NodeModulesBak = Join-Path $WebDir "node_modules.bak"
$NodeModulesMoved = $false
if (Test-Path $NodeModules) {
    Write-Host "[build] 临时移走 node_modules 以减小 wheel 体积..." -ForegroundColor Gray
    Move-Item $NodeModules $NodeModulesBak -Force
    $NodeModulesMoved = $true
}

try {
# 2. 构建 wheel
Write-Host "[build] 正在构建 wheel 包..." -ForegroundColor Yellow
Push-Location $ProjectRoot
try {
    python -m pip install --upgrade build wheel 2>$null
    python -m build --wheel --no-isolation
    if ($LASTEXITCODE -ne 0) {
        throw "wheel 构建失败"
    }
} finally {
    Pop-Location
}

# 确保 dist 目录存在
$DistOutput = Join-Path $ProjectRoot "dist"
if (-not (Test-Path $DistOutput)) {
    New-Item -ItemType Directory -Path $DistOutput -Force | Out-Null
    Write-Host "[build] 创建 dist 目录: $DistOutput" -ForegroundColor Gray
}
Write-Host "[build] 完成! wheel 包位于: $DistOutput" -ForegroundColor Green
Get-ChildItem $DistOutput -Filter "*.whl" | ForEach-Object { Write-Host "  - $($_.Name)" }
} finally {
    # 恢复 node_modules
    if ($NodeModulesMoved -and (Test-Path $NodeModulesBak)) {
        Move-Item $NodeModulesBak $NodeModules -Force
        Write-Host "[build] 已恢复 node_modules" -ForegroundColor Gray
    }
}
