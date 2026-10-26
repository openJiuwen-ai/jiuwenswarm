# 快速开始

> **⚠️ 版本同步**: 本文档应与英文版 [`docs/en/Quickstart_tui.md`](../en/Quickstart_tui.md) 保持同步。更新一版时请同时更新另一版。

JiuwenSwarm提供两种安装方式：`pip安装`或`源码安装`。

更完整的终端命令、Slash 指令与 Code 模式说明见 **[TUI 使用指南](TUI使用指南.md)**。

安装前准备：
- JiuwenSwarm代码下载
  ```bash
  git clone https://gitcode.com/openjiuwen/jiuwenswarm.git
  ```
- 环境依赖：
  - python：>=3.11，<3.14
  - nodejs：>=18.0.0（仅源码前端构建或 browser-use 功能需要，推荐 20 LTS）

**注意：用户可根据自己实际需要，基于以下任意一种方案安装。**

## **方式一：pip安装**  

​适合自行管理Python环境的用户。具体操作如下：
- 创建虚拟环境 & 安装 jiuwenswarm 项目

  ```bash
  # 创建名为 jiuwenswarm 的虚拟环境
  python -m venv jiuwenswarm

  # Windows 激活 jiuwenswarm 虚拟环境
  jiuwenswarm\Scripts\activate

  # Mac 激活 jiuwenswarm 虚拟环境
  source .venv/bin/activate

  # 安装 jiuwenswarm
  pip install jiuwenswarm

  # 安装 jiuwenswarm-tui
  pip install jiuwenswarm-tui
  ```

- 初始化 & 启动 jiuwenswarm 项目

  ```bash
  # 初始化 JiuwenSwarm (首次启动)
  jiuwenswarm-init

  # 启动 JiuwenSwarm
  jiuwenswarm-start
  ```

- 启动 jiuwenswarm-tui 项目
  ```bash
  # 另打开终端界面，启动 JiuwenSwarm-tui
  jiuwenswarm-tui
  ```

  可在**多个终端**重复执行上述命令，连接同一 Gateway（默认 `ws://127.0.0.1:19001/tui`），实现多窗口并行会话。详见 [TUI 使用指南 — 多窗口 TUI](TUI使用指南.md#多窗口-tui)。

## **方式二：源码运行** 

​适合基于JiuwenSwarm进行二次开发适配的用户。

### `uv`方式安装
- 使用`uv`新建虚拟环境
  ```bash
  # 使用uv新建虚拟环境（支持 3.11、3.12、3.13 任一版本）
  uv venv --python=3.11
  # 或 uv venv --python=3.12
  # 或 uv venv --python=3.13
  ```

- 激活 jiuwenswarm 虚拟环境
  ```bash
  # Windows 激活 jiuwenswarm 虚拟环境
  jiuwenswarm\Scripts\activate

  # Mac 激活 jiuwenswarm 虚拟环境
  source .venv/bin/activate
  ```

- 执行uv同步操作
  
  进入项目根目录`jiuwenswarm/`执行：
  ```bash
  uv sync
  ```

- 安装前端依赖

  进入前端目录 `channels/web/frontend` 安装依赖：
  ```bash
  cd channels/web/frontend
  npm install
  ```

- 运行前端服务

  可以采取两种方式运行前端服务：
  - 静态运行前端服务（适合生产环境部署）
    ```bash
    npm run build
    cd ../../../
    uv run jiuwenswarm-init
    uv run jiuwenswarm-start
    ```

  - 动态运行前端服务（适合开发调试）
    ```bash
    cd ../../../
    uv run jiuwenswarm-init
    uv run jiuwenswarm-start dev
    ```

  运行完成后即可在网页前端访问JiuwenSwarm服务。

- 安装TUI依赖
  另外打开新终端界面，进入项目根目录后，再进入 TUI 目录 `channels/tui/frontend` 安装依赖：
  ```bash
  cd channels/tui/frontend
  npm install
  ```

- 运行TUI

  ```bash
  npm run dev
  ```

  可在多个终端重复运行 TUI，连接同一 Gateway 实现多窗口并行会话（详见 [TUI 使用指南 — 多窗口 TUI](TUI使用指南.md#多窗口-tui)）。

### `conda`方式安装
- 使用`conda`新建虚拟环境
  ```bash
  # 使用Anaconda新建虚拟环境（支持 3.11、3.12、3.13 任一版本）
  conda create -n JiuwenSwarm python=3.11
  # 或 conda create -n JiuwenSwarm python=3.12
  # 或 conda create -n JiuwenSwarm python=3.13
  ```
- 安装python依赖
  
  进入项目根目录`jiuwenswarm/`执行：
  ```bash
  # 模式1：开发模式安装（推荐，便于修改代码）
  pip install -e .

  # 模式2：普通安装
  pip install .
  ```
  **注意：** 该安装方式依赖项目的可安装包（pyproject.toml），同时会默认安装`jiuwenswarm`自己。

- 安装前端依赖

  进入前端目录 `channels/web/frontend` 安装依赖：
  ```bash
  cd channels/web/frontend
  npm install
  ```

- 运行前端服务

  可以采取两种方式运行前端服务：
  - 静态运行前端服务（适合生产环境部署）
    ```bash
    npm run build
    cd ../../../
    jiuwenswarm-init
    jiuwenswarm-start
    ```

  - 动态运行前端服务（适合开发调试）
    ```bash
    cd ../../../
    # 直接启动（不使用 uv run）
    jiuwenswarm-init
    jiuwenswarm-start dev
    ```

  运行完成后即可在网页前端访问JiuwenSwarm服务。

- 安装TUI依赖
  另外打开新终端界面，进入项目根目录后，再进入 TUI 目录 `channels/tui/frontend` 安装依赖：
  ```bash
  cd channels/tui/frontend
  npm install
  ```

- 运行TUI

  ```bash
  npm run dev
  ```

  可在多个终端重复运行 TUI，连接同一 Gateway 实现多窗口并行会话（详见 [TUI 使用指南 — 多窗口 TUI](TUI使用指南.md#多窗口-tui)）。