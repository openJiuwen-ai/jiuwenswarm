#!/bin/bash
# JiuwenSwarm Electron macOS Build Script
# Usage:
#   Release:    ./scripts/build-electron-exe.sh
#   Test:       ./scripts/build-electron-exe.sh --test
#   Frontend:   ./scripts/build-electron-exe.sh --test --frontend-only
#
# Build flow:
# 1. Install Python dependencies (uv sync --extra dev --extra claude --extra codex) [skip: --frontend-only]
# 2. Build frontend (vite build with ELECTRON=true)
# 3. Install Electron desktop dependencies (npm install)
# 4. PyInstaller backend (jiuwenswarm.spec)                   [skip: --frontend-only]
# 5. Assemble: Electron.app + frontend dist + backend + resources
# 6. Package .dmg

set -e

# ── Parse args ────────────────────────────────────────────────────────────────

TEST=false
FRONTEND_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --test) TEST=true ;;
        --frontend-only) FRONTEND_ONLY=true ;;
    esac
done

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/jiuwenswarm/channels/web/frontend"
DESKTOP_DIR="$PROJECT_ROOT/jiuwenswarm/channels/desktop/electron"
DIST_DIR="$PROJECT_ROOT/dist"
ELECTRON_APP_DIR="$DIST_DIR/JiuwenSwarm-Electron"

cd "$PROJECT_ROOT"

echo "=== JiuwenSwarm Electron Build (macOS) ==="
echo "Project root: $PROJECT_ROOT"
echo "Test: $TEST  FrontendOnly: $FRONTEND_ONLY"
echo ""

# ── Resolve Electron binary ───────────────────────────────────────────────────
# macOS: npm install electron gives node_modules/electron/dist/Electron.app
ELECTRON_APP_SOURCE="${ELECTRON_DIR:-}"
if [ -z "$ELECTRON_APP_SOURCE" ]; then
    CANDIDATES=(
        "$DESKTOP_DIR/node_modules/electron/dist/Electron.app"
        "$PROJECT_ROOT/node_modules/electron/dist/Electron.app"
        "$HOME/node_modules/electron/dist/Electron.app"
    )
    for c in "${CANDIDATES[@]}"; do
        if [ -d "$c" ]; then
            ELECTRON_APP_SOURCE="$c"
            break
        fi
    done
fi

if [ -z "$ELECTRON_APP_SOURCE" ] || [ ! -d "$ELECTRON_APP_SOURCE" ]; then
    echo "ERROR: Electron.app not found."
    echo "Run 'npm install' in jiuwenswarm/channels/desktop/electron, or"
    echo "set ELECTRON_DIR env var."
    exit 1
fi

ELECTRON_BIN="$ELECTRON_APP_SOURCE/Contents/MacOS/Electron"
if [ ! -f "$ELECTRON_BIN" ]; then
    echo "ERROR: Electron binary not found at: $ELECTRON_BIN"
    exit 1
fi

echo "Electron: $ELECTRON_APP_SOURCE"
echo ""

# ── 1. Install Python dependencies ───────────────────────────────────────────
if [ "$FRONTEND_ONLY" = false ]; then
    echo "[1/6] Installing Python dependencies (uv sync --extra dev --extra claude --extra codex)..."
    uv sync --extra dev --extra claude --extra codex
else
    echo "[1/6] Skipping Python dependencies (FrontendOnly)"
fi

# ── 2. Build frontend ────────────────────────────────────────────────────────
echo ""
echo "[2/6] Building frontend..."
cd "$FRONTEND_DIR"
export ELECTRON=true
if [ ! -d "node_modules" ]; then
    echo "[build] node_modules missing, running npm install..."
    npm install
fi
npm run build
cd "$PROJECT_ROOT"

# ── 3. Install Electron desktop dependencies ─────────────────────────────────
echo ""
echo "[3/6] Installing Electron desktop dependencies..."
cd "$DESKTOP_DIR"
if [ ! -d "node_modules" ]; then
    echo "[desktop] node_modules missing, running npm install..."
    npm install
