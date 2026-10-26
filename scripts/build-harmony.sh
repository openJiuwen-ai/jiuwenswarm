#!/bin/bash
# build-harmony.sh — JiuwenSwarm 鸿蒙应用打包脚本
#
# 与 Windows build-exe.ps1 对照：
#   Windows: uv sync → npm build → PyInstaller → Inno Setup
#   鸿蒙:  npm build → HNP 更新(嵌入前端) → rawfile 准备 → hvigorw 构建 .hap
#
# 用法:
#   ./scripts/build-harmony.sh
#   ./scripts/build-harmony.sh  # 默认主方案
#   HARMONY_FALLBACK=true ./scripts/build-harmony.sh  # Fallback 方案（rawfile 前端）
#
# 前置条件:
#   - 在鸿蒙 PC (aarch64-linux-ohos) 上运行
#   - hnpcli 可用 (/data/service/hnp/bin/hnpcli)
#   - Node.js / npm 可用
#   - DevEco Studio 或 hvigorw 可用（步骤4可选手动执行）
#   - ~/yangzequ/jiuwenswarm_hnp 目录存在（HNP 工作目录）

set -euo pipefail

# ─── 配置区 ───
JIUWENSWARM_REPO="$(cd "$(dirname "$0")/.." && pwd)"
FRONTEND_DIR="${JIUWENSWARM_REPO}/jiuwenswarm/channels/web/frontend"
HNP_WORK_DIR="$HOME/yangzequ/jiuwenswarm_hnp"
HNP_OUTPUT_DIR="$HOME/yangzequ"
HNPCLI="/data/service/hnp/bin/hnpcli"
HARMONY_PROJECT="${JIUWENSWARM_REPO}/../harmony_facade"
RAWFILE_DIR="${HARMONY_PROJECT}/entry/src/main/resources/rawfile"
HNP_DEST_DIR="${HARMONY_PROJECT}/hnp/arm64-v8a"
HNP_NAME="jiuwen"
HNP_VERSION="1.0"

# 默认端口配置（与 harmony_entry.py 一致）
DEFAULT_AGENTSERVER_PORT=18092
DEFAULT_GATEWAY_PORT=19000
DEFAULT_FRONTEND_PORT=5173

# Fallback 模式检测
FALLBACK_MODE="${HARMONY_FALLBACK:-false}"

echo "============================================"
echo " JiuwenSwarm 鸿蒙应用打包脚本"
echo "============================================"
if [ "$FALLBACK_MODE" = "true" ]; then
    echo " ⚠️ Fallback 模式（rawfile 前端 + WebSocket 远程连接）"
fi
echo ""

# ─── [1/4] 构建 Web 前端 ───
echo "=== [1/4] 构建 Web 前端 ==="

if [ ! -d "$FRONTEND_DIR" ]; then
    echo "错误: 前端目录不存在: $FRONTEND_DIR"
    exit 1
fi

cd "$FRONTEND_DIR"

# 清理旧产物
rm -rf dist

# 安装依赖
npm install

# 构建（Fallback 模式使用 harmony mode：相对路径 + 动态 WS/API URL）
if [ "$FALLBACK_MODE" = "true" ]; then
    echo "⚠️ 使用 Fallback 模式构建前端 (harmony mode: relative paths + dynamic URLs)"
    npm run build -- --mode harmony
else
    npm run build
fi

# 验证构建产物
if [ ! -d "dist" ] || [ -z "$(ls -A dist)" ]; then
    echo "错误: 前端构建失败，dist 目录为空"
    echo "请检查 npm install 和 npm run build 的输出"
    exit 1
fi

echo "✅ 前端构建完成: dist/ 目录已生成"
echo "   产物大小: $(du -sh dist | cut -f1)"
echo ""

# ─── [2/4] 更新 HNP 包（嵌入前端 dist） ───
echo "=== [2/4] 更新 HNP 包（嵌入前端 dist） ==="

if [ ! -d "$HNP_WORK_DIR" ]; then
    echo "错误: HNP 工作目录不存在: $HNP_WORK_DIR"
    echo "请先完成 HNP 基础打包（参考 .pi/specs/hnp-packaging-plan.md）"
    exit 1
fi

# 检查 HNP 工作目录中是否有 jiuwenswarm 包
JWS_PKG_DIR="${HNP_WORK_DIR}/lib/python3.12/site-packages/jiuwenswarm"
if [ ! -d "$JWS_PKG_DIR" ]; then
    echo "错误: HNP 中未找到 jiuwenswarm 包: $JWS_PKG_DIR"
    exit 1
fi

# 拷贝 frontend/dist 到 HNP 的 jiuwenswarm 包内
FRONTEND_DIST_DEST="${JWS_PKG_DIR}/channels/web/frontend/dist"
echo "目标路径: $FRONTEND_DIST_DEST"

