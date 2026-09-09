# Quick Start

> **⚠️ Version Sync**: This document should be kept in sync with [`docs/zh/Quickstart.md`](../zh/Quickstart.md). When updating one, please update the other.

## Installation

### Prerequisites

Before installing JiuwenSwarm, ensure your system meets the following requirements:

| Dependency | Version | Description |
|------------|---------|-------------|
| Operating System | Windows 10/11, macOS 10.15+, Linux | Supports mainstream operating systems |
| Python | `≥3.11, <3.14` | Python 3.11 recommended |
| Node.js | 18.x or higher | For frontend interface |
| Git | Latest | For source code installation |

Check Node.js version:

```bash
node --version
# Expected output: v18.x.x or higher
```

### pip Install

```bash
# Create virtual environment
python -m venv jiuwenswarm

# Activate virtual environment
# Windows:
jiuwenswarm\Scripts\activate
# Linux/Mac:
source jiuwenswarm/bin/activate

# Install JiuwenSwarm
pip install jiuwenswarm
```

## Start Service

```bash
# Initialize (first run)
jiuwenswarm-init

# Start service
jiuwenswarm-start
```

After successful startup, the terminal will display a service port banner:

```
[INFO] ================================================================
[INFO]   Service started. Port information:
[INFO]   ✓ Web UI                http://localhost:5173
[INFO]   ✓ AgentServer WebSocket  ws://localhost:18092
[INFO]   ✓ Gateway HTTP           http://localhost:19001
[INFO]   ✓ WebChannel WebSocket   ws://localhost:19000/ws
[INFO] ================================================================
```

When you see similar output, the service is ready. Open `http://localhost:5173` in your browser to use.

### Remote Web UI Access on Linux (Optional)

By default, `jiuwenswarm-start` binds the Web UI to `localhost`, so you can open the Web UI address shown in the terminal from a browser on the same Linux host. To access the Web UI on a Linux server from another computer, start it with:

```bash
FRONTEND_HOST=0.0.0.0 jiuwenswarm-start
```

Then open `http://<linux-server-ip>:<web-ui-port>` from the other computer. The default Web UI port is `5173`; if startup automatically selects another port, use the Web UI port shown in the terminal. Make sure the Linux firewall or cloud security group allows that port, and restrict access to trusted networks or source IPs.

### Automatic Port Conflict Resolution

JiuWenSwarm uses a fixed set of default ports (`18092 / 19000 / 19001 / 5173`). If a port is already in use when starting (e.g. a previous run did not fully stop, or another app occupies it), the launcher **automatically scans upward for the next free port group** and uses it instead of failing:

```
[start_services] ⚠️  Original ports conflict. Falling back to alternative port group (index 1):
[start_services] Using ports:
  agent_server: 19092
  web: 20000
  gateway: 20001
  frontend: 6173
[start_services] TUI/CLI connect with:
  jiuwenswarm-tui --url ws://127.0.0.1:20001/tui
  jiuwenswarm chat   (auto-reads GATEWAY_PORT)
```

The chosen ports are **persisted**, so the next launch and the TUI/CLI read them automatically — no need to memorize. If no free group is found within the scan range, the command prints conflict-resolution hints and exits.

To **force specific ports**, two options:

1. **Stop the occupying process**: `jiuwenswarm-start --stop default` (or `lsof -i :19001` on Linux/macOS; on Windows `netstat -ano | findstr :19001` then `taskkill /PID <pid> /F`).
2. **Override base ports via env vars** (Docker / container-friendly):

   ```bash
   # Override base ports (index-0 starting ports; named instances add index×1000)
   export JIUWENSWARM_GATEWAY_PORT=29001
   export JIUWENSWARM_AGENT_SERVER_PORT=28092
   export JIUWENSWARM_FRONTEND_PORT=15173
   export JIUWENSWARM_WEB_PORT=29000
   jiuwenswarm-start
   ```

### Terminal CLI

You can also chat with JiuwenSwarm directly from the terminal:

```bash
jiuwenswarm chat "Hello, introduce yourself"
```

