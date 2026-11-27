#!/usr/bin/env bash
# JiuwenClaw macOS .app + .dmg build script

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="JiuwenClaw.app"
APP_PATH="$PROJECT_ROOT/dist/$APP_NAME"
DMG_ROOT="$PROJECT_ROOT/dist/dmg-root"
VERSION="0.1.7"
DMG_PATH="$PROJECT_ROOT/dist/JiuwenClaw-$VERSION.dmg"

printf '=== JiuwenClaw macOS package build ===\n'
printf 'Project root: %s\n\n' "$PROJECT_ROOT"

printf '[1/4] Install Python dependencies (uv sync --extra dev)...\n'
uv sync --extra dev

printf '\n[2/4] Build frontend (jiuwenclaw/web)...\n'
pushd "$PROJECT_ROOT/jiuwenclaw/web" >/dev/null
npm install
npm run build
popd >/dev/null

printf '\n[3/4] Build macOS app bundle with PyInstaller...\n'
uv run pyinstaller scripts/jiuwenclaw.spec --noconfirm

if [[ ! -d "$APP_PATH" ]]; then
  printf 'Error: app bundle not found: %s\n' "$APP_PATH" >&2
  exit 1
fi

printf '\n[4/4] Create DMG...\n'
rm -rf "$DMG_ROOT"
mkdir -p "$DMG_ROOT"
cp -R "$APP_PATH" "$DMG_ROOT/"
ln -s /Applications "$DMG_ROOT/Applications"
rm -f "$DMG_PATH"
hdiutil create -volname "JiuwenClaw" -srcfolder "$DMG_ROOT" -ov -format UDZO "$DMG_PATH"

printf '\n=== Build complete ===\n'
printf 'App bundle: %s\n' "$APP_PATH"
printf 'DMG file:   %s\n' "$DMG_PATH"
