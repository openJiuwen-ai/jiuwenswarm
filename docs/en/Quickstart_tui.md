# Quick Start

> **⚠️ Version Sync**: This document should be kept in sync with [`docs/zh/Quickstart_tui.md`](../zh/Quickstart_tui.md). When updating one, please update the other.

JiuwenSwarm provides two installation methods: `pip install` or `install from source`.

## Prerequisites

- Download JiuwenSwarm code:
  ```bash
  git clone https://gitcode.com/openjiuwen/jiuwenswarm.git
  ```
- Environment dependencies:
  - Python: >=3.11, <3.14
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

`--session <id>` makes the TUI connect with a specific session id and, after the connection is established, take one of two paths depending on whether the id already exists:

| id state | Startup behavior | Backend RPC |
|------|------|------|
| **exists** | Resume the session: triggers `session.switch` lifecycle (KV cache affinity / Team state migration), aligns the frontend mode to the backend-resolved mode, then fetches history and replays it in the UI | `session.switch` + `history.get` + `session.rename` (title) |
| **does not exist** | Create and persist the session: triggers `session.create` (creates the directory + `metadata.json` + lifecycle init); the UI starts as an empty session. Once persisted it can be resumed next time | `session.create` + `history.get` (empty) |

Detection is **try-create-then-switch**: first attempt `session.create(<id>)`; on `ALREADY_EXISTS`, fall back to `session.switch` to resume. Driven by `app-state.ts` `resumeOrCreateBootSession` after `connection.ack`, runs once (idempotent on reconnect).

**Examples**:

```bash
# First launch with an id → does not exist → created and persisted
jiuwenswarm-tui --session tui_myproj_001
# Chat a few turns in the TUI, then quit. The id is persisted to ~/.jiuwenswarm/agent/sessions/tui_myproj_001/

# Launch again with the same id → exists → resume and replay history
jiuwenswarm-tui --session tui_myproj_001
```

**Relationship with runtime `/resume`**: `--session` is the **startup-time** resume/create entry; `/resume` is the **runtime** command to switch to another session after the TUI is already running. Both invoke the same backend RPCs (`session.switch`/`session.create`), but `--session` fires on the handshake first frame and `/resume` fires on user input. No conflict in normal sequencing.

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

**Without `--session`**: the frontend generates a random id (`generateSessionId` → `tui_<hex>_<hex>`), and the backend directory is lazily created only when the **first message writes history**. So launching without `--session` and quitting before sending any message leaves no backend directory and the id does not appear in `/sessions` (empty sessions do not pollute the list). Launching with `--session` creates the directory immediately, so **even without sending a message the id appears in the list** for next-time resume — a key behavioral difference.

## Method 2: Install from Source

Suitable for users who perform custom development or adaptation based on JiuwenSwarm.

### uv Installation

- Create a virtual environment with `uv`
  ```bash
  # Create a virtual environment with uv (supports any of 3.11, 3.12, 3.13)
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
  # Create a virtual environment with Anaconda (supports any of 3.11, 3.12, 3.13)
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