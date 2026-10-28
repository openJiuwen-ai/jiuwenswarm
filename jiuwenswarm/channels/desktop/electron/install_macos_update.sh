#!/bin/bash
# Arguments are passed as argv, never interpolated into shell source.
set -euo pipefail
INSTALLER="$1"
INSTALL_TARGET="$2"
PARENT_PID="$3"
CHILD_PIDS="$4"
PORTS="$5"
LOG_FILE="$6"
ARCH="$7"
BACKEND_EXECUTABLE="$8"
SHUTDOWN_RECEIPT="$9"
exec 3>&1
exec >>"$LOG_FILE" 2>&1
echo "=== Electron update: $(date) ==="

MOUNT_POINT="$(mktemp -d /tmp/workswarm-electron-mount.XXXXXX)"
STAGING=""
MOUNTED=0
BACKED_UP=0
INSTALLED=0
COMPLETED=0
cleanup() {
    local status=$?
    if [ "$COMPLETED" = 0 ] && [ "$status" = 0 ]; then status=1; fi
    trap - EXIT
    if [ "$status" -ne 0 ] && [ "$BACKED_UP" = 1 ]; then
        if [ "$INSTALLED" = 1 ]; then
            mv "$INSTALL_TARGET" "$STAGING/failed.app" || true
        fi
        if ! mv "$STAGING/previous.app" "$INSTALL_TARGET"; then
            echo "ERROR: restore failed; previous app retained at $STAGING/previous.app"
            STAGING=""
        fi
    fi
    if [ "$MOUNTED" = 1 ]; then hdiutil detach "$MOUNT_POINT" || true; fi
    rmdir "$MOUNT_POINT" 2>/dev/null || true
    if [ -n "$STAGING" ]; then rm -rf "$STAGING"; fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 1' TERM INT HUP

hdiutil verify "$INSTALLER"
hdiutil attach "$INSTALLER" -mountpoint "$MOUNT_POINT" -nobrowse -noautoopen </dev/null
MOUNTED=1
SOURCE_APP="$MOUNT_POINT/$(basename "$INSTALL_TARGET")"
test -d "$SOURCE_APP"
test ! -L "$SOURCE_APP"
test -f "$SOURCE_APP/Contents/Resources/app/main.cjs"
test -f "$SOURCE_APP/Contents/Resources/app/package.json"
test ! -f "$SOURCE_APP/Contents/Resources/app/.frontend-only"
test ! -f "$SOURCE_APP/Contents/Resources/app/.test"
CURRENT_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$INSTALL_TARGET/Contents/Info.plist")"
NEW_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$SOURCE_APP/Contents/Info.plist")"
test -n "$CURRENT_ID"
test "$CURRENT_ID" = "$NEW_ID"
NEW_EXE="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$SOURCE_APP/Contents/Info.plist")"
case "$NEW_EXE" in ''|*/*|.|..) exit 1 ;; esac
lipo "$SOURCE_APP/Contents/MacOS/$NEW_EXE" -verify_arch "$ARCH"
codesign --verify --deep --strict "$SOURCE_APP"
spctl --assess --type execute "$SOURCE_APP"

# Stage beside the destination so the final rename is on the same filesystem.
# Permission/space/signature failures happen before the running app is stopped.
STAGING="$(mktemp -d "$(dirname "$INSTALL_TARGET")/.electron-update.XXXXXX")"
ditto "$SOURCE_APP" "$STAGING/new.app"
codesign --verify --deep --strict "$STAGING/new.app"
printf 'READY\n' >&3
exec 3>&-

wait_exit() {
    local pid="$1"
    local deadline=$((SECONDS + 60))
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "ERROR: process $pid did not exit; keeping installed app"
            return 1
        fi
        sleep 0.2
    done
}
wait_exit "$PARENT_PID"
if [ -n "$CHILD_PIDS" ]; then
    IFS=',' read -r -a children <<< "$CHILD_PIDS"
    for pid in "${children[@]}"; do wait_exit "$pid"; done
fi
# A detached Worker need not be among Electron's original service children.
# Run the old backend before replacing its archive and require observed exit.
"$BACKEND_EXECUTABLE" --desktop-run-worker-lifecycle --shutdown-worker --timeout 30 --shutdown-receipt "$SHUTDOWN_RECEIPT"
if [ -n "$PORTS" ]; then
IFS=',' read -r -a ports <<< "$PORTS"
for port in "${ports[@]}"; do
    deadline=$((SECONDS + 15))
    while lsof -iTCP:"$port" -sTCP:LISTEN -P -n >/dev/null 2>&1; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "ERROR: port $port still occupied; keeping installed app"
            exit 1
        fi
        sleep 0.2
    done
done
fi
mv "$INSTALL_TARGET" "$STAGING/previous.app"
BACKED_UP=1
mv "$STAGING/new.app" "$INSTALL_TARGET"
INSTALLED=1
# Keep Gatekeeper/quarantine intact; only verified, trusted apps are installed.
open "$INSTALL_TARGET"
COMPLETED=1
echo "Update installed and relaunched: $INSTALL_TARGET"
