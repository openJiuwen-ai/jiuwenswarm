# 快速开始

> **⚠️ 版本同步**: 本文档应与英文版 [`docs/en/Quickstart.md`](../en/Quickstart.md) 保持同步。更新一版时请同时更新另一版。

JiuwenClaw提供两种安装方式：`pip安装`或`源码安装`。

安装前准备：
- JiuwenClaw代码下载
  ```bash
  git clone https://gitcode.com/openjiuwen/jiuwenclaw.git
  ```
- 环境依赖：
  - python：>=3.11，<3.14
  - nodejs：>=18.0.0（仅源码前端构建或 browser-use 功能需要，推荐 20 LTS）

**注意：用户可根据自己实际需要，基于以下任意一种方案安装。**

## **方式一：pip安装**  

​适合自行管理Python环境的用户。具体操作如下：
- 创建虚拟环境 & 安装 jiuwenclaw 项目

  ```bash
  # 创建名为 Jiuwenclaw 的虚拟环境
  python -m venv jiuwenclaw

  # 激活 Jiuwenclaw 虚拟环境
  jiuwenclaw\Scripts\activate

  # 安装 Jiuwenclaw
  pip install jiuwenclaw
  ```

- 初始化 & 启动 jiuwenclaw 项目

  ```bash
  # 初始化 JiuwenClaw (首次启动)
  jiuwenclaw-init

  # 启动 JiuwenClaw
  jiuwenclaw-start
  ```

  运行完成后即可在网页前端访问JiuwenClaw服务，默认网页本地访问 `http://localhost:5173`。

  **注：** 如需远程访问可以执行入下命令：

  ``````
  # 启动web服务
  jiuwenclaw-web --host 0.0.0.0 --port 自定义端口

  # 启动后端服务
  jiuwenclaw-app
  ``````

## **方式二：源码运行** 

​适合基于JiuwenClaw进行二次开发适配的用户。

### `uv`方式安装
- 使用`uv`新建虚拟环境
  ```bash
  # 使用uv新建虚拟环境（支持 3.11、3.12、3.13 任一版本）
  uv venv --python=3.11
  # 或 uv venv --python=3.12
  # 或 uv venv --python=3.13
  ```

- 执行uv同步操作
  
  进入项目根目录`jiuwenclaw/`执行：
  ```bash
  uv sync
  ```

- 安装前端依赖

  进入前端目录 jiuwenclaw/web 安装依赖：
  ```bash
  cd jiuwenclaw/web
  npm install
  ```

- 运行前端服务

  可以采取两种方式运行前端服务：
  - 静态运行前端服务（适合生产环境部署）
    ```bash
    npm run build
    cd ../../
    uv run jiuwenclaw-init
    uv run jiuwenclaw-start
    ```

  - 动态运行前端服务（适合生开发调试）
    ```bash
    cd ../../
    uv run jiuwenclaw-init
    uv run jiuwenclaw-start dev
    ```

  运行完成后即可在网页前端访问JiuwenClaw服务。

### `conda`方式安装
- 使用`conda`新建虚拟环境
  ```bash
  # 使用Anaconda新建虚拟环境（支持 3.11、3.12、3.13 任一版本）
  conda create -n JiuwenClaw python=3.11
  # 或 conda create -n JiuwenClaw python=3.12
  # 或 conda create -n JiuwenClaw python=3.13
  ```
- 安装python依赖
  
  进入项目根目录`jiuwenclaw/`执行：
  ```bash
  # 模式1：开发模式安装（推荐，便于修改代码）
  pip install -e .

  # 模式2：普通安装
  pip install .
  ```
  **注意：** 该安装方式依赖项目的可安装包（pyproject.toml），同时会默认安装`jiuwenclaw`自己。

- 安装前端依赖

  进入前端目录 jiuwenclaw/web 安装依赖：
  ```bash
  cd jiuwenclaw/web
  npm install
  ```

- 运行前端服务

  可以采取两种方式运行前端服务：
  - 静态运行前端服务（适合生产环境部署）
    ```bash
    npm run build
    cd ../../
    jiuwenclaw-init
    jiuwenclaw-start
    ```

  - 动态运行前端服务（适合生开发调试）
    ```bash
    cd ../../
    # 直接启动（不使用 uv run）
    jiuwenclaw-init
    jiuwenclaw-start dev
    ```

  运行完成后即可在网页前端访问JiuwenClaw服务。
