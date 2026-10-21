#!/bin/bash
# WorkSwarm Electron macOS Build Script
# Usage:
#   Local:      NOSIGN=1 bash scripts/build-electron-exe.sh
#   Release:    NOTARIZE=1 bash scripts/build-electron-exe.sh
#   Test:       bash scripts/build-electron-exe.sh --test
#   Frontend:   bash scripts/build-electron-exe.sh --test --frontend-only
#
# Build flow:
# 1. Install Python dependencies (uv sync --extra dev) [skip: --frontend-only]
# 2. Build frontend (vite build with ELECTRON=true)
# 3. Install Electron desktop dependencies and resolve Electron.app
# 4. PyInstaller backend (jiuwenswarm.spec)                   [skip: --frontend-only]
# 5. Assemble: Electron.app + frontend dist + backend + MCP/runtime resources
# 6. Sign/notarize Electron.app, package/sign/notarize .dmg

set -euo pipefail

# ── Parse args ────────────────────────────────────────────────────────────────

TEST=false
FRONTEND_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --test) TEST=true ;;
        --frontend-only) FRONTEND_ONLY=true ;;
        *) echo "ERROR: unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/jiuwenswarm/channels/web/frontend"
DESKTOP_DIR="$PROJECT_ROOT/jiuwenswarm/channels/desktop/electron"
DIST_DIR="$PROJECT_ROOT/dist"

# Synchronize and resolve the canonical build metadata before any dependency
# synchronization or npm/Bun installation, matching scripts/build-macos.sh.
# FrontendOnly also needs these values for package.json and DMG names.
BUILD_VALUES="$(uv run --no-project --python 3.11 python "$PROJECT_ROOT/scripts/build_config.py" --sync --emit-shell)"
BUILD_DIST_DIR_NAME="$(printf '%s\n' "$BUILD_VALUES" | sed -n 's/^BUILD_DIST_DIR_NAME=//p')"
BUILD_EXECUTABLE_NAME="$(printf '%s\n' "$BUILD_VALUES" | sed -n 's/^BUILD_EXECUTABLE_NAME=//p')"
BUILD_VERSION="$(printf '%s\n' "$BUILD_VALUES" | sed -n 's/^BUILD_VERSION=//p')"
BUILD_BUNDLE_IDENTIFIER="$(printf '%s\n' "$BUILD_VALUES" | sed -n 's/^BUILD_BUNDLE_IDENTIFIER=//p')"
BUILD_DISPLAY_NAME="$(printf '%s\n' "$BUILD_VALUES" | sed -n 's/^BUILD_DISPLAY_NAME=//p')"
BUILD_APP_BUNDLE_NAME="$(printf '%s\n' "$BUILD_VALUES" | sed -n 's/^BUILD_APP_BUNDLE_NAME=//p')"
BUILD_DMG_FILENAME="$(printf '%s\n' "$BUILD_VALUES" | sed -n 's/^BUILD_DMG_FILENAME=//p')"
if [ -z "$BUILD_VERSION" ] || [ -z "$BUILD_BUNDLE_IDENTIFIER" ] \
    || [ -z "$BUILD_DISPLAY_NAME" ] || [ -z "$BUILD_APP_BUNDLE_NAME" ] \
    || [ -z "$BUILD_DMG_FILENAME" ]; then
    echo "ERROR: failed to resolve build config from build_config.py"
    exit 1
fi
ELECTRON_APP_DIR="$DIST_DIR/$BUILD_DISPLAY_NAME-Electron"

# 签名与公证参数与 build-macos.sh 保持一致，但两条打包链路仍彼此独立。
ENTITLEMENTS="$PROJECT_ROOT/scripts/entitlements.mac.plist"
NOTARY_PROFILE="${NOTARY_PROFILE:-jiuwenswarm-notary}"
DO_NOTARIZE="${NOTARIZE:-0}"
DO_SIGN=1

if [ "${NOSIGN:-0}" = "1" ]; then
    DO_SIGN=0
    SIGN_IDENTITY=""
elif [ -z "${SIGN_IDENTITY:-}" ]; then
    SIGN_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
        | grep -oE 'Developer ID Application: [^(]+\([0-9A-Z]+\)' \
        | head -1 || true)"
    if [ -z "$SIGN_IDENTITY" ]; then
        DO_SIGN=0
    fi
