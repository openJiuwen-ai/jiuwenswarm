# Quick Start

> **⚠️ Version Sync**: This document should be kept in sync with [`docs/zh/Quickstart.md`](../zh/Quickstart.md). When updating one, please update the other.

JiuwenClaw provides two installation methods: `pip install` or `install from source`.

## Prerequisites

- Download JiuwenClaw code:
  ```bash
  git clone https://gitcode.com/openjiuwen/jiuwenclaw.git
  ```
- Environment dependencies:
  - Python: >=3.11, <3.14
  - Node.js: >=18.0.0 (only needed for building frontend from source or for browser-use functionality; 20 LTS recommended)

**Note: Users can choose any of the following installation methods based on their needs.**

## Method 1: pip Install

Suitable for users who manage their own Python environment. Follow these steps:

- Create a virtual environment & install JiuwenClaw

  ```bash
  # Create a virtual environment named jiuwenclaw
  python -m venv jiuwenclaw

  # Activate the jiuwenclaw virtual environment
  jiuwenclaw\Scripts\activate

  # Install JiuwenClaw
  pip install jiuwenclaw
  ```

- Initialize & start JiuwenClaw

  ```bash
  # Initialize JiuwenClaw (first time setup)
  jiuwenclaw-init

  # Start JiuwenClaw
  jiuwenclaw-start
  ```

  After running, you can access the JiuwenClaw web UI. The default local access URL is `http://localhost:5173`.

  **Note:** For remote access, run the following commands:

  ```bash
  # Start web service
  jiuwenclaw-web --host 0.0.0.0 --port <custom-port>

  # Start backend service
  jiuwenclaw-app
  ```

## Method 2: Install from Source

Suitable for users who perform custom development or adaptation based on JiuwenClaw.

### uv Installation

- Create a virtual environment with `uv`
  ```bash
  # Create a virtual environment with uv (supports any of 3.11, 3.12, 3.13)
  uv venv --python=3.11
  # or: uv venv --python=3.12
  # or: uv venv --python=3.13
  ```

- Run uv sync

  Navigate to the project root directory `jiuwenclaw/` and run:
  ```bash
  uv sync
  ```

- Install frontend dependencies

  Navigate to the frontend directory `jiuwenclaw/web` and install dependencies:
  ```bash
  cd jiuwenclaw/web
  npm install
  ```

- Run frontend service

  Two methods are available for running the frontend service:

  - Static frontend service (suitable for production deployment)
    ```bash
    npm run build
    cd ../../
    uv run jiuwenclaw-init
    uv run jiuwenclaw-start
    ```

  - Dynamic frontend service (suitable for development and debugging)
    ```bash
    cd ../../
    uv run jiuwenclaw-init
    uv run jiuwenclaw-start dev
    ```

  After running, you can access the JiuwenClaw web UI.

### conda Installation

- Create a virtual environment with `conda`
  ```bash
  # Create a virtual environment with Anaconda (supports any of 3.11, 3.12, 3.13)
  conda create -n JiuwenClaw python=3.11
  # or: conda create -n JiuwenClaw python=3.12
  # or: conda create -n JiuwenClaw python=3.13
  ```

- Install Python dependencies

  Navigate to the project root directory `jiuwenclaw/` and run:
  ```bash
  # Mode 1: Development installation (recommended, facilitates code modification)
  pip install -e .

  # Mode 2: Regular installation
  pip install .
  ```
  **Note:** This installation method relies on the project's installable package (pyproject.toml) and will install `jiuwenclaw` itself by default.

- Install frontend dependencies

  Navigate to the frontend directory `jiuwenclaw/web` and install dependencies:
  ```bash
  cd jiuwenclaw/web
  npm install
  ```

- Run frontend service

  Two methods are available for running the frontend service:

  - Static frontend service (suitable for production deployment)
    ```bash
    npm run build
    cd ../../
    jiuwenclaw-init
    jiuwenclaw-start
    ```

  - Dynamic frontend service (suitable for development and debugging)
    ```bash
    cd ../../
    # Start directly (without using uv run)
    jiuwenclaw-init
    jiuwenclaw-start dev
    ```

  After running, you can access the JiuwenClaw web UI.
