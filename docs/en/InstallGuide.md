# JiuwenSwarm install guide

> **Important:** Finishing installation does not mean the app is ready to use. You must complete model configuration first. See [Configuration](https://gitcode.com/openJiuwen/jiuwenswarm/blob/develop/docs/en/Configuration.md) ([Chinese](https://gitcode.com/openJiuwen/jiuwenswarm/blob/develop/docs/zh/%E9%85%8D%E7%BD%AE%E4%BF%A1%E6%81%AF.md)) for model setup.

---

## First-time installation

### Option 1: Desktop installer (dmg / exe)

For Windows and macOS users who want a ready-to-run app without setting up Python / Node.js themselves. Download the installer for your platform from the gitcode [Release](https://gitcode.com/openJiuwen/jiuwenswarm/releases) page.

| Platform | Artifact |
|----------|----------|
| Windows | `JiuwenSwarm-setup-<version>.exe` |
| macOS | `JiuwenSwarm-<version>.dmg` |

Releases: https://gitcode.com/openJiuwen/jiuwenswarm/releases

##### System requirements

Desktop installers are pre-built for a specific platform and architecture. Confirm your machine meets these before installing:

| Platform | OS version | Architecture | Privilege | Minimum runtime resources (suggested) |
|----------|------------|--------------|-----------|---------------------------------------|
| Windows | Windows 10 / 11 (64-bit) | x64 only | **Administrator privileges required** to run the installer (`PrivilegesRequired=admin`) | 2 CPU cores, 4 GB RAM, 2 GB free disk |
| macOS | macOS 11 (Big Sur) or newer | See "Architecture note" below | On first launch, right-click the app in Finder and choose "Open" to pass GateKeeper | 2 CPU cores, 4 GB RAM, 2 GB free disk |

> 💡 **Resources**: The values above are an empirical lower bound for a single-machine local run (Web UI plus at least one model channel). If you also enable browser automation (Playwright), vector retrieval (ChromaDB/FAISS), or connect multiple IM channels, 8 GB+ RAM is recommended. Actual cost is dominated by the configured models and concurrency.

##### Architecture note (macOS)

The macOS desktop build is a **single-architecture artifact**, not a Universal binary:

- A dmg built on an **Apple Silicon (M-series)** machine contains only `arm64` native binaries and Node runtime.
- A dmg built on an **Intel** machine contains only `x64` native binaries and Node runtime.

So pick the artifact that matches your Mac chip on the Release page (e.g. `JiuwenSwarm-<version>-arm64.dmg` or `JiuwenSwarm-<version>-x64.dmg`). **Downloading the wrong architecture will fail to start the bundled Node runtime**, so the Web front end cannot be built/loaded. If you are unsure of your chip, run `uname -m` in a terminal: `arm64` is Apple Silicon, `x86_64` is Intel.

#### 1. macOS: download the dmg with curl (recommended)

> ⚠️ **Important:** A `.dmg` downloaded through a browser gets a macOS quarantine flag (`com.apple.quarantine`). When opened it triggers a Gatekeeper check and may report "damaged and can't be opened" or "can't be verified developer". Downloading from the terminal with `curl` does not add the quarantine flag, so the dmg mounts and installs normally.

```bash
# Replace <version> with the target version
curl -L --fail -o JiuwenSwarm-<version>.dmg \
  https://gitcode.com/openJiuwen/jiuwenswarm/releases/download/JiuwenSwarm<version>/JiuwenSwarm-<version>.dmg
```

#### 2. Install and first launch

- **macOS**: double-click to mount the dmg, then drag `JiuwenSwarm.app` into `Applications`. If macOS blocks the first launch, right-click it in Finder and choose "Open".
- **Windows**: double-click the downloaded installer (`.exe`) and follow the prompts. The installer requests **administrator privileges** (a UAC elevation prompt); click "Yes" to continue. It initializes the workspace automatically. For the portable onedir build, run `jiuwenswarm.exe init` once manually.
  > ⚠️ Only the **64-bit** edition of Windows 10 / 11 is supported; 32-bit systems and earlier Windows versions are not.

On first launch the app creates `~/.jiuwenswarm/`. Then follow [Post-start verification](#3-post-start-verification) to finish model configuration.

> Match the version to the actual download link on the Release page. For desktop auto-update behavior (Windows and macOS), see [Desktop auto-update design](WindowsAutoUpdateDesign.md).

---

### Option 2: pip install

#### Environment check

The desktop installers already include the Python runtime and front-end static assets, so desktop users do not need to run these checks. This section applies only to pip and source installs; both support Windows 10/11, macOS 10.15+, and Linux.

> 📦 **About the wheel package**: The wheel distributed on PyPI is not tied to a specific OS image, but its runtime dependencies still follow the OS-version constraint above and the Python constraint in the table below. Note the desktop dmg requires macOS 11+, while pip/wheel only requires macOS 10.15+. Suggested runtime resources match the desktop installer: 2 CPU cores, 4 GB RAM, 2 GB free disk (8 GB+ if you enable browser/vector retrieval/multiple channels).

| Dependency | Version | Applies to | Notes |
|------------|---------|------------|-------|
| Python | ≥3.11, <3.14 | pip and source installs | Python 3.11 recommended |
| Node.js | 18.x or newer | Source install only | Builds the Web front end; the pip package already includes the static assets |
| Git | Latest | Source install only | Clones and updates the source tree |

Run the checks that apply to your installation method:

```bash
# pip and source installs: check Python
python --version
# Expect: Python 3.11.x, 3.12.x, or 3.13.x

# Source install only: check Node.js
node --version
# Expect: v18.x.x or newer

# Source install only: check Git
git --version
# Expect: git version 2.x.x
```

#### 1. Installation steps

```bash
# Create a virtual environment (recommended)
python -m venv jiuwenswarm-env

# Activate the virtual environment
# Windows:
jiuwenswarm-env\Scripts\activate
# macOS/Linux:
source jiuwenswarm-env/bin/activate

# Install JiuwenSwarm
## Option 1: default install
pip install jiuwenswarm

## Option 2: use a China mirror (recommended)
# Tsinghua mirror
pip install jiuwenswarm -i https://pypi.tuna.tsinghua.edu.cn/simple

# Aliyun mirror
pip install jiuwenswarm -i https://mirrors.aliyun.com/pypi/simple/
```

#### 2. First launch

```bash
# Initialize JiuwenSwarm (first run)
jiuwenswarm-init
# Start JiuwenSwarm
jiuwenswarm-start
```

After the first start, the app creates the config directory `~/.jiuwenswarm/`.

#### 3. Post-start verification

After a successful start, verify the installation:

1. **Open the Web UI**: in your browser go to `http://localhost:5173`
2. **Open configuration**: in the left sidebar choose **Configuration**
3. **Configure the model**: follow [Configuration](Configuration.md) to set up your model API
4. **Confirm it works**:
   - The Web UI loads
   - After model configuration you can run a basic chat (tools/MCP may be used depending on your setup)

![Example: Web UI connected with a successful verification chat](../assets/images/天气.png)

> 💡 **Tip:** If the Web UI does not load, check `~/.jiuwenswarm/logs/` for errors.

#### 4. Restarting the service

If you closed JiuwenSwarm and want to run it again:

```bash
# Start again
jiuwenswarm-start
```

---

### Option 3: Install from source (uv)

#### 1. Environment setup

First complete the Python, Node.js, and Git checks for source installs in [Environment check](#environment-check).

Install **uv** first. If it is not installed, follow the [uv documentation](https://docs.astral.sh/uv/).

```bash
# Check that uv is installed
uv --version
# Expect: uv 0.x.x
```

#### 2. Clone and install

```bash
# Clone the repository
git clone https://gitcode.com/openJiuwen/jiuwenswarm.git

# Enter project directory
cd jiuwenswarm

# Create venv and install dependencies with uv
uv venv
uv pip install -e .
```

#### 3. Build the front end

> ⚠️ **Important:** With a source (editable) install you must build the front end manually, or startup will fail with `dist directory not found`. The build output stays at `jiuwenswarm/channels/web/frontend/dist` in the source tree, and the application reads it there directly; do not copy it to `~/.jiuwenswarm/`. The forward-slash paths below work in Windows PowerShell/Command Prompt and macOS/Linux shells.

```bash
# Windows / macOS / Linux: enter the front-end directory from the repository root
cd jiuwenswarm/channels/web/frontend

# Install front-end dependencies
npm install

# Build
npm run build

# Back to repo root
cd ../../../..
```

**Notes:**

- `uv pip install -e .` is an editable install that points at your source tree.
- `frontend/dist` is ignored by `.gitignore`, so the repository does not contain build output.
- `jiuwenswarm/channels/web/app_web.py` reads `frontend/dist` directly from the source tree.

#### 4. First launch

```bash
# Activate virtual environment
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# Initialize JiuwenSwarm (first run)
jiuwenswarm-init
# Start
jiuwenswarm-start
```

#### 5. Post-start verification (same as Option 2)

Use the checklist under [Post-start verification](#3-post-start-verification).

#### 6. Restarting the service

```bash
# After activating the virtual environment
jiuwenswarm-start
```

---

### Option 4: Install from source (conda)

#### 1. Environment setup

First complete the Python, Node.js, and Git checks for source installs in [Environment check](#environment-check).

Install **conda** first. If it is not installed, follow the [Miniconda documentation](https://docs.conda.io/en/latest/miniconda.html).

```bash
# Check that conda is installed
conda --version
# Expect: conda 23.x.x or newer
```

#### 2. Create the conda environment

```bash
# Create environment
conda create -n jiuwenswarm python=3.11

# Initialize conda (first time)
conda init
# After init, close the window and open a new session before activate

# Activate environment
conda activate jiuwenswarm
```

#### 3. Clone and install

```bash
# Clone the repository
git clone https://gitcode.com/openJiuwen/jiuwenswarm.git

# Enter project directory
cd jiuwenswarm

# Install dependencies
pip install -e .
```

#### 4. Build the front end

> ⚠️ **Important:** With a source (editable) install you must build the front end manually, or startup will fail with `dist directory not found`. The build output stays at `jiuwenswarm/channels/web/frontend/dist` in the source tree, and the application reads it there directly; do not copy it to `~/.jiuwenswarm/`. The forward-slash paths below work in Windows PowerShell/Command Prompt and macOS/Linux shells.

```bash
# Windows / macOS / Linux: enter the front-end directory from the repository root
cd jiuwenswarm/channels/web/frontend

# Install front-end dependencies
npm install

# Build
npm run build

# Back to repo root
cd ../../../..
```

**Notes:**

- `pip install -e .` is an editable install that points at your source tree.
- `frontend/dist` is ignored by `.gitignore`, so the repository does not contain build output.
- `jiuwenswarm/channels/web/app_web.py` reads `frontend/dist` directly from the source tree.

#### 5. First launch

```bash
# Initialize JiuwenSwarm (first run)
jiuwenswarm-init
# Start
jiuwenswarm-start
```

#### 6. Post-start verification (same as Option 2)

Use the checklist under [Post-start verification](#3-post-start-verification).

#### 7. Restarting the service

```bash
# Activate environment, then start
conda activate jiuwenswarm
jiuwenswarm-start
```

---

## Version upgrades

| Current range | Approach | Notes |
|---------------|----------|-------|
| Routine (e.g. 0.1.8 → 0.1.9, does not cross 0.1.7) | [Routine version upgrades](#routine-version-upgrades) | Upgrade directly; no backup required |
| Major (e.g. &lt;0.1.7 → &gt;0.1.7, crosses 0.1.7) | [Major version upgrades](#major-version-upgrades) | Back up data first |

---

### Routine version upgrades

#### pip install upgrade

```bash
# Activate your virtual environment
# Then upgrade
pip install --upgrade jiuwenswarm
```

#### Source install upgrade

```bash
# Enter project directory
cd jiuwenswarm

# Pull latest
git pull

# Reinstall
pip install -e .
```

If the front end changed, rebuild it. Keep the build output in the source tree; do not copy it to the user workspace. The forward-slash paths below work in Windows PowerShell/Command Prompt and macOS/Linux shells.

```bash
# Windows / macOS / Linux
cd jiuwenswarm/channels/web/frontend
npm install
npm run build
cd ../../../..
```

---

### Major version upgrades

> ⚠️ Always back up your data before upgrading across major versions.

#### 1. Data backup

**Windows:**

```bash
# Back up the whole config and data directory
xcopy "%USERPROFILE%\.jiuwenswarm" "%USERPROFILE%\.jiuwenswarm_backup" /E /I

# Or with PowerShell (recommended)
Copy-Item -Path "$env:USERPROFILE\.jiuwenswarm" -Destination "$env:USERPROFILE\.jiuwenswarm_backup" -Recurse
```

**macOS/Linux:**

```bash
# Back up the whole config and data directory
cp -r ~/.jiuwenswarm ~/.jiuwenswarm_backup

# Or with rsync (recommended; preserves permissions)
rsync -av ~/.jiuwenswarm ~/.jiuwenswarm_backup
```

**What to back up:**

| Path | Description |
|------|-------------|
| `config/config.yaml` | Main config (models, API keys, etc.) |
| `config/.env` | Environment variables |
| `agent/workspace/` | Identity, task, and workspace files |
| `agent/workspace/memory/` | User memory data |
| `agent/workspace/skills/` | Skills library (custom skills and config) |
| `agent/home/` | Scheduled task data (`cron_jobs.json`) |

> `agent/jiuwenclaw_workspace/`, `agent/memory/`, and `agent/skills/` are legacy locations. Current versions migrate their contents into `agent/workspace/`.

#### 2. Perform the upgrade

Pick the steps that match how you installed JiuwenSwarm:

##### pip install upgrade

Follow the same steps as [Routine version upgrade – pip install upgrade](#pip-install-upgrade) (activate your virtual environment, then run `pip install --upgrade jiuwenswarm`).

##### Source install upgrade

Follow the same steps as [Routine version upgrade – source install upgrade](#source-install-upgrade) (pull, reinstall, and rebuild the front end when needed).

#### 3. Data migration

After upgrading, migrate data so config and stores match the new version.

##### Step 1: Review config changes

```bash
# View the new config template (source install)
cat jiuwenswarm/resources/config.yaml

# Or read the changelog
# https://gitcode.com/openJiuwen/jiuwenswarm/blob/develop/docs/CHANGELOG.md
```

##### Step 2: Migrate configuration

1. **Compare old and new config shape**

   New releases may add or remove options. Check:
   - New required keys in `config.yaml`
   - New variables in `.env`
   - Deprecated or renamed keys

2. **Migrate by hand**

   ```bash
   # Back up the new default config
   cp ~/.jiuwenswarm/config/config.yaml ~/.jiuwenswarm/config/config.yaml.new

   # Restore from backup (use with care)
   # Prefer diff/merge in an editor instead of blind overwrite
   ```

3. **Common migration cases**

   | Case | What to do |
   |------|------------|
   | New model support | Add the new model block in `config.yaml` |
   | API endpoint change | Update URLs in `.env` |
   | Renamed keys | Map old → new using the changelog |
   | Removed keys | Delete obsolete entries |

##### Step 3: Migrate memory data

Memory is usually backward compatible; still verify:

```bash
# Inspect memory layout
ls ~/.jiuwenswarm/agent/workspace/memory/

# If something looks wrong, restore from backup
cp -r ~/.jiuwenswarm_backup/agent/workspace/memory/* ~/.jiuwenswarm/agent/workspace/memory/
```

##### Step 4: Verify migration

```bash
# Start the service
jiuwenswarm-start

# Watch logs for config errors
# Logs: ~/.jiuwenswarm/logs/
```

**Migration checklist:**

- [ ] Service starts cleanly
- [ ] Model config works (chat OK)
- [ ] Historical memory is readable
- [ ] Custom settings carried over
- [ ] No serious errors or warnings

---

## FAQ

### Q: On start I see "Python version not supported"

JiuwenSwarm requires Python **3.11, 3.12, or 3.13** (i.e. ≥3.11 and <3.14). 3.10 and earlier, as well as 3.14 and newer, are not supported. 3.11 and 3.12 have the best compatibility. See [Environment check](#environment-check) for details.

> ℹ️ If a page renders something like `Python >=3.11 ❤️ 3.14` or `Python 3.11 3.14`, that is the `<=` angle brackets being parsed as an HTML tag or triggering an emoji substitution. The full intent is `≥3.11, <3.14`.

### Q: Windows prompts "Do you want to allow this app to make changes to your device?"

This is expected. The Windows installer (`.exe`) runs with administrator privileges (`PrivilegesRequired=admin`) so it can write to system directories and register the uninstaller. Click "Yes" to continue.

### Q: The dmg I downloaded on macOS will not start, or says "damaged"

See [macOS: download the dmg with curl (recommended)](#1-macos-download-the-dmg-with-curl-recommended) above. A dmg downloaded via a browser carries a quarantine flag; use `curl` from a terminal instead. Also confirm you downloaded the build matching your Mac chip — see [Architecture note (macOS)](#architecture-note-macos).

### Q: During a source install I see "Node.js not found" or `npm` is unavailable

Install Node.js 18.x or newer, then rebuild the front end. The desktop installers and pip package already include the front-end static assets, so normal installation and startup do not require Node.js. See [Environment check](#environment-check) for details.

### Q: How do I check the installed version?

```bash
pip show jiuwenswarm
```

### Q: How do I uninstall?

```bash
pip uninstall jiuwenswarm
```

---

## Related links

- **Repository:** https://gitcode.com/openJiuwen/jiuwenswarm
- **Issues:** https://gitcode.com/openJiuwen/jiuwenswarm/issues
- **Docs:** https://gitcode.com/openJiuwen/jiuwenswarm/tree/develop/docs

---

*Last updated: 2026-08-18*
