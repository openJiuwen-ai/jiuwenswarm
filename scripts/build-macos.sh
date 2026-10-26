#!/usr/bin/env bash
# JiuwenSwarm macOS .app + .dmg build script
#
# 签名/公证是可选的，按机器是否配置了 Developer ID 身份自动决定：
#   - 配置了 Developer ID 身份（含私钥）→ 自动用真签名；加 NOTARIZE=1 再做 Apple 公证+staple
#   - 没配置签名身份（如全新机器）→ 跳过签名，等同原先未签名构建，产物仅本地可用
#
# 前置条件（仅分发时需要）：
#   1. 本机 login 钥匙串已装入 "Developer ID Application: ..." 身份（含私钥）。
#      `security find-identity -v -p codesigning` 应看到 1 valid identity。
#   2.（公证）已用 notarytool 存入 keychain profile，例如：
#        xcrun notarytool store-credentials "jiuwenswarm-notary" \
#          --key AuthKey_XXXXXXXXXX.p8 --key-id KEY_ID --issuer ISSUER_ID
#
# 用法：
#   bash scripts/build-macos.sh             # 有身份就签名、没身份就跳过（不公证）
#   NOTARIZE=1 bash scripts/build-macos.sh  # 签名 + 公证 + staple（分发用，需真身份）
#   SIGN_IDENTITY="-" bash scripts/build-macos.sh   # 强制 ad-hoc 本地测试签名
#   NOSIGN=1 bash scripts/build-macos.sh           # 强制跳过签名，构建未签名 DMG（= 原先流程）

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="JiuwenSwarm.app"
APP_PATH="$PROJECT_ROOT/dist/$APP_NAME"
DMG_ROOT="$PROJECT_ROOT/dist/dmg-root"
VERSION="0.2.3"
DMG_PATH="$PROJECT_ROOT/dist/JiuwenSwarm-$VERSION.dmg"

# === 签名 + 公证配置 ===
# 签名身份（codesign -s 的值）与是否签名。解析顺序：
#   1. 环境变量 SIGN_IDENTITY（可设具体身份，或 "-" 做 ad-hoc 本地测试）
#   2. 自动探测 keychain 里唯一的 "Developer ID Application" 身份
#   3. 都没有 → DO_SIGN=0，跳过签名（等同原先未签名构建，产物仅本地可用，不过 Gatekeeper）
ENTITLEMENTS="$PROJECT_ROOT/scripts/entitlements.mac.plist"
NOTARY_PROFILE="${NOTARY_PROFILE:-jiuwenswarm-notary}"
# 设 NOTARIZE=1 才执行公证（提交 Apple + staple）
DO_NOTARIZE="${NOTARIZE:-0}"
DO_SIGN=1

if [ "${NOSIGN:-0}" = "1" ]; then
  # 显式跳过签名（即使本机有 Developer ID 身份），构建未签名 DMG = 原先流程
  DO_SIGN=0
  SIGN_IDENTITY=""
elif [ -z "${SIGN_IDENTITY:-}" ]; then
  # 未显式指定：自动找 keychain 里的 Developer ID Application 身份（取第一个）
  SIGN_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
    | grep -oE 'Developer ID Application: [^(]+\([0-9A-Z]+\)' \
    | head -1 || true)"
  if [ -z "$SIGN_IDENTITY" ]; then
    # 新机器/未配置签名身份：跳过签名，保留 PyInstaller 默认 ad-hoc，等同原先流程
    DO_SIGN=0
  fi
fi

# ── 内置 Node 运行时（单架构，M 系列优先 arm64）─────────────────
# 任选 ≥ v18 的 LTS；v20/v22 为推荐版本。可用环境变量覆盖。
NODE_VERSION="${NODE_VERSION:-v22.11.0}"
BUNDLE_NODE="${BUNDLE_NODE:-1}"   # 0=跳过内置 node

# 返回构建机的 Node 架构名。M 系列(arm64) 优先；Intel 回退 x64。
resolve_node_arch() {
  case "$(uname -m)" in
    arm64) echo "arm64" ;;
    *)     echo "x64"   ;;
  esac
}

# 校验某个目录是一份「能真正跑起来」的 Node：既有 bin/node，又能执行 --version。
# 比 [[ -x ... ]] 强：能挡住下载损坏 / 架构不匹配 / 只解压了一半的情况。
node_runs() {
  local n="$1/bin/node"
  [[ -x "$n" ]] && "$n" --version >/dev/null 2>&1
}

