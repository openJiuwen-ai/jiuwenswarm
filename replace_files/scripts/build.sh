#!/usr/bin/env bash
# JiuwenClaw 打包脚本
# 1. 编译前端 (jiuwenclaw/web)
# 2. 构建 wheel 包（包含前端 dist）

set -e
PROJECT_ROOT="$(cd "$(dirname "$(dirname "$0")")" && pwd)"

echo "[build] 项目根目录: $PROJECT_ROOT"

# 1. 编译前端
WEB_DIR="$PROJECT_ROOT/jiuwenclaw/web"
if [[ ! -d "$WEB_DIR" ]]; then
    echo "[build] 错误: 前端目录不存在: $WEB_DIR" >&2
    exit 1
fi

echo "[build] 正在编译前端..."
cd "$WEB_DIR"
if [[ ! -d node_modules ]]; then
    echo "[build] 安装 npm 依赖..."
    npm install
fi
npm run build
cd "$PROJECT_ROOT"

DIST_DIR="$WEB_DIR/dist"
if [[ ! -d "$DIST_DIR" ]]; then
    echo "[build] 错误: 前端编译输出不存在: $DIST_DIR" >&2
    exit 1
fi
echo "[build] 前端编译完成: $DIST_DIR"

# 临时移走 node_modules，避免被打包进 wheel
NODE_MODULES="$WEB_DIR/node_modules"
NODE_MODULES_BAK="$WEB_DIR/node_modules.bak"
NODE_MODULES_MOVED=false
if [[ -d "$NODE_MODULES" ]]; then
    echo "[build] 临时移走 node_modules 以减小 wheel 体积..."
    mv "$NODE_MODULES" "$NODE_MODULES_BAK"
    NODE_MODULES_MOVED=true
fi

cleanup() {
    # 恢复 node_modules
    if [[ "$NODE_MODULES_MOVED" == "true" && -d "$NODE_MODULES_BAK" ]]; then
        mv "$NODE_MODULES_BAK" "$NODE_MODULES"
        echo "[build] 已恢复 node_modules"
    fi
}
trap cleanup EXIT

# 2. 构建 wheel
echo "[build] 正在构建 wheel 包..."
pip install -q --upgrade build wheel
python -m build --wheel --no-isolation

# 确保 dist 目录存在
DIST_OUTPUT="$PROJECT_ROOT/dist"
if [[ ! -d "$DIST_OUTPUT" ]]; then
    mkdir -p "$DIST_OUTPUT"
    echo "[build] 创建 dist 目录: $DIST_OUTPUT"
fi
echo "[build] 完成! wheel 包位于: $DIST_OUTPUT"
ls -la dist/*.whl 2>/dev/null || true

# 3. 构建 TUI wheel
if ! command -v bun &>/dev/null; then
    echo "[build] 跳过 TUI 构建: 未找到 bun 命令" >&2
    echo "完成bun安装: curl -fsSL https://bun.sh/install | bash  # 针对 macOS、Linux 和 WSL" >&2
else
    echo "[build] 正在构建TUI的 wheel包..."
    cd "$PROJECT_ROOT"
    python scripts/build_python_packages.py --target all --clean --install-js-deps
    echo "[build] TUI的 wheel 包构建完成"
fi