# 清理旧的 dist（如果存在）
rm -rf "$FRONTEND_DIST_DEST"

# 拷贝新的 dist
mkdir -p "$FRONTEND_DIST_DEST"
cp -a "${FRONTEND_DIR}/dist/"* "$FRONTEND_DIST_DEST/"

# 验证拷贝
if [ ! -d "$FRONTEND_DIST_DEST" ] || [ -z "$(ls -A "$FRONTEND_DIST_DEST")" ]; then
    echo "错误: 前端产物拷贝到 HNP 失败"
    exit 1
fi

echo "✅ 前端 dist 已嵌入 HNP 包"
echo ""

# 清理缓存（减小体积）
echo "清理 HNP 包内缓存..."
find "$HNP_WORK_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$HNP_WORK_DIR" -name '*.pyc' -delete 2>/dev/null || true
find "$HNP_WORK_DIR" -name '*.pyo' -delete 2>/dev/null || true

# 设置权限
chmod -R 777 "$HNP_WORK_DIR"

echo ""

# 重新打包 HNP
echo "重新打包 HNP..."
if [ ! -f "$HNPCLI" ]; then
    echo "错误: hnpcli 不存在: $HNPCLI"
    echo "请安装 hnpcli 或确认路径"
    exit 1
fi

"$HNPCLI" pack -i "$HNP_WORK_DIR" -o "$HNP_OUTPUT_DIR" -n "$HNP_NAME" -v "$HNP_VERSION"

# 验证 HNP 包
HNP_FILE="${HNP_OUTPUT_DIR}/${HNP_NAME}.hnp"
if [ ! -f "$HNP_FILE" ]; then
    echo "错误: HNP 包生成失败"
    exit 1
fi

echo "✅ HNP 包已生成: $HNP_FILE"
echo "   包大小: $(du -sh "$HNP_FILE" | cut -f1)"
echo ""

# 复制 HNP 包到鸿蒙项目
echo "复制 HNP 包到鸿蒙项目..."
mkdir -p "$HNP_DEST_DIR"
cp "$HNP_FILE" "$HNP_DEST_DIR/"

echo "✅ HNP 包已复制到: $HNP_DEST_DIR/$(basename "$HNP_FILE")"
echo ""

# ─── [3/4] 准备 rawfile ───
echo "=== [3/4] 准备 rawfile ==="

# 复制 harmony_entry.py 到 rawfile
ENTRY_SCRIPT="${JIUWENSWARM_REPO}/scripts/harmony_entry.py"
if [ ! -f "$ENTRY_SCRIPT" ]; then
    echo "错误: harmony_entry.py 不存在: $ENTRY_SCRIPT"
    echo "请先创建该脚本"
    exit 1
fi

mkdir -p "$RAWFILE_DIR"
cp "$ENTRY_SCRIPT" "$RAWFILE_DIR/"

echo "✅ harmony_entry.py 已复制到 rawfile"

# Fallback 模式：将前端 dist 也复制到 rawfile
if [ "$FALLBACK_MODE" = "true" ]; then
    echo "复制前端 dist 到 rawfile (Fallback 模式)..."
    cp -a "${FRONTEND_DIR}/dist/"* "$RAWFILE_DIR/"
    echo "✅ 前端 dist 已复制到 rawfile"
fi

echo ""

# ─── [4/4] 构建鸿蒙 .hap ───
echo "=== [4/4] 构建鸿蒙 .hap ==="
echo ""
echo "⚠️ 此步骤需要在 DevEco Studio 中手动执行，或通过 hvigorw 命令行执行。"
echo ""
echo "请在 DevEco Studio 中打开项目:"
echo "  项目路径: $HARMONY_PROJECT"
echo ""
echo "然后执行:"
echo "  1. File → Project Structure → Signing Configs → 配置签名证书"
echo "  2. Build → Build Hap(s)/APP(s)"
echo ""
echo "或者通过命令行:"
echo "  cd $HARMONY_PROJECT"
echo "  ./hvigorw assembleHap --mode module -p module=entry@default"
echo ""
echo "构建产物位置:"
echo "  $HARMONY_PROJECT/entry/build/default/outputs/default/entry-default-signed.hap"
echo ""
echo "============================================"
echo " 打包脚本执行完成！"
echo "============================================"
echo ""
echo "下一步:"
echo "  1. 在 DevEco Studio 中打开项目并构建"
echo "  2. 连接鸿蒙 PC 设备"
echo "  3. 点击 Run 安装并启动应用"
echo ""
echo "端口配置: AgentServer=${DEFAULT_AGENTSERVER_PORT} / Gateway=${DEFAULT_GATEWAY_PORT} / Frontend=${DEFAULT_FRONTEND_PORT}"
echo ""
echo "使用指南: .pi/specs/harmony-app-usage-guide.md"
