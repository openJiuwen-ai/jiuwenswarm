# Quick Start

> **⚠️ Version Sync**: This document should be kept in sync with [`docs/zh/Quickstart_tui.md`](../zh/Quickstart_tui.md). When updating one, please update the other.

JiuwenSwarm provides two installation methods: `pip install` or `install from source`.

## Prerequisites

- Download JiuwenSwarm code:
  ```bash
  git clone https://gitcode.com/openjiuwen/jiuwenswarm.git
  ```
- Environment dependencies:
  - Python: >=3.11
  - Node.js: >=18.0.0 (only needed for building frontend from source or for browser-use functionality; 20 LTS recommended)

**Note: Users can choose any of the following installation methods based on their needs.**

## Method 1: pip Install

Suitable for users who manage their own Python environment. Follow these steps:

- Create a virtual environment & install JiuwenSwarm

  ```bash
  # Create a virtual environment named jiuwenswarm
  python -m venv jiuwenswarm

  # Activate the jiuwenswarm virtual environment on Windows
  jiuwenswarm\Scripts\activate

  # Activate the jiuwenswarm virtual environment on Mac
  source .venv/bin/activate

  # Install JiuwenSwarm
  pip install jiuwenswarm

  # Install JiuwenSwarm-tui
  pip install jiuwenswarm-tui
  ```

- Initialize & start JiuwenSwarm

  ```bash
  # Initialize JiuwenSwarm (first time setup)
  jiuwenswarm-init

  # Start JiuwenSwarm
  jiuwenswarm-start
  ```