fi

if [ "$DO_SIGN" = "0" ]; then
    if [ "$DO_NOTARIZE" = "1" ]; then
        echo "ERROR: NOTARIZE=1 requires a Developer ID Application identity." >&2
        exit 1
    fi
elif [ "$SIGN_IDENTITY" = "-" ]; then
    echo "WARNING: ad-hoc signing is for local testing only."
    if [ "$DO_NOTARIZE" = "1" ]; then
        echo "ERROR: NOTARIZE=1 cannot be used with ad-hoc signing." >&2
        exit 1
    fi
elif ! security find-identity -v -p codesigning | grep -q "$SIGN_IDENTITY"; then
    echo "ERROR: signing identity not found in the keychain: $SIGN_IDENTITY" >&2
    exit 1
fi

# Node 运行时解析/绑定实现在 scripts/build-runtimes.sh，与 build-macos.sh 单一来源；
# 契约测试 tests/unit_tests/test_desktop_electron_contract.py 钉住两侧同步。
# macOS 冻结后端入口在 <.app>/Contents/Resources/node-runtime/bin 查找并前置
# PATH（jiuwenswarm_exe_entry.py），Agent 技能（ppt-creation 等 node 脚本）
# 因此不依赖用户机器的 Node.js。
NODE_MINIMUM_VERSION=22.12.0
NODE_DEFAULT_VERSION=v22.12.0
source "$PROJECT_ROOT/scripts/build-runtimes.sh"
NODE_SRC="$(resolve_node_dir)"
export PATH="$NODE_SRC/bin:$PATH"
BUILD_ARCH="$(uname -m)"
case "$BUILD_ARCH" in
    arm64|x86_64) ;;
    *) echo "ERROR: unsupported macOS architecture: $BUILD_ARCH" >&2; exit 1 ;;
esac

check_arch() {
    local binary="$1"
    if ! lipo "$binary" -verify_arch "$BUILD_ARCH"; then
        echo "ERROR: $binary does not contain $BUILD_ARCH" >&2
        exit 1
    fi
}
check_arch "$NODE_SRC/bin/node"

cd "$PROJECT_ROOT"

echo "=== WorkSwarm Electron Build (macOS) ==="
echo "Project root: $PROJECT_ROOT"
echo "Test: $TEST  FrontendOnly: $FRONTEND_ONLY"
if [ "$DO_SIGN" = "1" ]; then
    echo "Sign identity: $SIGN_IDENTITY"
else
    echo "Sign identity: (skipped; local artifact only)"
fi
if [ "$DO_NOTARIZE" = "1" ]; then
    echo "Notarization: ENABLED (profile=$NOTARY_PROFILE)"
else
    echo "Notarization: DISABLED"
fi
echo ""

# ── Resolve Electron binary ───────────────────────────────────────────────────

# ── 1. Install Python dependencies ───────────────────────────────────────────
if [ "$FRONTEND_ONLY" = false ]; then
    echo "[1/6] Installing locked Python dependencies (uv sync --locked --extra dev)..."
    uv sync --locked --extra dev
else
    echo "[1/6] Skipping Python dependencies (FrontendOnly)"
fi

# ── 2. Build frontend ────────────────────────────────────────────────────────
echo ""
echo "[2/6] Building frontend..."
cd "$FRONTEND_DIR"
if [ ! -d "node_modules" ]; then
    npm ci --prefer-offline --no-audit --no-fund
fi
ELECTRON=true npm run build
cd "$PROJECT_ROOT"

# ── 3. Install Electron desktop dependencies ─────────────────────────────────
echo ""
echo "[3/6] Installing Electron desktop dependencies..."
cd "$DESKTOP_DIR"
if [ -z "${ELECTRON_DIR:-}" ] \
    && [ ! -x "node_modules/electron/dist/Electron.app/Contents/MacOS/Electron" ]; then
    echo "[desktop] Electron runtime missing, running npm ci..."
    npm ci --prefer-offline --no-audit --no-fund
    if [ ! -x "node_modules/electron/dist/Electron.app/Contents/MacOS/Electron" ]; then
        node node_modules/electron/install.js
    fi