# 解析 Node 运行时来源，优先级：$NODE_DIR > vendor/node > nodejs.org 官方下载
resolve_node_dir() {
  if [[ -n "${NODE_DIR:-}" ]] && node_runs "$NODE_DIR"; then
    echo "$NODE_DIR"; return
  fi
  local vendored="$PROJECT_ROOT/vendor/node"
  if node_runs "$vendored"; then
    echo "$vendored"; return
  fi
  local arch dir url
  arch="$(resolve_node_arch)"
  dir="$vendored"
  printf 'Downloading Node %s (%s) from nodejs.org...\n' "$NODE_VERSION" "$arch" >&2
  url="https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-darwin-${arch}.tar.gz"
  mkdir -p "$PROJECT_ROOT/vendor"

  # 先把 tarball 下载、解压到临时目录，校验 bin/node 可执行后，
  # 再 rm -rf 旧缓存并原子替换。任一步失败都不会破坏已有 vendor/node。
  local tmpdir staged tarball
  tmpdir="$(mktemp -d)"
  tarball="$tmpdir/node.tar.gz"
  staged="$tmpdir/node-${NODE_VERSION}-darwin-${arch}"

  if ! curl -fL "$url" -o "$tarball"; then
    rm -rf "$tmpdir"
    printf 'Error: failed to download %s\n' "$url" >&2
    return 1
  fi

  tar -xzf "$tarball" -C "$tmpdir"
  if ! node_runs "$staged"; then
    rm -rf "$tmpdir"
    printf 'Error: extracted tarball has no working bin/node (%s)\n' "$staged" >&2
    return 1
  fi

  rm -rf "$dir"          # 仅在替换物校验通过后才删旧缓存
  mv "$staged" "$dir"
  if ! node_runs "$dir"; then   # 跨卷 mv 可能只搬了一半，落地后再校验一次
    rm -rf "$dir" "$tmpdir"
    printf 'Error: node moved into %s but bin/node does not run\n' "$dir" >&2
    return 1
  fi
  rm -rf "$tmpdir"
  echo "$dir"
}

# 把 Node 运行时拷进 .app 包的 Contents/Resources/node-runtime
copy_node_into_app() {
  local src="$1"
  local dest="$APP_PATH/Contents/Resources/node-runtime"
  # 先确认来源 node 真能跑，否则 cp 会吐出晦涩的 'No such file or directory'。
  if ! node_runs "$src"; then
    printf 'Error: node source not usable — %s/bin/node missing or not runnable\n' "$src" >&2
    printf '       if this is the cached vendor/node, remove it and re-run to re-download.\n' >&2
    return 1
  fi
  rm -rf "$dest"
  mkdir -p "$dest"
  # cp -R 保留 npx/npm 符号链接（指向 ../lib/node_modules/...）与可执行位。
  cp -R "$src/." "$dest/"
  printf 'Bundled Node %s (%s) into app: %s\n' \
    "$( "$dest/bin/node" --version )" "$(resolve_node_arch)" "$dest"
}

printf '=== JiuwenSwarm macOS package build ===\n'
printf 'Project root: %s\n' "$PROJECT_ROOT"
if [ "$DO_SIGN" = "1" ]; then
  printf 'Sign identity: %s\n' "$SIGN_IDENTITY"
else
  printf 'Sign identity: (skipped，本机未配置签名身份，产物仅本地可用)\n'
fi
if [ "$DO_NOTARIZE" = "1" ]; then
  printf 'Notarization: ENABLED (profile=%s)\n' "$NOTARY_PROFILE"
else
  printf 'Notarization: DISABLED（未设 NOTARIZE=1，不公证）\n'
fi
printf '\n'

# 校验签名身份 / 公证前提
if [ "$DO_SIGN" = "0" ]; then
  if [ "$DO_NOTARIZE" = "1" ]; then
    printf '错误：NOTARIZE=1 需要真实 Developer ID 身份。请先导入 .p12，或用 SIGN_IDENTITY 显式指定。\n' >&2
    exit 1
  fi
elif [ "$SIGN_IDENTITY" = "-" ]; then
  printf '注意：ad-hoc 签名（SIGN_IDENTITY="-"），仅供本地测试，无法通过 Gatekeeper/公证。\n'
  if [ "$DO_NOTARIZE" = "1" ]; then
    printf '错误：公证（NOTARIZE=1）需要真实的 Developer ID 身份，不能用 ad-hoc "-"。\n' >&2
    exit 1
  fi
elif ! security find-identity -v -p codesigning | grep -q "$SIGN_IDENTITY"; then
  printf '错误：钥匙串中找不到签名身份 "%s"\n' "$SIGN_IDENTITY" >&2
  printf '      用 security find-identity -v -p codesigning 查看可用身份。\n' >&2
  exit 1
fi

printf '[1/9] Install Python dependencies (uv sync --extra dev)...\n'
uv sync --extra dev

printf '\n[2/9] Build frontend (jiuwenswarm/channels/web/frontend)...\n'
rm -rf "$PROJECT_ROOT/jiuwenswarm/web/dist"
pushd "$PROJECT_ROOT/jiuwenswarm/channels/web/frontend" >/dev/null
npm install
npm run build
popd >/dev/null