- start JiuwenSwarm-tui

  ```bash
  # Start JiuwenSwarm
  jiuwenswarm-tui
  ```

  You can run the command above in **multiple terminals** against the same Gateway (default `ws://127.0.0.1:19001/tui`) for parallel sessions in separate TUI windows. See the **Multi-window TUI** section in [TUI Usage Guide (zh)](../zh/TUI使用指南.md#多窗口-tui).

### `--session`: resume or create a session by id

`--session <id>` makes the TUI connect with a specific session id. After the connection is established, the TUI registers that id with AgentServer. This compatibility path deliberately bypasses the prewarm pool because the runtime identity was supplied externally.

| id state | Startup behavior | Backend RPC |
|------|------|------|
| **exists** | AgentServer preserves the persisted project/mode binding, runs the switch lifecycle, and the TUI replays history | explicit-ID `session.create` + `history.get` + `session.rename` (title) |
| **does not exist** | AgentServer validates the id, resolves the TUI project under a per-id lock, writes `metadata.json`, and starts with empty history without claiming a warm slot | explicit-ID `session.create` + `history.get` (empty) |

The explicit-ID form of `session.create` is TUI-only and idempotent. AgentServer logs that this compatibility request bypasses prewarming. It is released by `app-state.ts` `initializeBootSession` after `connection.ack` and runs once on reconnect. Ordinary startup, `/new`, and `/clear` keep a draft until the first message; their eventual `session.create` omits `session_id` so AgentServer allocates one.

**Examples**:

```bash
# First launch with an id → does not exist → created and persisted
jiuwenswarm-tui --session tui_myproj_001
# Chat a few turns in the TUI, then quit. The id is persisted to ~/.jiuwenswarm/agent/sessions/tui_myproj_001/

# Launch again with the same id → exists → resume and replay history
jiuwenswarm-tui --session tui_myproj_001
```

**Relationship with runtime `/resume`**: `--session` is the **startup-time** external-id compatibility entry and uses explicit-ID `session.create`; `/resume` is the **runtime** command for switching to an already persisted session and uses `session.switch`.

**id naming constraints** (validated by the frontend before startup; invalid ids exit immediately without entering the TUI):

| Constraint | Value | Reason |
|------|------|------|
| Max length | ≤ 128 chars | `session_id` is used directly as a directory name on disk (`~/.jiuwenswarm/agent/sessions/<id>/`); bounded by the filesystem's 255-char single-name limit, leaving headroom for the path prefix |
| Allowed chars | `A-Z a-z 0-9 . _ -` | Same charset as `generateSessionId` output (`tui_<hex>_<hex>`) |
| Forbidden chars | CJK / Unicode letters, spaces, `/ \ : * ? " < > |`, etc. | Prevents directory injection (`/` creates nested dirs → lost session) and cross-platform `mkdir` failures |

Invalid examples:

```bash
jiuwenswarm-tui --session 测试会话        # CJK chars → exits: --session <id> contains invalid characters
jiuwenswarm-tui --session "my session"    # space → same
jiuwenswarm-tui --session a/b             # slash → same
jiuwenswarm-tui --session "$(printf 'a%.0s' {1..200})"  # over 128 → length limit
```

**Without `--session`**: an ordinary startup opens an in-memory draft. No session directory is created until the first message. Reopening the TUI without sending a message leaves no empty session behind. `--session <id>` resumes or creates the specified ID immediately. `--persist-session` without `--session` creates a Persist Session immediately.

## Method 2: Install from Source

Suitable for users who perform custom development or adaptation based on JiuwenSwarm.

### uv Installation

- Create a virtual environment with `uv`
  ```bash
  # Create a virtual environment with uv (supports any of 3.11, 3.12, 3.13, 3.14)
  uv venv --python=3.11
  # or: uv venv --python=3.12
  # or: uv venv --python=3.13
  ```

- 激活 jiuwenswarm 虚拟环境
  ```bash
  # Activate the jiuwenswarm virtual environment on Windows
  jiuwenswarm\Scripts\activate

  # Activate the jiuwenswarm virtual environment on Mac
  source .venv/bin/activate
  ```

- Run uv sync

  Navigate to the project root directory `jiuwenswarm/` and run:
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

  Two methods are available for running the frontend service:

  - Static frontend service (suitable for production deployment)
    ```bash
    npm run build
    cd ../../../
    uv run jiuwenswarm-init
    uv run jiuwenswarm-start
    ```

  - Dynamic frontend service (suitable for development and debugging)
    ```bash
    cd ../../../
    uv run jiuwenswarm-init
    uv run jiuwenswarm-start dev
    ```

  After running, you can access the JiuwenSwarm web UI.

- Install TUI dependencies
  Open one new terminal, navigate to the project root, then enter the TUI directory `channels/tui/frontend` and install dependencies:
  ```bash
  cd channels/tui/frontend
  npm install
  ```

- Start TUI

  ```bash
  npm run dev
  ```

### conda Installation

- Create a virtual environment with `conda`
  ```bash
  # Create a virtual environment with Anaconda (supports any of 3.11, 3.12, 3.13, 3.14)
  conda create -n JiuwenSwarm python=3.11
  # or: conda create -n JiuwenSwarm python=3.12
  # or: conda create -n JiuwenSwarm python=3.13
  ```

- Install Python dependencies

  Navigate to the project root directory `jiuwenswarm/` and run:
  ```bash
  # Mode 1: Development installation (recommended, facilitates code modification)
  pip install -e .

  # Mode 2: Regular installation
  pip install .
  ```
  **Note:** This installation method relies on the project's installable package (pyproject.toml) and will install `jiuwenswarm` itself by default.

- Install frontend dependencies

  Navigate to the frontend directory `channels/web/frontend` and install dependencies:
  ```bash
  cd channels/web/frontend
  npm install
  ```

- Run frontend service

  Two methods are available for running the frontend service:

  - Static frontend service (suitable for production deployment)
    ```bash
    npm run build
    cd ../../../
    jiuwenswarm-init
    jiuwenswarm-start
    ```

  - Dynamic frontend service (suitable for development and debugging)
    ```bash
    cd ../../../
    # Start directly (without using uv run)
    jiuwenswarm-init
    jiuwenswarm-start dev
    ```

  After running, you can access the JiuwenSwarm web UI.

- Install TUI dependencies
  Open one new terminal, navigate to the project root, then enter the TUI directory `channels/tui/frontend` and install dependencies:
  ```bash
  cd channels/tui/frontend
  npm install
  ```

- Start TUI

  ```bash
  npm run dev
  ```