fi
cd "$PROJECT_ROOT"

# macOS: npm install electron gives node_modules/electron/dist/Electron.app.
# Resolve only after the dependency step so a clean checkout is buildable.
ELECTRON_APP_SOURCE="${ELECTRON_DIR:-}"
if [ -z "$ELECTRON_APP_SOURCE" ]; then
    CANDIDATES=(
        "$DESKTOP_DIR/node_modules/electron/dist/Electron.app"
        "$PROJECT_ROOT/node_modules/electron/dist/Electron.app"
    )
    for candidate in "${CANDIDATES[@]}"; do
        if [ -d "$candidate" ]; then
            ELECTRON_APP_SOURCE="$candidate"
            break
        fi
    done
fi

if [ -z "$ELECTRON_APP_SOURCE" ] || [ ! -d "$ELECTRON_APP_SOURCE" ]; then
    echo "ERROR: Electron.app not found after installing desktop dependencies." >&2
    echo "Set ELECTRON_DIR to an Electron.app path to override the local runtime." >&2
    exit 1
fi

ELECTRON_BIN="$ELECTRON_APP_SOURCE/Contents/MacOS/Electron"
if [ ! -x "$ELECTRON_BIN" ]; then
    echo "ERROR: Electron binary missing or not executable: $ELECTRON_BIN" >&2
    exit 1
fi
echo "Electron: $ELECTRON_APP_SOURCE"
check_arch "$ELECTRON_BIN"

# ── 3.5 Build TUI native binary (Bun; same as build-macos.sh) ────────────────
# macOS Python 包把 jiuwenswarm-tui 放在后端 exe 旁（desktop_app 按
# sys.executable 同目录查找并写入 ~/.zshrc PATH），Electron 包必须同样提供。
TUI_BINARY=""
if [ "$FRONTEND_ONLY" = false ]; then
    echo ""
    echo "[3.5/6] Building TUI native binary (Bun)..."
    if command -v bun &>/dev/null; then
        pushd "$PROJECT_ROOT/jiuwenswarm/channels/tui/frontend" >/dev/null
        bun install
        popd >/dev/null
        uv run python scripts/build_tui.py --target current
        # build_tui.py logs progress, not a machine-readable stdout path.
        TUI_ARCH="$BUILD_ARCH"
        if [ "$TUI_ARCH" = "x86_64" ]; then TUI_ARCH=x64; fi
        TUI_BINARY="$PROJECT_ROOT/packages/jiuwenswarm-tui/jiuwenswarm_tui/resources/tui-bin/macos-$TUI_ARCH/jiuwenswarm-tui"
        if [ ! -x "$TUI_BINARY" ]; then
            echo "ERROR: TUI build did not produce an executable: $TUI_BINARY" >&2
            exit 1
        fi
    else
        echo "ERROR: bun is required to include the TUI in a full Electron DMG." >&2
        exit 1
    fi
fi

# ── 4. PyInstaller backend ───────────────────────────────────────────────────
if [ "$FRONTEND_ONLY" = false ]; then
    echo ""
    echo "[4/6] Running PyInstaller (backend)..."
    uv run pyinstaller scripts/jiuwenswarm.spec --noconfirm

    BACKEND_DIST="$DIST_DIR/$BUILD_DIST_DIR_NAME"
    if [ ! -f "$BACKEND_DIST/$BUILD_EXECUTABLE_NAME" ]; then
        echo "ERROR: PyInstaller output not found: $BACKEND_DIST/$BUILD_EXECUTABLE_NAME"
        exit 1
    fi
    check_arch "$BACKEND_DIST/$BUILD_EXECUTABLE_NAME"

    # Verify the actual frozen runtime, not only the source configuration
    # (same self-check as the Python packaging build in build-macos.sh).
    "$BACKEND_DIST/$BUILD_EXECUTABLE_NAME" "$PROJECT_ROOT/scripts/verify_a2ui_bundle.py"

    # Same RSI Harness baseline self-check as build-macos.sh.
    "$BACKEND_DIST/$BUILD_EXECUTABLE_NAME" "$PROJECT_ROOT/scripts/verify_rsi_bundle.py"

    # Exercise lazy adapter exports before shipping a backend that only binds
    # its Front port but cannot initialize the actual runtime.
    "$BACKEND_DIST/$BUILD_EXECUTABLE_NAME" "$PROJECT_ROOT/scripts/verify_gateway_adapter_bundle.py"

    # Same GitCode CLI self-check as build-macos.sh.
    "$BACKEND_DIST/$BUILD_EXECUTABLE_NAME" "$PROJECT_ROOT/scripts/verify_gitcode_cli_bundle.py"
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
cp "$DESKTOP_DIR/browser_panels.cjs" "$APP_DIR/"
cp "$DESKTOP_DIR/share_image_export.cjs" "$APP_DIR/"
cp "$DESKTOP_DIR/display_media_picker.cjs" "$APP_DIR/"
cp "$DESKTOP_DIR/preload.cjs" "$APP_DIR/"
cp "$DESKTOP_DIR/target_mcp_wrapper.cjs" "$APP_DIR/"
cp "$DESKTOP_DIR/macos_update.cjs" "$APP_DIR/"
cp "$DESKTOP_DIR/install_macos_update.sh" "$APP_DIR/"

