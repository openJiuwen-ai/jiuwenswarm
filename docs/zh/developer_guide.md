# 开发者指南

本文档面向 JiuwenSwarm 项目的开发者，介绍如何从源码搭建开发环境并运行测试。

## 环境要求

| 依赖项 | 版本要求 | 说明 |
|--------|----------|------|
| 操作系统 | Windows 10/11, macOS 10.15+, Linux | 支持主流操作系统 |
| Python | ≥3.11, <3.14 | 推荐 Python 3.11 |
| Git | 最新版本 | 用于克隆源码 |
| Node.js | ≥18.x | 用于前端界面 |
| Bun | 最新版本 | 用于编译 TUI 前端包 |

## 1. 克隆项目

```bash
git clone <repository-url> jiuwenswarm
cd jiuwenswarm
```

## 2. 使用 `uv` 搭建开发环境

### 安装 `uv` 方式安装

如果尚未安装 `uv`，请先安装：

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 创建虚拟环境

使用 `uv` 新建虚拟环境（支持 3.11、3.12、3.13 任一版本）：

```bash
uv venv --python=3.11
```

### 激活环境

```bash
# macOS / Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

### 执行 uv 同步操作

在项目根目录 `jiuwenswarm/` 执行：

```bash
uv sync
```

该命令会根据 `pyproject.toml` 和 `uv.lock` 安装所有依赖，包括开发依赖（如 `pytest`、`pytest-cov`、`pytest-asyncio` 等）。

## 3. 安装 Bun（编译 TUI 前端包）

TUI 前端包需要使用 [Bun](https://bun.sh/) 进行编译。安装方式：

```bash
# macOS、Linux 和 WSL
curl -fsSL https://bun.sh/install | bash

# Windows（通过 PowerShell）
powershell -c "irm bun.sh/install.ps1 | iex"

# 也可通过 npm 安装
npm install -g bun
```

安装完成后，验证：

```bash
bun --version
```

## 4. 开发

完成环境搭建后，即可开始源码及测试用例的开发。

### 项目结构概览

```
jiuwenswarm/
├── jiuwenswarm/           # 项目源码
├── tests/
│   ├── unit_tests/       # 单元测试
│   ├── system_tests/     # 系统测试
│   └── ...
├── docs/                 # 文档
├── pyproject.toml        # 项目配置与依赖声明
├── uv.lock               # 依赖锁定文件
└── pytest.ini            # pytest 配置
```

### 添加依赖

- **运行时依赖**：`uv add <package>`
- **开发依赖**（测试、lint 等工具）：`uv add --dev <package>`

例如：

```bash
uv add --dev pytest-cov pytest-asyncio
```

### 自验证开发流程

修改代码后，需要根据修改的内容类型进行对应的构建和验证：

#### 后端代码修改

修改后端 Python 代码后，执行以下命令重新初始化并启动服务：

```bash
uv run jiuwenswarm-init
uv run jiuwenswarm-start
```

#### 前端代码修改

修改前端代码后，需要重新构建前端资源：

```bash
npm run build
```

#### TUI 修改

修改 TUI（终端用户界面）相关代码后，运行开发模式进行调试：

```bash
npm run dev
```

## 5. 验证测试用例

> **重要**：务必使用 `uv run pytest` 而非直接运行 `pytest`，以确保使用 `uv` 管理的虚拟环境中的依赖。

### 运行全部单元测试

```bash
uv run pytest tests/unit_tests/
```

### 运行全部系统测试

```bash
uv run pytest tests/system_tests/
```

### 运行指定测试文件

```bash
uv run pytest tests/unit_tests/agentserver/test_team_config_loader.py
```

### 常见问题排查

**问题：`ModuleNotFoundError: No module named 'xxx'`**

原因：直接运行了系统的 `pytest`，未使用 `uv` 环境。解决方案：

```bash
# 错误写法 - 使用了系统 Python
pytest tests/

# 正确写法 - 通过 uv 运行
uv run pytest tests/
```

**问题：`error: unrecognized arguments: --cov=...`**

原因：`pytest.ini` 中配置了 `--cov` 参数，但当前环境未安装 `pytest-cov`。解决方案：

```bash
uv add --dev pytest-cov pytest-asyncio
```

**问题：测试间状态泄漏导致莫名失败**

如果某个测试单独运行通过但在全量运行时失败，通常是因为某个测试文件在模块级别修改了 `sys.modules` 或全局状态，影响了后续测试的收集或执行。排查时可使用二分法定位：

```bash
# 单独运行通过
uv run pytest tests/unit_tests/xxx/test_foo.py

