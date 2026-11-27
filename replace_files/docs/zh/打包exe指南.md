# JiuwenClaw 桌面打包指南

本文档说明如何使用 uv + PyInstaller + pywebview 将 JiuwenClaw 打包成桌面应用。当前支持 Windows `onedir` 分发目录和 macOS `.app + .dmg`。

## 前置要求

- **uv**：项目使用的 Python 包管理器
- **Node.js**：仅用于**构建时**编译前端，最终桌面程序不依赖 Node.js
- **Windows**：支持 `onedir` 分发目录，适合继续交给 Inno Setup 制作安装包
- **macOS**：支持生成 `.app` 与 `.dmg`

## 打包相关文件位置

打包配置与入口脚本统一放在 `scripts/` 目录，便于维护：

| 文件 | 说明 |
|------|------|
| `scripts/jiuwenclaw.spec` | PyInstaller 打包配置 |
| `scripts/jiuwenclaw_exe_entry.py` | exe 入口脚本（桌面模式 + 子命令分发） |
| `jiuwenclaw/desktop_app.py` | pywebview 桌面窗口与本地服务编排 |
| `scripts/build-exe.ps1` | 一键打包脚本（PowerShell） |
| `scripts/build-exe.bat` | 一键打包脚本（批处理） |
| `scripts/build-macos.sh` | macOS `.app + .dmg` 构建脚本 |

## Windows 打包步骤

### 方式一：使用脚本（推荐）

在项目根目录执行：

```powershell
# PowerShell
.\scripts\build-exe.ps1
```

或双击运行 `scripts\build-exe.bat`。

脚本会自动完成：安装依赖 → 构建前端 → 执行 PyInstaller 打包。

### 方式二：手动执行

#### 1. 安装 uv 和依赖

```bash
# 若未安装 uv
# Windows (PowerShell): irm https://astral.sh/uv/install.ps1 | iex

# 进入项目目录
cd e:\Projects\jiuwenclaw_9980

# 安装项目依赖（含 PyInstaller 开发依赖）
uv sync --extra dev --upgrade-package openjiuwen
```

其中 `openjiuwen` 会按 `pyproject.toml` 中的配置，从 `agent-core` 的 `develop` 分支同步，不需要再把 `openjiuwen/` 源码目录放进联调仓。

#### 2. 构建前端

前端为 React 应用，需先构建为静态文件，打包进 exe：

```bash
cd jiuwenclaw/web
npm install
npm run build
cd ../..
```

构建完成后，`jiuwenclaw/web/dist` 下会有静态文件。

#### 3. 执行打包

```bash
uv run pyinstaller scripts/jiuwenclaw.spec
```

成功后，桌面版位于 `dist/jiuwenclaw/`，主程序为 `dist/jiuwenclaw/jiuwenclaw.exe`。

## 使用打包后的 Windows 桌面版

### 首次使用

1. **初始化工作区**（首次必须执行）：
   ```bash
   jiuwenclaw.exe init
   ```
   会在 `~/.jiuwenclaw` 创建配置和工作区。

2. **编辑配置**：
   - 打开 `%USERPROFILE%\.jiuwenclaw\.env`
   - 填写 `API_KEY`、`MODEL_PROVIDER` 等

3. **启动应用**：
   ```bash
   jiuwenclaw.exe
   ```

4. 应用会启动本地后端与静态前端，并由 pywebview 直接打开无边框桌面窗口；默认不需要再手动打开浏览器。

## 给 Inno Setup 的产物约定

- 安装源目录使用整个 `dist/jiuwenclaw/`
- 主程序入口使用 `dist/jiuwenclaw/jiuwenclaw.exe`
- 首次初始化可由安装完成页触发 `jiuwenclaw.exe init`
- 用户配置与运行数据位于 `%USERPROFILE%\.jiuwenclaw`，卸载时通常不建议默认删除
- 若后续增加应用图标，请同时给 `scripts/jiuwenclaw.spec` 和 Inno Setup 脚本引用同一份 `.ico`

### 子命令

| 命令 | 说明 |
|------|------|
| `jiuwenclaw.exe` | 启动桌面应用 |
| `jiuwenclaw.exe init` | 初始化工作区（首次使用） |

## macOS 打包步骤

在 macOS 机器上执行：

```bash
chmod +x scripts/build-macos.sh
./scripts/build-macos.sh
```

脚本会自动完成：安装依赖 → 构建前端 → 使用 PyInstaller 生成 `JiuwenClaw.app` → 生成 `JiuwenClaw-0.1.7.dmg`。

生成后的产物：

- `dist/JiuwenClaw.app`
- `dist/JiuwenClaw-0.1.7.dmg`

验证方式：

1. 双击 `dist/JiuwenClaw.app`
2. 或挂载 `dist/JiuwenClaw-0.1.7.dmg`
3. 将 `JiuwenClaw.app` 拖到 `Applications`

注意事项：

- 当前 `.app` 未做 `codesign` / notarization，仅适合本机验证或内部测试
- 首次打开可能需要在 Finder 中右键选择“打开”绕过 Gatekeeper
- 如果后续要正式分发，建议补 `.icns`、签名和公证流程

## 技术说明

- **Python 运行时**：PyInstaller 将 Python 解释器及依赖打包进桌面分发目录，目标机器无需安装 Python。
- **桌面窗口**：pywebview 负责加载本地 `http://127.0.0.1:5173` 页面，并提供无边框窗口壳。
- **Node.js**：前端在构建阶段用 Node 编译，运行时只使用静态文件。
- **工作区路径**：与 pip 安装一致，使用 `~/.jiuwenclaw` 作为配置与工作区根目录。
- **安装包制作**：后续使用 Inno Setup 时，请将整个 `dist/jiuwenclaw/` 目录作为安装源，而不是只取单个 exe。
- **macOS DMG**：当前脚本会在 DMG 中附带 `Applications` 快捷方式，方便拖拽安装。

## 常见问题

### 1. 打包失败：找不到 web/dist

先执行 `cd jiuwenclaw/web && npm run build`，确保 `jiuwenclaw/web/dist` 存在。

### 2. 运行 exe 报错 ModuleNotFoundError

在 `scripts/jiuwenclaw.spec` 的 `hiddenimports` 中补充缺失模块，然后重新打包。

### 3. exe 体积过大

当前已经使用 `onedir` 模式，便于桌面应用拉起子进程并方便 Inno Setup 安装。若仍需继续缩减体积，可在 `scripts/jiuwenclaw.spec` 的 `excludes` 中排除未用模块。

### 4. 杀毒软件误报

PyInstaller 生成的 exe 可能被误报，可尝试：
- 添加排除规则
- 使用代码签名（若有证书）