# Copy logo.icns for window icon
cp "$FRONTEND_DIR/public/logo.icns" "$APP_DIR/logo.icns"

# Copy the loading-page logo used by main.cjs in packaged mode.
if [ -f "$FRONTEND_DIR/public/logo.svg" ]; then
    cp "$FRONTEND_DIR/public/logo.svg" "$APP_DIR/logo.svg"
fi

# Copy frontend dist
cp -R "$FRONTEND_DIR/dist" "$APP_DIR/dist"

# Copy PyInstaller backend output (skip if FrontendOnly)
if [ "$FRONTEND_ONLY" = false ]; then
    echo "  Copying PyInstaller backend..."
    BACKEND_DIR="$ELECTRON_APP_DIR/Electron.app/Contents/Resources/backend"
    mkdir -p "$BACKEND_DIR"
    cp -R "$BACKEND_DIST/"* "$BACKEND_DIR/"

    # TUI binary sits next to the backend executable (same contract as
    # build-macos.sh; desktop_app resolves it via sys.executable's directory).
    if [ -n "$TUI_BINARY" ] && [ -f "$TUI_BINARY" ]; then
        echo "  Copying TUI binary next to backend executable..."
        cp "$TUI_BINARY" "$BACKEND_DIR/jiuwenswarm-tui"
        chmod +x "$BACKEND_DIR/jiuwenswarm-tui"
        check_arch "$BACKEND_DIR/jiuwenswarm-tui"
    fi

    # Bundle the Node runtime (same contract as build-macos.sh; the frozen
    # backend entry looks it up at <.app>/Contents/Resources/node-runtime/bin
    # and prepends it to PATH, so Agent skills get node without a
    # user-installed Node.js). Bundling happens before the .app rename, so the
    # destination uses the pre-rename Electron.app path.
    if [ "$BUNDLE_NODE" = "1" ]; then
        echo "  Bundling Node.js runtime into app..."
        copy_node_runtime "$NODE_SRC" "$ELECTRON_APP_DIR/Electron.app/Contents/Resources/node-runtime"
        echo "  Verifying frozen Playwright MCP bundle and bundled Node..."
        JIUWENSWARM_EXPECTED_NODE="$ELECTRON_APP_DIR/Electron.app/Contents/Resources/node-runtime/bin/node" \
            "$BACKEND_DIR/$BUILD_EXECUTABLE_NAME" "$PROJECT_ROOT/scripts/verify_playwright_mcp_bundle.py"
    fi
fi

# Create package.json for the Electron app (main.cjs is CommonJS). The pinned
# MCP package is parsed from main.cjs, matching the Windows Electron builder,
# so packaged browser agents never depend on user-installed Node/npm.
MCP_PACKAGE_SPEC="$(sed -n "s/^const PLAYWRIGHT_MCP_PACKAGE = '\([^']*\)';/\1/p" "$DESKTOP_DIR/main.cjs")"
MCP_PACKAGE_NAME="${MCP_PACKAGE_SPEC%@*}"
MCP_PACKAGE_VERSION="${MCP_PACKAGE_SPEC##*@}"
if [ -z "$MCP_PACKAGE_SPEC" ] \
    || [ "$MCP_PACKAGE_NAME" = "$MCP_PACKAGE_SPEC" ] \
    || [ -z "$MCP_PACKAGE_VERSION" ]; then
    echo "ERROR: failed to parse PLAYWRIGHT_MCP_PACKAGE from main.cjs" >&2
    exit 1
