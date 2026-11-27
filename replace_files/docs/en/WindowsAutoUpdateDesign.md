# Windows auto-update design

Minimal auto-update path for the **Windows desktop** build: stability first, not silent “magic” upgrades.

## Scope

- Windows desktop only
- Check once at startup
- Manual check from the sidebar **Update** page
- Source: **GitHub Releases**
- Installer: Inno Setup **`jiuwenclaw-setup-<version>.exe`**
- After download, an external helper runs a **silent** install and restarts the app

## Out of scope

- Delta/patch updates
- In-place self-replacement while running
- macOS auto-install
- Skip lists, staged rollouts, multiple channels
- Forced updates

## Flow

1. After start, the frontend calls `updater.check` asynchronously.
2. Backend calls the GitHub Releases API for the latest release.
3. If newer, record version, published time, notes, and installer URL.
4. User clicks **Download** on the **Update** page.
5. Backend downloads the installer to `%USERPROFILE%\.jiuwenclaw\.updates`.
6. When done, the frontend calls pywebview to **install**.
7. The desktop process writes a temporary **cmd** helper that waits for exit.
8. Helper runs Inno Setup **silently**, then restarts `jiuwenclaw.exe`.

## Source

GitHub Releases API:

```text
https://api.github.com/repos/{owner}/{repo}/releases/latest
```

Read:

- `tag_name` — version
- `body` — release notes
- `published_at` — time
- `assets[]` — installer and optional `.sha256`

## Config

`config.yaml` → `updater`:

```yaml
updater:
  enabled: true
  repo_owner: CharlieZhao95
  repo_name: jiuwenclaw
  release_api_url: ""
  asset_name_pattern: "jiuwenclaw-setup-{version}.exe"
  sha256_name_pattern: "jiuwenclaw-setup-{version}.exe.sha256"
  timeout_seconds: 20
```

## RPC

WebSocket RPC methods:

- `updater.get_status`
- `updater.check`
- `updater.download`

States:

- `idle`
- `checking`
- `up_to_date`
- `update_available`
- `downloading`
- `downloaded`
- `installing`
- `error`
- `unsupported`

## Install mechanics

The running exe is not replaced in-process.

1. Desktop app writes a temp **cmd** helper.
2. Helper waits for the main process to exit.
3. Runs the installer:

```text
/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /CLOSEAPPLICATIONS
```

4. Starts `jiuwenclaw.exe` again after install completes.
