# Desktop Auto-Update Design

This document describes the auto-update solution for JiuwenSwarm desktop (Windows and macOS). The goal is to prioritize stability while covering both stable and pre-release (beta) upgrade flows.

## Scope

- Supports Windows and macOS desktop (Linux desktop also applies)
- Automatic update check on startup
- Manual update check via the sidebar "Update" page
- Desktop update source defaults to GitCode Releases, switchable to GitHub Releases; pip install mode uses PyPI
- Download artifacts differ per platform:
  - Windows: Inno Setup installer `JiuwenSwarm-setup-<version>.exe`
  - macOS: DMG image `JiuwenSwarm-<version>.dmg`
  - Linux: `JiuwenSwarm-<version>.tar.gz`
- After download, an external helper completes installation and restart: Windows via an interactive install wizard, macOS / Linux via a silent helper script that installs and restarts
- Pre-release support: stable and pre-release releases share the same update channel, so stable users also receive beta pushes

## Out of Scope

- No incremental/delta updates
- No in-process self-replacement
- No version-skip, canary releases, or multi-channel distribution
- No forced updates

## Version Numbers and Pre-release Rules

Installer naming examples:

| Type | Windows | macOS |
|---|---|---|
| Stable | `JiuwenSwarm-setup-0.2.2.exe` | `JiuwenSwarm-0.2.2.dmg` |
| Pre-release | `JiuwenSwarm-setup-0.2.3.beta1.exe` | `JiuwenSwarm-0.2.3.beta1.dmg` |

Windows and macOS installers for the same version are released together.

Version comparison uses a total-order key `release_sort_key` with these rules:

1. Base version compared numerically first (segment by segment): `0.2.3` > `0.2.2`
2. At the same base, a stable release ranks above any pre-release: `0.2.3` > `0.2.3.beta1`
3. Among pre-releases at the same base, the type decides: `dev` < `alpha` < `beta` < `rc` < `pre`
4. Within the same type, a larger number is newer: `0.2.3.beta2` > `0.2.3.beta1`

Because stable and pre-release releases share a channel, a user running stable `0.2.2` is prompted to update to `0.2.3.beta1` (a higher base version); this is the intended behavior.

## Core Flow

1. After app launch, the frontend asynchronously calls `updater.check`
2. The backend requests the Releases list endpoint and fetches all published releases (including pre-releases, skipping drafts)
3. The newest version is selected by `release_sort_key` and compared against the current `__version__`
4. If a newer version is found, the latest version, publish date, release notes, and the platform-matched installer download URL are recorded
5. The user clicks "Download Update" on the Update page
6. The backend downloads the installer to the `.updates` directory under the user workspace in the background
7. After download completes, the frontend calls the pywebview API `install_update` to trigger installation
8. The desktop process launches the platform-specific helper, which waits for the current process and ports to release, then performs installation (interactive on Windows, silent on macOS / Linux) and restarts the app

## Update Source

Desktop defaults to the GitCode Releases list endpoint:

```text
https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases
```

It can also be switched to GitHub Releases. To discover pre-releases, the backend fetches the full releases list (not the `/latest` endpoint, which excludes pre-releases), skips drafts, keeps prereleases, and picks the newest by version sort. It falls back to `/latest` when the list endpoint is unavailable.

Fields read from the release:

- `tag_name` — version number (pre-release suffix preserved, e.g. `0.2.3.beta1`)
- `body` — release notes
- `published_at` — publish date
- `assets[]` — the platform-matched installer

## Configuration

Update settings are in the `updater` section of `config.yaml`:

```yaml
updater:
  enabled: true
  desktop_release_api_type: gitcode   # gitcode | github
  repo_owner: openJiuwen
  repo_name: jiuwenswarm
  release_api_url: ""
  asset_name_pattern_windows: "JiuwenSwarm-setup-{version}.exe"
  asset_name_pattern_macos: "JiuwenSwarm-{version}.dmg"
  asset_name_pattern_linux: "JiuwenSwarm-{version}.tar.gz"
  timeout_seconds: 20
```

Pip install mode additionally supports a `pypi_mirror` field.

## Backend API

The following WebSocket RPC methods are registered:

- `updater.get_status` — query current update status
- `updater.check` — check for updates
- `updater.download` — download the installer (desktop mode) / perform pip upgrade (pip mode)
- `updater.upgrade` — pip mode only, perform upgrade and restart
- `updater.set_conf` — save update configuration

In desktop mode, installation is triggered by the frontend via the pywebview API `install_update(installer_path)`, executed directly by the desktop process (it owns the window and can close it before installation).

Status values:

- `idle`
- `checking`
- `up_to_date`
- `update_available`
- `downloading`
- `downloaded`
- `installing`
- `upgrading` (pip mode)
- `restart_pending` / `restarting` (pip mode)
- `error`
- `unsupported`
- `disabled`

## Installation Execution

To avoid replacing files while the main process is running, installation is not performed within the current process. When the desktop process receives an install request from the frontend, it launches a platform-specific helper process/script that completes installation and restart after the main process exits.

### Windows

The desktop process launches an independent update-helper subprocess via the `update-helper` subcommand, passing the installer path, app executable path, and parent PID. The helper flow:

1. Wait for the parent process to exit
2. Wait for backend / frontend ports to release (up to 15 seconds)
3. Launch the installer interactively (no silent arguments), showing the Inno Setup wizard

The installer handles elevation (UAC prompt) and file replacement itself via Inno Setup. After the user completes the wizard, the installer is responsible for relaunching the app (Inno Setup's `[Run]` section can be configured to launch the app after install). The helper exits right after launching the installer; the installation is left to the user and the installer.

### macOS

The desktop process generates a bash helper script and launches it independently. The script flow:

1. Wait for the parent process to exit
2. Wait for backend / frontend ports to release (up to 15 seconds)
3. `hdiutil attach` mounts the DMG at a controlled mount point
4. Find the `.app` bundle inside the mount point
5. `ditto` copies the `.app` to a temp target `<install_target>.new`
6. Atomic swap: move the old bundle aside as `<install_target>.old`, move the new one into place, then remove the old one
7. `hdiutil detach` unmounts the DMG and cleans up the mount point
8. `xattr -dr com.apple.quarantine` removes the quarantine attribute
9. `open` launches the new app

The install target is fixed to `/Applications/JiuwenSwarm.app` (derived by walking up from the executable path to the `.app` bundle name).

### Linux

The desktop process generates a bash helper script that, after the parent process exits, backs up the current install directory, extracts the tar.gz into it, removes the backup, and relaunches `jiuwenswarm`.

## Security Notes

- All external paths are escaped with `shlex.quote` in helper scripts to prevent shell injection if the release API serves a malicious asset name
- Helper scripts are written to the `.updates` directory under the user workspace, with write permission checked before writing
- The macOS helper writes full execution logs to `update_helper.log` in the logs directory