fi

cat > "$APP_DIR/package.json" << PKGJSON
{
    "name": "jiuwenswarm-electron",
    "version": "$BUILD_VERSION",
    "main": "main.cjs",
    "dependencies": {
        "$MCP_PACKAGE_NAME": "$MCP_PACKAGE_VERSION"
    }
}
PKGJSON

echo "  Installing bundled MCP runtime ($MCP_PACKAGE_SPEC)..."
pushd "$APP_DIR" >/dev/null
npm install --omit=dev --no-audit --no-fund
popd >/dev/null
if [ ! -d "$APP_DIR/node_modules/@playwright/mcp" ]; then
    echo "ERROR: bundled @playwright/mcp runtime is missing" >&2
    exit 1
fi

REQUIRED_FILES=(main.cjs preload.cjs browser_panels.cjs share_image_export.cjs display_media_picker.cjs target_mcp_wrapper.cjs
    macos_update.cjs install_macos_update.sh package.json dist/index.html
    node_modules/@playwright/mcp/package.json node_modules/@playwright/mcp/cli.js)
for required in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$APP_DIR/$required" ]; then
        echo "ERROR: required Electron resource missing: $required" >&2
        exit 1
    fi
done

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
if [ ! -f "$ICON_PATH" ]; then
    echo "ERROR: required macOS icon is missing: $ICON_PATH" >&2
    exit 1
fi
cp "$ICON_PATH" "$ICON_DST"
echo "  Set app icon to logo.icns"

# Rename Electron.app to the canonical product bundle name from build_config.
mv "$ELECTRON_APP_DIR/Electron.app" "$ELECTRON_APP_DIR/$BUILD_APP_BUNDLE_NAME"
APP_BUNDLE="$ELECTRON_APP_DIR/$BUILD_APP_BUNDLE_NAME"

# Electron uses the executable name to distinguish packaged apps from its
# development runtime. Renaming only the .app leaves app.isPackaged false.
mv "$APP_BUNDLE/Contents/MacOS/Electron" "$APP_BUNDLE/Contents/MacOS/$BUILD_EXECUTABLE_NAME"

# Update Info.plist
PLIST="$APP_BUNDLE/Contents/Info.plist"
if [ -f "$PLIST" ]; then
    /usr/libexec/PlistBuddy -c "Set :CFBundleExecutable $BUILD_EXECUTABLE_NAME" "$PLIST"
    /usr/libexec/PlistBuddy -c "Set :CFBundleName $BUILD_DISPLAY_NAME" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName $BUILD_DISPLAY_NAME" "$PLIST" 2>/dev/null || true
    # 捆绑标识与版本号来自 build_config：默认的 Electron 标识会与其他
    # 未改名 Electron 应用冲突（同标识无法共存于 /Applications）。
    /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $BUILD_BUNDLE_IDENTIFIER" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $BUILD_VERSION" "$PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_VERSION" "$PLIST" 2>/dev/null || true
    echo "  Updated Info.plist"
fi

BUNDLE_EXECUTABLE="$(/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" "$PLIST")"
if [ ! -x "$APP_BUNDLE/Contents/MacOS/$BUNDLE_EXECUTABLE" ]; then
    echo "ERROR: Electron bundle executable is missing: $BUNDLE_EXECUTABLE" >&2
    exit 1
fi