fi
cd "$PROJECT_ROOT"

# ── 4. PyInstaller backend ───────────────────────────────────────────────────
# COLLECT 输出目录、exe 名、版本号与捆绑标识均来自 build_config（改名后会变化），
# 不能写死，否则可能命中 dist 下改名前的陈旧目录（如 dist/jiuwenswarm），把旧后端
# 打进包里；--sync 先同步 _build_config.py（PyInstaller spec 对漂移会硬失败）。
# FrontendOnly 也读取版本号（组装 package.json / DMG 命名需要）。与 ps1 同源。
BUILD_VALUES="$(uv run --no-project --python 3.11 python "$PROJECT_ROOT/scripts/build_config.py" --sync --emit-shell)"
BUILD_DIST_DIR_NAME="$(printf '%s\n' "$BUILD_VALUES" | sed -n 's/^BUILD_DIST_DIR_NAME=//p')"
BUILD_EXECUTABLE_NAME="$(printf '%s\n' "$BUILD_VALUES" | sed -n 's/^BUILD_EXECUTABLE_NAME=//p')"
BUILD_VERSION="$(printf '%s\n' "$BUILD_VALUES" | sed -n 's/^BUILD_VERSION=//p')"
BUILD_BUNDLE_IDENTIFIER="$(printf '%s\n' "$BUILD_VALUES" | sed -n 's/^BUILD_BUNDLE_IDENTIFIER=//p')"
if [ -z "$BUILD_VERSION" ] || [ -z "$BUILD_BUNDLE_IDENTIFIER" ]; then
    echo "ERROR: failed to resolve build config from build_config.py"
    exit 1
fi

if [ "$FRONTEND_ONLY" = false ]; then
    echo ""
    echo "[4/6] Running PyInstaller (backend)..."
    uv run pyinstaller scripts/jiuwenswarm.spec --noconfirm

    BACKEND_DIST="$DIST_DIR/$BUILD_DIST_DIR_NAME"
    if [ ! -f "$BACKEND_DIST/$BUILD_EXECUTABLE_NAME" ]; then
        echo "ERROR: PyInstaller output not found: $BACKEND_DIST/$BUILD_EXECUTABLE_NAME"
        exit 1
    fi

    # Verify the actual frozen runtime, not only the source configuration
    # (same self-check as the Python packaging build in build-macos.sh).
    "$BACKEND_DIST/$BUILD_EXECUTABLE_NAME" "$PROJECT_ROOT/scripts/verify_a2ui_bundle.py"
else
    echo ""
    echo "[4/6] Skipping PyInstaller (FrontendOnly)"
fi

# ── 5. Assemble Electron app ─────────────────────────────────────────────────
echo ""
echo "[5/6] Assembling Electron desktop app..."

rm -rf "$ELECTRON_APP_DIR"
mkdir -p "$ELECTRON_APP_DIR"

# Copy Electron.app
echo "  Copying Electron.app..."
cp -R "$ELECTRON_APP_SOURCE" "$ELECTRON_APP_DIR/Electron.app"

# Prepare resources/app directory
APP_DIR="$ELECTRON_APP_DIR/Electron.app/Contents/Resources/app"
mkdir -p "$APP_DIR"

# Copy the mature Electron desktop shell (main.cjs, preload.cjs, target_mcp_wrapper.cjs)
echo "  Copying Electron desktop shell..."
cp "$DESKTOP_DIR/main.cjs" "$APP_DIR/"
cp "$DESKTOP_DIR/preload.cjs" "$APP_DIR/"
cp "$DESKTOP_DIR/target_mcp_wrapper.cjs" "$APP_DIR/"

# Copy logo.icns for window icon
cp "$FRONTEND_DIR/public/logo.icns" "$APP_DIR/logo.icns"

# Copy frontend dist
cp -R "$FRONTEND_DIR/dist" "$APP_DIR/dist"

