# Desktop packaging (Windows & macOS)

This guide explains how to build a desktop app with **uv**, **PyInstaller**, and **pywebview**. Supported outputs: Windows **`onedir`** layout (for Inno Setup) and macOS **`.app` + `.dmg`**.

## Prerequisites

- **uv**: Python package manager used by the project
- **Node.js**: **Build-time only** for the web UI; the shipped app does not require Node at runtime
- **Windows**: `onedir` output for Inno Setup installers
- **macOS**: `.app` bundle and `.dmg`

## Files

| Path | Role |
|------|------|
| `scripts/jiuwenclaw.spec` | PyInstaller spec |
| `scripts/jiuwenclaw_exe_entry.py` | Exe entry (desktop mode + subcommands) |
| `jiuwenclaw/desktop_app.py` | pywebview window and local server |
| `scripts/build-exe.ps1` | One-shot build (PowerShell) |
| `scripts/build-exe.bat` | One-shot build (batch) |
| `scripts/build-macos.sh` | macOS `.app` + `.dmg` |

## Windows

### Option A: scripts (recommended)

From the repo root:

```powershell
.\scripts\build-exe.ps1
```

Or double-click `scripts\build-exe.bat`.

The script installs deps, builds the frontend, and runs PyInstaller.

### Option B: manual

#### 1. uv and deps

```bash
# Install uv if needed (PowerShell):
# irm https://astral.sh/uv/install.ps1 | iex

cd <your-repo-root>
uv sync --extra dev
```

#### 2. Build the web UI

```bash
cd jiuwenclaw/web
npm install
npm run build
cd ../..
```

Static files land in `jiuwenclaw/web/dist`.

#### 3. PyInstaller

```bash
uv run pyinstaller scripts/jiuwenclaw.spec
```

Output: `dist/jiuwenclaw/`, main binary `dist/jiuwenclaw/jiuwenclaw.exe`.

## Using the Windows build

### First run

1. **Initialize** (required once):

   ```bash
   jiuwenclaw.exe init
   ```

   Creates `~/.jiuwenclaw` config and workspace.

2. **Configure**: edit `%USERPROFILE%\.jiuwenclaw\.env` (`API_KEY`, `MODEL_PROVIDER`, …).

3. **Start**:

   ```bash
   jiuwenclaw.exe
   ```

   Starts backend + static UI in a borderless pywebview window (default `http://127.0.0.1:5173`); you usually do not open a separate browser.

## Inno Setup notes

- Package the whole `dist/jiuwenclaw/` directory.
- Entry point: `dist/jiuwenclaw/jiuwenclaw.exe`.
- Run `jiuwenclaw.exe init` from the installer finish page if needed.
- User data lives under `%USERPROFILE%\.jiuwenclaw` — do not delete on uninstall by default.
- Share one `.ico` between `jiuwenclaw.spec` and Inno Setup if you add an icon.

### Subcommands

| Command | Role |
|---------|------|
| `jiuwenclaw.exe` | Start desktop app |
| `jiuwenclaw.exe init` | Initialize workspace |

## macOS

```bash
chmod +x scripts/build-macos.sh
./scripts/build-macos.sh
```

Produces `dist/JiuwenClaw.app` and e.g. `dist/JiuwenClaw-0.1.7.dmg`.

- Open the `.app` or mount the `.dmg` and drag to **Applications**.
- Not codesigned/notarized — fine for local testing; for distribution add `.icns`, signing, and notarization.
- First launch may require **Open** from the context menu (Gatekeeper).

## Technical notes

- **Python**: Bundled by PyInstaller; end users do not install Python.
- **pywebview**: Loads local `http://127.0.0.1:5173`.
- **Node**: Only for building the React app; runtime uses static files.
- **Workspace**: Same as pip install — `~/.jiuwenclaw`.
- **Inno**: Ship the full `dist/jiuwenclaw/` tree, not a single exe only.
- **DMG**: Script may include an **Applications** shortcut for drag install.

## Troubleshooting

### Missing `web/dist`

Run `cd jiuwenclaw/web && npm run build`.

### `ModuleNotFoundError` at runtime

Add missing modules to `hiddenimports` in `scripts/jiuwenclaw.spec` and rebuild.

### Large bundle

`onedir` is intentional. Trim further via `excludes` in the spec.

### Antivirus false positives

Add exclusions or sign the binary if you have a certificate.