# Sign nested native code inside-out, including code under Resources (which
# codesign --deep does not reliably discover). Seal the outer app last.
if [ "$DO_SIGN" = "1" ]; then
    echo "  Code signing Electron.app (hardened runtime)..."
    while IFS= read -r -d '' native_file; do
        if file -b "$native_file" | grep -q 'Mach-O'; then
            check_arch "$native_file"
            codesign --force --timestamp --options runtime \
                --entitlements "$ENTITLEMENTS" --sign "$SIGN_IDENTITY" "$native_file"
        fi
    done < <(find "$APP_BUNDLE" -type f -print0)
    while IFS= read -r -d '' nested_bundle; do
        codesign --force --timestamp --options runtime \
            --entitlements "$ENTITLEMENTS" --sign "$SIGN_IDENTITY" "$nested_bundle"
    done < <(find "$APP_BUNDLE/Contents" -depth -type d \( -name '*.app' -o -name '*.framework' \) -print0)
    codesign --force --timestamp --options runtime \
        --entitlements "$ENTITLEMENTS" \
        --sign "$SIGN_IDENTITY" \
        "$APP_BUNDLE"
    codesign --verify --deep --strict --verbose=2 "$APP_BUNDLE"
else
    echo "  Skipping app signing; artifact is for local testing only."
fi

if [ "$DO_NOTARIZE" = "1" ]; then
    APP_ZIP="$ELECTRON_APP_DIR/$BUILD_APP_BUNDLE_NAME.zip"
    rm -f "$APP_ZIP"
    ditto -c -k --keepParent "$APP_BUNDLE" "$APP_ZIP"
    xcrun notarytool submit "$APP_ZIP" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$APP_BUNDLE"
    xcrun stapler validate "$APP_BUNDLE"
    rm -f "$APP_ZIP"
fi

# Calculate size
APP_SIZE=$(du -sh "$ELECTRON_APP_DIR" | awk '{print $1}')
echo "  Assembled app size: $APP_SIZE"

# ── 6. Package .dmg ──────────────────────────────────────────────────────────
echo ""
echo "[6/6] Building .dmg..."

DMG_DIR="$DIST_DIR"
VERSION="$BUILD_VERSION"

if [ "$FRONTEND_ONLY" = true ]; then
    DMG_NAME="$BUILD_DISPLAY_NAME-electron-frontend-test-$VERSION-$BUILD_ARCH.dmg"
elif [ "$TEST" = true ]; then
    DMG_NAME="$BUILD_DISPLAY_NAME-electron-test-$VERSION-$BUILD_ARCH.dmg"
else
    DMG_NAME="$BUILD_DMG_FILENAME"
fi

DMG_PATH="$DMG_DIR/$DMG_NAME"

# Create a temporary DMG directory
DMG_STAGING="$(mktemp -d "$DIST_DIR/.electron-dmg-staging.XXXXXX")"
trap 'rm -rf "$DMG_STAGING"' EXIT

# Copy app bundle
cp -R "$APP_BUNDLE" "$DMG_STAGING/"

# Create Applications symlink
ln -s /Applications "$DMG_STAGING/Applications"

# Create DMG
rm -f "$DMG_PATH"
hdiutil create -volname "$BUILD_DISPLAY_NAME" -srcfolder "$DMG_STAGING" -ov -format UDZO "$DMG_PATH"
rm -rf "$DMG_STAGING"

if [ "$DO_SIGN" = "1" ]; then
    codesign --force --sign "$SIGN_IDENTITY" "$DMG_PATH"
    codesign --verify --verbose=2 "$DMG_PATH"
fi
hdiutil verify "$DMG_PATH"

if [ "$DO_NOTARIZE" = "1" ]; then
    xcrun notarytool submit "$DMG_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG_PATH"
    xcrun stapler validate "$DMG_PATH"
    spctl -a -t open --context context:primary-signature -v "$DMG_PATH"
elif [ "$DO_SIGN" = "1" ]; then
    # A signed-but-not-notarized image is useful for internal checks but is not
    # expected to pass Gatekeeper's distribution assessment.
    spctl -a -t open --context context:primary-signature -v "$DMG_PATH" 2>&1 || true
fi

DMG_SIZE=$(du -h "$DMG_PATH" | awk '{print $1}')

echo ""
echo "=== Build complete ==="
echo "DMG: $DMG_PATH"
echo "Size: $DMG_SIZE"
if [ "$DO_NOTARIZE" = "1" ]; then
    echo "The Electron DMG is signed, notarized, stapled, and Gatekeeper-verified."
elif [ "$DO_SIGN" = "1" ]; then
    echo "The Electron DMG is signed but not notarized; set NOTARIZE=1 for distribution."
else
    echo "The Electron DMG is unsigned and intended for local testing only."
fi