```

**问题：如何安装 Node.js**

前端开发需要 Node.js 环境。推荐通过 [nvm](https://github.com/nvm-sh/nvm)（Node Version Manager）安装：

```bash
# 安装 nvm（macOS / Linux）
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash

# 重新加载 shell 配置
source ~/.bashrc

# 安装并使用 Node.js 18
nvm install 18
nvm use 18
```

Windows 用户可使用 [nvm-windows](https://github.com/coreybutler/nvm-windows) 或直接从 [Node.js 官网](https://nodejs.org/) 下载安装。

安装完成后，验证版本：

```bash
node --version
# 预期输出：v18.x.x 或更高
```

## 6. 编译包

项目支持两种分发形式：**Python wheel 包**（`.whl`）和**桌面可执行文件**（`.exe` / `.app`）。

### 6.1 wheel 包构建

项目包含两个独立的 wheel 包：

| 包名 | 说明 | 配置文件 |
|------|------|----------|
| `jiuwenswarm` | 后端服务主包（含 Web 前端构建产物） | `pyproject.toml` |
| `jiuwenswarm-tui` | TUI 终端界面 sidecar 包（含 Bun 编译的原生二进制） | `packages/jiuwenswarm-tui/pyproject.toml` |

#### 6.1.1 一键构建全部（推荐）

在项目根目录执行：

```bash
# macOS / Linux
bash scripts/build.sh
```

该脚本会依次执行：
1. 编译 Web 前端（`jiuwenswarm/channels/web/frontend` 目录下执行 `npm run build`）
2. 构建主包 `jiuwenswarm.whl`
3. 如果检测到 `bun` 命令，继续构建 TUI 原生二进制和 `jiuwenswarm-tui.whl`

产物输出到两个目录：
- `./dist/jiuwenswarm-<version>-py3-none-any.whl`（主包）
- `./packages/jiuwenswarm-tui/dist/jiuwenswarm_tui-<version>-<platform>.whl`（TUI sidecar 包）

#### 6.1.2 单独构建 jiuwenbox 包

`jiuwenbox` 是独立的沙箱系统包（配置文件 `jiuwenbox/pyproject.toml`），可通过 `scripts/build_python_packages.py` 跳过主包和 TUI sidecar，仅构建 jiuwenbox wheel：

```bash
python scripts/build_python_packages.py --skip-root --skip-sidecar --clean
```

该命令会：

1. 清理 `jiuwenbox/` 下的 `dist/`、`build/`、`jiuwenbox.egg-info`（因 `--clean` 触发）
2. 在 `jiuwenbox/` 目录下执行 `uv build --wheel`，产物输出到 `jiuwenbox/dist/`

产物：`./jiuwenbox/dist/jiuwenbox-<version>-py3-none-any.whl`

> 说明：`build_python_packages.py` 默认会构建主包、TUI sidecar 和 jiuwenbox 三者。仅当需要单独出 jiuwenbox 包时，才用 `--skip-root --skip-sidecar` 跳过另外两者。若三者全部跳过（同时传 `--skip-root --skip-sidecar --skip-jiuwenbox`），脚本会报错退出。

> 注意：`jiuwenbox` 要求 Python `>=3.11`。若构建机器的系统 Python 低于该版本，请显式指定 3.11 解释器（例如 `python3.11 scripts/build_python_packages.py --skip-root --skip-sidecar --clean`），否则构建会因依赖解析失败而报错。

### 6.2 桌面版 EXE / DMG 打包

桌面版通过 [PyInstaller](https://pyinstaller.org/) 将 Python 应用打包为独立可执行文件。

#### 6.2.1 前置条件

- 已安装 `uv`、`Node.js`
- 通过 `uv sync --extra dev` 安装开发依赖（包含 PyInstaller）

#### 6.2.2 Windows 平台打包（EXE）

**使用批处理脚本（推荐）：**

```cmd
scripts\build-exe.bat
```

**使用 PowerShell 脚本：**

```powershell
.\scripts\build-exe.ps1
```

产物目录：`dist\jiuwenswarm\jiuwenswarm.exe`

#### 6.2.3 macOS 平台打包（DMG）

```bash
bash scripts/build-macos.sh
```

该脚本会依次执行：
1. 安装 Python 依赖（`uv sync --extra dev`）
2. 编译 Web 前端（`npm run build`）
3. PyInstaller 打包生成 `JiuwenSwarm.app`
4. 使用 `hdiutil` 创建 DMG 安装镜像

产物：`dist/JiuwenSwarm-<version>.dmg`