TUI_BINARY=""
printf '\n[3/9] Build TUI native binary (Bun)...\n'
if command -v bun &>/dev/null; then
  pushd "$PROJECT_ROOT/jiuwenswarm/channels/tui/frontend" >/dev/null
  bun install
  popd >/dev/null
  TUI_BINARY="$(uv run python scripts/build_tui.py --target current | tail -n1)"
  if [[ -z "$TUI_BINARY" ]]; then
    printf 'Warning: TUI build produced no output, skipping TUI.\n'
    TUI_BINARY=""
  else
    TUI_BINARY="$PROJECT_ROOT/$TUI_BINARY"
    if [[ ! -f "$TUI_BINARY" ]]; then
      printf 'Warning: TUI binary not found at %s, skipping TUI.\n' "$TUI_BINARY"
      TUI_BINARY=""
    fi
  fi
else
  printf 'Warning: bun not found, skipping TUI build.\n'
  printf 'Install bun: curl -fsSL https://bun.sh/install | bash\n'
fi

printf '\n[4/9] Build macOS app bundle with PyInstaller...\n'
uv run pyinstaller scripts/jiuwenswarm.spec --noconfirm

if [[ ! -d "$APP_PATH" ]]; then
  printf 'Error: app bundle not found: %s\n' "$APP_PATH" >&2
  exit 1
fi

printf 'Verifying frozen A2UI v0.8 bundle...\n'
"$APP_PATH/Contents/MacOS/jiuwenswarm" "$PROJECT_ROOT/scripts/verify_a2ui_bundle.py"

if [[ -n "$TUI_BINARY" && -f "$TUI_BINARY" ]]; then
  printf 'Copying TUI binary into app bundle...\n'
  cp "$TUI_BINARY" "$APP_PATH/Contents/MacOS/jiuwenswarm-tui"
  chmod +x "$APP_PATH/Contents/MacOS/jiuwenswarm-tui"
fi

# 内置 Node 运行时：在打 DMG 之前暂存进 .app，随后 cp -R app 到 dmg-root
# 会把它一并卷进 DMG。
if [[ "$BUNDLE_NODE" == "1" ]]; then
  printf '\n[5/9] Bundle Node.js runtime (single arch, M-series first)...\n'
  NODE_SRC="$(resolve_node_dir)" || exit 1
  copy_node_into_app "$NODE_SRC"
fi

# 注意：--deep 会打印 deprecation 警告，但对 PyInstaller 应用（含大量嵌套 .so/.dylib、
# TUI 二进制、内置 node-runtime）仍是最省事的方式，会把所有可执行码一次性签掉。
if [ "$DO_SIGN" = "1" ]; then
  printf '\n[6/9] Code sign the .app (hardened runtime + entitlements, --deep)...\n'
  codesign --force --deep --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --sign "$SIGN_IDENTITY" \
    "$APP_PATH"
  codesign --verify --deep --strict --verbose=2 "$APP_PATH"
else
  printf '\n[6/9] Skipping .app signing (本机未配置签名身份).\n'
fi

if [ "$DO_NOTARIZE" = "1" ]; then
  printf '\n[7/9] Notarize the .app (submit to Apple + staple)...\n'
  ditto -c -k --keepParent "$APP_PATH" "$APP_PATH.zip"
  xcrun notarytool submit "$APP_PATH.zip" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP_PATH"
  rm -f "$APP_PATH.zip"
  xcrun stapler validate "$APP_PATH"
else
  printf '\n[7/9] Skipping .app notarization (NOTARIZE 未启用).\n'
fi

printf '\n[8/9] Create DMG...\n'
rm -rf "$DMG_ROOT"
mkdir -p "$DMG_ROOT"
cp -R "$APP_PATH" "$DMG_ROOT/"
ln -s /Applications "$DMG_ROOT/Applications"
rm -f "$DMG_PATH"
hdiutil create -volname "JiuwenSwarm" -srcfolder "$DMG_ROOT" -ov -format UDZO "$DMG_PATH"

printf '\n[9/9] Code sign + notarize + staple the DMG, then verify...\n'
if [ "$DO_SIGN" = "1" ]; then
  codesign --sign "$SIGN_IDENTITY" "$DMG_PATH"
fi
if [ "$DO_NOTARIZE" = "1" ]; then
  xcrun notarytool submit "$DMG_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG_PATH"
  xcrun stapler validate "$DMG_PATH"
fi
# Gatekeeper 评估（公证后应 accepted, source=Notarized Developer ID）
if [ "$DO_SIGN" = "1" ]; then
  spctl -a -t open --context context:primary-signature -v "$DMG_PATH" 2>&1 || true
fi

printf '\n=== Build complete ===\n'
printf 'App bundle: %s\n' "$APP_PATH"
printf 'DMG file:   %s\n' "$DMG_PATH"
if [ "$DO_NOTARIZE" = "1" ]; then
  printf '该 DMG 已签名+公证+staple，可分发给用户通过 Gatekeeper。\n'
elif [ "$DO_SIGN" = "1" ]; then
  printf '提示：当前只签名未公证。分发前请用 NOTARIZE=1 重新构建。\n'
else
  printf '提示：未签名（本地构建）。分发前需在配置好签名身份的机器上用 NOTARIZE=1 重新构建。\n'
fi