For details, see [CLI / Terminal Chat](CLI.md#terminal-cli-jiuwenswarm-chat).

**Configuration Directory Auto-Creation**:
After first starting the service, the system automatically creates the configuration directory:
- **Windows**: `C:\Users\<your-username>\.jiuwenswarm`
- **Linux/Mac**: `~/.jiuwenswarm/`

Configuration files, memory files, and other data will be stored in this directory.

The following installation methods are suitable for users who want to perform secondary development and adaptation based on JiuwenSwarm.

First, clone the repository and enter the project directory:

```bash
git clone https://gitcode.com/openJiuwen/jiuwenswarm.git
cd jiuwenswarm
```

### Install via `uv`
- Create a new virtual environment using `uv`
  ```bash
  # Create virtual environment with uv (supports any of Python 3.11, 3.12, 3.13)
  uv venv --python=3.11
  # or uv venv --python=3.12
  # or uv venv --python=3.13
  ```

- Run uv sync

  Enter the project root directory `jiuwenswarm/` and run:
  ```bash
  uv sync
  ```

- Install frontend dependencies

  Enter the frontend directory `channels/web/frontend` to install dependencies:
  ```bash
  cd channels/web/frontend
  npm install
  ```

- Run frontend service

  Two ways to run the frontend service:
  - Static run (suitable for production deployment)
    ```bash
    npm run build
    cd ../../../
    uv run jiuwenswarm-init
    uv run jiuwenswarm-start
    ```

  - Dynamic run (suitable for development debugging)
    ```bash
    cd ../../../
    uv run jiuwenswarm-init
    uv run jiuwenswarm-start dev
    ```

  After running, you can access the JiuwenSwarm service via the web frontend.

### Install via `conda`
- Create a new virtual environment using `conda`
  ```bash
  # Create virtual environment with Anaconda (supports any of Python 3.11, 3.12, 3.13)
  conda create -n JiuwenSwarm python=3.11
  # or conda create -n JiuwenSwarm python=3.12
  # or conda create -n JiuwenSwarm python=3.13
  ```
- Activate the conda environment
  ```bash
  conda activate JiuwenSwarm
  ```
- Install Python dependencies

  Enter the project root directory `jiuwenswarm/` and run:
  ```bash
  # Mode 1: Development mode installation (recommended, easier to modify code)
  pip install -e .

  # Mode 2: Normal installation
  pip install .
  ```
  **Note:** This installation method depends on the project's installable package (`pyproject.toml`), and will also install `jiuwenswarm` itself by default.

- Install frontend dependencies

  Navigate to the frontend directory `channels/web/frontend` and install dependencies:
  ```bash
  cd channels/web/frontend
  npm install
  ```

- Run frontend service

  Two ways to run the frontend service:
  - Static run (suitable for production deployment)
    ```bash
    npm run build
    cd ../../../
    jiuwenswarm-init
    jiuwenswarm-start
    ```

  - Dynamic run (suitable for development debugging)
    ```bash
    cd ../../../
    # Start directly (without uv run)
    jiuwenswarm-init
    jiuwenswarm-start dev
    ```

  After running, you can access the JiuwenSwarm service via the web frontend.

---

## Quick Start

### 1️⃣ Conversation Modes

| Method | Description |
|------|------|
| **Web Frontend** | After starting the service, visit `http://localhost:5173` to chat directly via browser |
| **Xiaoyi Channel** | Huawei phone users can directly wake up Xiaoyi to chat with JiuwenSwarm |
| **Feishu Channel** | After completing channel configuration, chat with JiuwenSwarm in Feishu |

### 2️⃣ Configure Model

See the [Configure Model](#configure-model) section below.

## Configure Model

In the left sidebar of the Web UI, find "Configuration" and enter the configuration page:

![](../assets/images/jiuwenswarm_configuration_Info.png)

Complete the following basic configuration, then click "Save" in the top right:

![](../assets/images/jiuwenswarm_config_api.png)

**Configuration Items:**

| Field | Environment variable | Description | Required |
|--------|------------------------|-------------|----------|
| `model_name` | `MODEL_NAME` | Model name, e.g., `deepseek-chat`, `gpt-4o` | ✅ Required |
| `api_base` | `API_BASE` | Model API base URL, e.g., `https://api.deepseek.com` | ✅ Required |
| `api_key` | `API_KEY` | Model API key | ✅ Required |
| `model_provider` | `MODEL_PROVIDER` | Model provider, e.g., `OpenAI`, `DeepSeek`, `DashScope`, `SiliconFlow`, `InferenceAffinity`, `OpenRouter`, `OpenAIAccount` | ✅ Required |

**Test After Configuration:**

After filling in the configuration, click the "Test" button to verify model availability. A successful test shows ✅, if failed check:
- Whether API Key is correct
- Whether API Base URL is accessible
- Whether model name and Provider match

**Notes:**

- **Auto hot-reload after save**: After saving, the system will hot-reload the configuration; most configuration items take effect immediately, while a few changes may trigger a process restart
- **Required fields**: The four fields above are basic configuration required for normal operation
- **Model Providers**: `OpenAI`, `DeepSeek`, `DashScope`, `SiliconFlow`, `AscendAffinity`, `OpenRouter`, `OpenAIAccount`

## Start Conversation

In the left sidebar of the Web UI, find "Chat" and enter your question to start:

![](../assets/images/jiuwenswarm_example.png)

## Session Management

Click the "+" button below to clear the current session and start a new one:

![](../assets/images/jiuwenswarm_new_session.png)

Page display after clearing:

![](../assets/images/jiuwenswarm_clear_session.png)

**When to clear a session?**

| Scenario | Description |
|----------|-------------|
| **Topic Switch** | Current conversation is complete, want to start a completely new topic |
| **Context Confusion** | Too much content in current session, model understanding deviates |
| **Repeated/Wrong Response** | Model falls into loop response or gives irrelevant answers |
| **Privacy/Sensitive Info** | Current session contains temporary sensitive information that needs immediate clearing |

**Comparison: Clear vs Not Clear Session:**

| Comparison | Not Clear (Continue Session) | Clear (New Session) |
|------------|------------------------------|---------------------|
| **Context Retention** | ✅ Keep all history, model knows full context | ❌ No history retained, model starts from scratch |
| **Token Consumption** | ⚠️ Grows with conversation, consumes more tokens | ✅ Initial tokens minimal, cost controllable |
| **Answer Relevance** | Early topics may interfere with current understanding | Each question processed independently, no interference |
| **Privacy Security** | History persists in current session | Sensitive info not carried to new session |

## Clear Memory

When you need JiuwenSwarm to forget all conversation history and user information, you can clear memory files.

> **⚠️ Risk Warning:** Clearing memory is **permanent**, deleted memory files **cannot be recovered**. Before proceeding, confirm:
> - Whether important memories are backed up
> - Whether you really need to delete (or just want to start a new session)

**Difference from Session Clearing:**

| Operation | Scope | Impact | Use Case |
|-----------|-------|--------|----------|
| **New Session** | Current chat window | Does not delete any memory, only starts new thread | Want to switch topics but keep historical memory for reference |
| **Clear Memory** | All memory files | Permanently deletes all history, user info, project memory | Completely clear all history, protect privacy, or reset to initial state |

**Use Cases:**
- **Privacy Protection**: Clear history containing sensitive information
- **Fresh Start**: Start a completely different project or topic, avoid historical interference
- **Debug Troubleshooting**: Reset when memory files are corrupted or content is abnormal
- **User Switching**: Clear previous user info in multi-user environments

**Steps to Clear Memory:**

The default built-in memory directory is:
- **Windows**: `C:\Users\<your-username>\.jiuwenswarm\agent\workspace\memory\`
- **Linux/Mac**: `~/.jiuwenswarm/agent/workspace/memory/`

The full default path to the long-term memory file `MEMORY.md` is:
- **Windows**: `C:\Users\<your-username>\.jiuwenswarm\agent\workspace\memory\MEMORY.md`
- **Linux/Mac**: `~/.jiuwenswarm/agent/workspace/memory/MEMORY.md`

**Method 1: Delete via Agent**
Tell JiuwenSwarm: "Please delete all memory files" or "Clear my memory", Agent will call file tools to delete files in the memory directory.
![](../assets/images/jiuwenswarm_delete_memory.png)

**Method 2: Manual Delete**
Stop JiuwenSwarm service, then directly delete all Markdown files in the `memory/` directory.
![](../assets/images/jiuwenswarm_memory.png)

> ⚠️ **Note**: Memory cannot be recovered after clearing, proceed with caution. Regularly backup important memory files.