# Copy PyInstaller backend output (skip if FrontendOnly)
if [ "$FRONTEND_ONLY" = false ]; then
    echo "  Copying PyInstaller backend..."
    BACKEND_DIR="$ELECTRON_APP_DIR/Electron.app/Contents/Resources/backend"
    mkdir -p "$BACKEND_DIR"
    cp -R "$BACKEND_DIST/"* "$BACKEND_DIR/"
fi

# Create package.json for the Electron app (main.cjs is CommonJS)
# 版本号来自 build_config（与 pyproject 单一来源，同 ps1）。
cat > "$APP_DIR/package.json" << PKGJSON
{
    "name": "jiuwenswarm-electron",
    "version": "$BUILD_VERSION",
    "main": "main.cjs"
}
PKGJSON

# Test build: write .test marker file
if [ "$TEST" = true ]; then
    echo "test" > "$APP_DIR/.test"
    echo "  Test build: .test marker written"
fi

# FrontendOnly build: write .frontend-only marker
if [ "$FRONTEND_ONLY" = true ]; then
    echo "frontend-only" > "$APP_DIR/.frontend-only"
    echo "  FrontendOnly build: no backend, loads local dist"
fi

# Set app icon (replace Electron's default Info.plist icon)
ICON_PATH="$FRONTEND_DIR/public/logo.icns"
ICON_DST="$ELECTRON_APP_DIR/Electron.app/Contents/Resources/electron.icns"
if [ -f "$ICON_PATH" ]; then
    cp "$ICON_PATH" "$ICON_DST"
    echo "  Set app icon to logo.icns"
fi

# Rename Electron.app to JiuwenSwarm.app
mv "$ELECTRON_APP_DIR/Electron.app" "$ELECTRON_APP_DIR/JiuwenSwarm.app"

# Update Info.plist
PLIST="$ELECTRON_APP_DIR/JiuwenSwarm.app/Contents/Info.plist"
if [ -f "$PLIST" ]; then
    /usr/libexec/PlistBuddy -c "Set :CFBundleName JiuwenSwarm" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName JiuwenSwarm" "$PLIST" 2>/dev/null || true
    # 捆绑标识与版本号来自 build_config：默认的 Electron 标识会与其他
    # 未改名 Electron 应用冲突（同标识无法共存于 /Applications）。
    /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $BUILD_BUNDLE_IDENTIFIER" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $BUILD_VERSION" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_VERSION" "$PLIST" 2>/dev/null || true
    echo "  Updated Info.plist"
fi

# Calculate size
APP_SIZE=$(du -sh "$ELECTRON_APP_DIR" | awk '{print $1}')
echo "  Assembled app size: $APP_SIZE"

# ── 6. Package .dmg ──────────────────────────────────────────────────────────
echo ""
echo "[6/6] Building .dmg..."

APP_BUNDLE="$ELECTRON_APP_DIR/JiuwenSwarm.app"
DMG_DIR="$DIST_DIR"
VERSION="$BUILD_VERSION"

if [ "$FRONTEND_ONLY" = true ]; then
    DMG_NAME="JiuwenSwarm-frontend-test-$VERSION.dmg"
elif [ "$TEST" = true ]; then
    DMG_NAME="JiuwenSwarm-test-$VERSION.dmg"
else
    DMG_NAME="JiuwenSwarm-setup-$VERSION.dmg"
fi

DMG_PATH="$DMG_DIR/$DMG_NAME"

# Create a temporary DMG directory
DMG_STAGING="$DIST_DIR/.dmg-staging"
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"

# Copy app bundle
cp -R "$APP_BUNDLE" "$DMG_STAGING/"

# Create Applications symlink
ln -s /Applications "$DMG_STAGING/Applications"

# Create DMG
rm -f "$DMG_PATH"
hdiutil create -volname "JiuwenSwarm" -srcfolder "$DMG_STAGING" -ov -format UDZO "$DMG_PATH"
rm -rf "$DMG_STAGING"

DMG_SIZE=$(du -h "$DMG_PATH" | awk '{print $1}')

echo ""
echo "=== Build complete ==="
echo "DMG: $DMG_PATH"
echo "Size: $DMG_SIZE"
