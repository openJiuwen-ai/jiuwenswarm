# 桌面端打包：Electron 替换 Python（pywebview）方案

> 交接文档：目标是**用 Electron 打包替换 Python 打包**作为桌面端发布形态，二者运行时逻辑已对齐。
> 新会话阅读本文即可获得完整上下文，不需要翻聊天记录。

## 1. 总体架构

三个部分，Electron 打包只替换"壳"，后端与前端完全复用：

```
┌─────────────────────────────────────────────────────┐
│ Electron 壳（main.cjs / preload.cjs / launch.cjs）    │
│  - BrowserWindow：受信渲染器，加载 Web 前端            │
│  - WebContentsView：内置浏览器 sideview（Agent 用）    │
│  - 进程管理：spawn/关闭 web/agent/gateway 三个后端服务  │
├─────────────────────────────────────────────────────┤
│ Python 后端（PyInstaller 冻结，与 Python 打包同产物）  │
│  workswarm.exe --desktop-run-web / -agent / -gateway │
├─────────────────────────────────────────────────────┤
│ Web 前端（vite 构建，ELECTRON=true 时 base='./'）      │
│  新增 DesktopBrowserPane 组件 + window.jiuwenDesktop  │
└─────────────────────────────────────────────────────┘
```

- 代码位置：Electron 壳在 `jiuwenswarm/channels/desktop/electron/`
- 受信渲染器加载 `http://127.0.0.1:<frontend端口>`（web 服务静态托管 + API/WS 代理）
- FrontendOnly 构建例外：不打包后端，直接 `file://` 加载本地 dist，API 指向 `127.0.0.1:19000`

## 2. 端口模型（与 Python 桌面完全一致）

来源 `jiuwenswarm/instance_manager/config.py` 的 `BASE_PORTS`：

| 服务 | 基础端口 |
|---|---|
| agentServer | 18092 |
| gatewayApi（web/前端代理目标） | 19000 |
| gatewayInternal | 19001 |
| frontend（web 静态服务） | 5173 |

一个实例占用全部四个端口，实例 index 偏移 = index × 1000。
Electron 启动时 `findAvailablePorts()` 按组扫描（最多 20 组），整组可用才用。

## 3. Electron 打包（Windows）

入口：`scripts/build-electron-exe.ps1`（macOS 对应 `build-electron-exe.sh`，见 §9 遗留问题）。
快捷方式：`jiuwenswarm/channels/desktop/electron/package.json` 的 `electron:build*` 脚本。

三种构建模式：
- 正式：`.\scripts\build-electron-exe.ps1`
- 测试：`-Test`（写入 `.test` 标记 → 打包版开放 F12 DevTools）
- 前端测试：`-Test -FrontendOnly`（写入 `.frontend-only` 标记，不打包后端）

流程：

1. `uv sync --extra dev --extra claude --extra codex`（FrontendOnly 跳过）
2. 前端 `vite build`（`ELECTRON=true` → `base: './'`；`$env:ELECTRON` 用 try/finally 恢复避免污染 shell）
3. electron 目录 `npm install`（Electron runtime 来自 `node_modules/electron/dist`）
4. `build_config.py --sync --emit-json` 读取 **dist 目录名 / exe 名 / 版本号**（当前 `workswarm` / `workswarm.exe` / pyproject 版本，单一来源，不写死；`--sync` 对齐 build-exe.ps1，否则 pyproject 改版本后 spec 漂移守卫会硬失败）→ PyInstaller 打后端 → 冻结 exe 自检 A2UI bundle（同 build-exe.ps1）
5. 组装 `dist/JiuwenSwarm-Electron/`：
   ```
   JiuwenSwarm.exe            ← electron.exe 改名 + rcedit 换图标
   resources/
     app/                     ← Electron 壳代码
       main.cjs  preload.cjs  target_mcp_wrapper.cjs
       package.json           ← version 来自 build_config
       logo.ico  logo.svg
       dist/                   ← 前端产物（FrontendOnly 模式才被加载）
       node_modules/           ← 内置 @playwright/mcp 运行时（见 §5）
     backend/                  ← PyInstaller COLLECT 产物（workswarm.exe + _internal）
   ```
6. Inno Setup `scripts/installer-electron.iss`：版本与后端 exe 名经 `/DMyAppVersion=` / `/DBackendExecutableName=` 传入（`#ifndef` 兜底）；`PrivilegesRequired=lowest` per-user 安装、`AppMutex` 四互斥体（运行中拒绝安装/卸载，同 installer.iss）、卸载执行 `--desktop-reset-external-cli-config` 并清理 `{app}\resources`（FrontendOnly 无后端，跳过该步）

版本号硬编码位置已全部消除：ps1/iss/组装 package.json 均来自 `build_config.py`（即 pyproject.toml）。

## 4. Electron 运行时（main.cjs）

### 启动流程（顺序有意为之，有源码断言测试保护）

```
app.whenReady
  → createMainWindow：
     loading 页（data URL，不阻塞后续）＋ 立即 spawn web 服务
     ——冻结后端的多秒级 Python 启动与渲染进程/CDP 初始化重叠（首屏关键路径优化）
  → startBrowserTargetResolver：target resolver HTTP 服务（127.0.0.1 随机端口）
     ——GET /<sessionId> 惰性创建该会话 sideview（先 load about:blank#jiuwen-session=<sid>
     使 CDP target 立即注册，不等外网）→ getOrCreateDevToolsTargetId() →
     waitForCdpTarget() 轮询 /json/list 确认 → 后台加载必应（失败不影响启动）
  → startBackendServices：
       web 的 waitForHttp 就绪即 early navigation（前端自行重连后端）
       → prepareRuntimeWorkspace（一次性进程 --desktop-prepare-runtime-workspace，
         之后 agent/gateway 靠 JIUWENSWARM_RUNTIME_WORKSPACE_READY=1 跳过）
       → agent + gateway 并行（waitForTcp）→ watchBackendPair 成对退出监护
```

### CDP 端口

- dev：`launch.cjs` 先保留一个回环端口经 `JIUWENSWARM_ELECTRON_CDP_PORT` 传入（直接 `electron .` 会 throw，必须走 launch.cjs）
- packaged：`--remote-debugging-port=0` 让 Chromium 自选临时端口并自行绑定，选定端口从用户数据目录的 `DevToolsActivePort` 文件读回（启动时先删除陈旧文件）；文件 5s 未出现则禁用浏览器 sideview。相比旧的"同步 spawn 自身 exe 预留端口"方案（冷启动被杀软扫描拖到数秒且串行阻塞首屏），Chromium 自绑定既无竞态窗口也不占启动关键路径
- 开关：`--remote-debugging-address=127.0.0.1 --remote-debugging-port=<0 或 ephemeral>`

### 前端静态资源缓存（app_web.py）

打包后前端由冻结 web 进程提供，静态响应头直接影响二次启动：Vite 哈希资产
（`assets/<name>-<hash>.<ext>`，内容变更即换名）返回
`Cache-Control: public, max-age=31536000, immutable`，Chromium 磁盘缓存直接
复用、免条件请求往返；index.html 与 SPA fallback 返回 `no-cache` 回源验证，
保证升级后拿到新的哈希引用。测试见
`tests/unit_tests/channels/web/test_web_static_cache_headers.py`。

### 子进程环境契约（对齐 Python 桌面 `desktop_app._build_child_env`）

```
JIUWENSWARM_DESKTOP=1  JIUWENSWARM_ELECTRON=1
WEB_PORT=<gatewayApi>  GATEWAY_PORT=<gatewayInternal>
AGENT_SERVER_PORT=AGENT_PORT=<agentServer>  FRONTEND_PORT=<frontend>
JIUWENSWARM_RUNTIME_WORKSPACE_READY=1
删除 AGENT_SERVER_URL（防 .env 里的陈旧 URL 绕过重映射端口）
```

Python 侧配合（均已实现并有测试）：
- `jiuwenswarm/dotenv_early.py`：`JIUWENSWARM_ELECTRON=1` 时额外保留 `DESKTOP_BROWSER_PRESERVED_ENV_KEYS`（BROWSER_DRIVER / PLAYWRIGHT_MCP_* 等），dotenv 不覆盖 Electron 注入的精确 target 绑定
- `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`：检测到 `PLAYWRIGHT_MCP_TARGET_ID` 时走 Electron 分支，不还原 headless 参数、不改写 JSON argv

### 服务管理

- Windows 关闭：`taskkill /pid <pid> /t /f`（强杀进程树）——与 Python 桌面的 `_psutil_terminate`（`TerminateProcess`）行为**对齐**，不是缺陷
- POSIX：`detached` 进程组，SIGTERM → 5s → SIGKILL
- 单实例锁：`app.requestSingleInstanceLock()`，二次启动聚焦已有窗口
- 日志：packaged 模式各服务写 `~/.jiuwenswarm/logs/electron-<name>.log`；**主进程 console 同步 tee 到 `electron-main.log`**（双击启动无控制台，主进程日志——含启动失败完整原因——otherwise 全部丢失；对应 Python 桌面的 desktop.log）
- 启动失败行为（对齐 Python 桌面）：`showStartupFailure` 在**窗口内渲染失败页**（错误详情可选中复制 + 日志路径 + 退出按钮 + 首启杀软提示），残余服务树立即清理但**应用不自动退出**，由用户自行退出；仅当窗口不可用/失败页加载失败时回退原生错误框 + 退出
- 成对退出：agent/gateway 任一退出立即终止另一个（防止端口/单例锁泄漏），与 Python 桌面同逻辑
- 应用内更新（Windows 打包版）：spawn `workswarm.exe --desktop-install-update`（detached，不持 AppMutex），由后端 helper 等主进程退出 + 端口释放后再启安装器——与 Python 桌面 `_launch_windows_install_helper` 同契约；dev / FrontendOnly / helper 拉起失败时回退 `shell.openPath` 直开安装器

## 5. 内置浏览器（Electron 核心增量）

### 双视图模型

| | 受信渲染器（主窗口） | sideview（WebContentsView） |
|---|---|---|
| 加载 | Web 前端 | 外网页面（默认必应中国） |
| session | 默认 | `persist:jiuwenswarm-browser-<sessionId>`（**每会话独立 partition**，登录态/历史/cookie 按会话隔离；上限 8 个，回收最久未用。回收时记录最后页面 URL，重开会话自动还原；URL 持久化到 `userData/browser-session-urls.json`——防抖 2s 落盘 + before-quit 冲刷 + 启动读回，**App 重启后同样还原页面**） |
| 安全 | sandbox + contextIsolation + preload | sandbox + contextIsolation + 无 preload + 权限全拒 |

sideview 弹窗（window.open/target=_blank）一律 deny 并导航回自身；渲染器崩溃自动 reload（上限 5 次，did-navigate 清零）。

### Agent 控制链路（安全边界）

```
Electron main.cjs
  └─ spawnService 注入 env：
       BROWSER_DRIVER=remote  BROWSER_SHARED_CONTROL=1
       PLAYWRIGHT_MCP_CDP_ENDPOINT=http://127.0.0.1:<cdp端口>
       PLAYWRIGHT_MCP_TARGET_RESOLVER=http://127.0.0.1:<resolver端口>
       PLAYWRIGHT_MCP_ENV_JSON=<env 白名单，openjiuwen 只转发这里的键>
       PLAYWRIGHT_MCP_COMMAND / PLAYWRIGHT_MCP_ARGS
Python 后端（interface_deep electron 分支透传；
       code_subagents.build_swarm_browser_agent 经 electron_sideview 按
       会话 GET /<sessionId> 解析 TargetID 并注入该成员 MCP env）
  └─ openjiuwen BrowserAgent 按 config.json 消费：
        command = PLAYWRIGHT_MCP_COMMAND
        args    = PLAYWRIGHT_MCP_ARGS（JSON 数组）
        env     = PLAYWRIGHT_MCP_ENV_JSON（白名单合并）+ 注入的 PLAYWRIGHT_MCP_TARGET_ID
target_mcp_wrapper.cjs（子进程）
  └─ chromium.connectOverCDP(endpoint)
  └─ findTargetPage(targetId)：只找精确 TargetID（本会话 sideview），绝不按列表序/URL 选择
  └─ guardedContext：Proxy 封装，禁 newPage/newContext/close、吞 page 事件
  └─ 屏蔽工具：browser_close / browser_install / browser_run_code(_unsafe) / browser_tabs
```

**打包版不依赖用户机器的 Node.js**：
- `PLAYWRIGHT_MCP_COMMAND = process.execPath`（JiuwenSwarm.exe 自身），args 只含 wrapper 路径
- `ELECTRON_RUN_AS_NODE=1` 经 `PLAYWRIGHT_MCP_ENV_JSON` 白名单转发（这是唯一通道，openjiuwen 不透传父进程 env）
- `@playwright/mcp@0.0.78` 由构建脚本装进 `resources/app/node_modules`，wrapper 的 `require.resolve` 同级命中；版本 pin 在 main.cjs 的 `PLAYWRIGHT_MCP_PACKAGE`，ps1 从该行正则解析（不双写）
- dev 模式仍走 `npx -y --package @playwright/mcp@0.0.78 node <wrapper>`

### CDP 风险声明（已接受）

CDP 端点仅回环但**无认证**，本机任意进程理论上可连接并控制暴露的 WebContents（含受信渲染器）。Playwright 需要HTTP endpoint，无法根治；exact-target wrapper 只是 agent 路径的执行边界。已记录在 electron README。

### 前端集成

- preload 暴露 `window.jiuwenDesktop`（browser.* / desktop.* IPC，全部 `trustedSender` 校验），同时保留 `window.pywebview.api` 兼容层
- `DesktopBrowserPane`（`src/components/DesktopBrowserPane/`）：工具栏 + 地址栏 + 视口；`ResizeObserver` + `zoom-changed` 重算 bounds，主进程按 `zoomFactor` 换算 CSS px → DIP 后 setBounds
- **模态遮挡规避**：原生 WebContentsView 永远盖在页面 DOM 之上（z-index 无效）；pane 监听到 `role="dialog"[aria-modal="true"]` 弹窗挂载时临时隐藏视图，关闭后恢复显示（`browser:set-visible` 的 focus=false 不抢主窗口焦点）
- **tab 显示条件**（重要）：`isElectron && (useBrowserAgentActivity(sessionId) || useDesktopBrowserTabFlags(sessionId).requested) && !closed`
  - 单 agent 模式：subagentStore 中出现 `subagent_type === 'browser_agent'` 的子代理（spawn 即算，含历史恢复）
  - team 模式：`teamMemberExecutionEvents` 中出现过 `browser_*` 工具调用（team 的浏览器子代理挂在成员内部，不进主会话 subagentStore）
  - 或本会话点开过 .html/.md 聊天文件（`openFileInDesktopBrowser`，按会话持久化于 localStorage）；页签右上 close.svg 显式关闭后隐藏（`closeDesktopBrowserTab`）
  - 信号实现在 `src/features/browserAgentActivity.ts` 与 `src/features/desktopBrowserFile.ts`；web 端短路，走原逻辑
- **每会话隔离**：browser.* IPC 全部携带 sessionId，主进程按会话维护独立 WebContentsView（独立 partition）；状态广播带 sessionId，pane 只响应当前会话
- 自动展开：每会话仅一次（`App.tsx` 的 ref 门控），用户手动收起后不再强制弹回；team 模式不抢 tab
- tab 状态持久化：`singleAgentPanelState.ts` 与 `teamPanelStateNormalize.ts` 的合法 tab 集合均已含 `'browser'`
- Electron 下前端跳过 IAM 鉴权探测（`AppWithAuth` 直接 `noIam`）
- 地址栏导航非法 URL → 主进程 reject → 前端 catch 后恢复地址栏

## 6. Python 打包现状（被替换方，仍可用）

入口：`scripts/build-exe.ps1`（macOS 为 build-macos.sh），spec 为 `scripts/jiuwenswarm.spec`。

```
build_config.py --sync（同步 _build_config.py 防漂移）
 → uv sync --extra dev
 → 前端 vite build
 → PyInstaller：双 exe（workswarm + openjiuwen-team-mcp）+ COLLECT
    （collect openjiuwen 全子模块、symphony、resources、前端 dist、
      pytest/mypy/ruff/chromadb 等；console=False、uac_admin=False）
 → 冻结 exe 自检 A2UI bundle
 → 捆绑 Node 运行时到 dist/<name>/runtime/node-runtime（供 browser 工具 npx）
 → Inno Setup scripts/installer.iss（per-user {localappdata}\Programs，
   /D 传版本，含旧管理员安装迁移逻辑）
```

运行时入口 `scripts/jiuwenswarm_exe_entry.py`：无参数 → 单实例锁 + `desktop_app.main()`（pywebview 窗口）；或分发 `--desktop-run-web/agent/gateway`、`--desktop-install-external-cli` 等子命令。
`desktop_app.py`：pywebview(EdgeChromium) 窗口、start_services 三服务、成对退出监护、workspace 一次准备、`_psutil_terminate`/`os.killpg` 关闭。

### 与 Electron 的对齐表

| 维度 | Python 桌面 | Electron | 状态 |
|---|---|---|---|
| 窗口 | pywebview(EdgeChromium) | BrowserWindow + preload | 替换 |
| 三服务启动 | desktop_app.start_services | main.cjs spawnService（同 flags） | 对齐 |
| workspace 准备一次 | launcher 内 | `--desktop-prepare-runtime-workspace` 一次性进程 | 对齐 |
| 端口组 | BASE_PORTS + instance×1000 | findAvailablePorts 同规则 | 对齐 |
| 成对退出 | watcher | watchBackendPair | 对齐 |
| 关闭策略 | psutil TerminateProcess / killpg | taskkill /t /f / kill(-pid) | 对齐（均强杀） |
| 子进程 env 契约 | `_build_child_env` | spawnService env | 对齐 |
| 单实例锁 | exe entry | requestSingleInstanceLock | 对齐 |
| 应用内更新（Win） | update-helper 等父进程退出+端口释放 | workswarm.exe --desktop-install-update helper | 对齐 |
| Node 运行时 | dist/runtime/node-runtime（npx） | resources/app/node_modules（内置 MCP，不再需要 npx） | 改进 |
| 浏览器 Agent | managed Chrome / 用户浏览器 | Electron sideview + CDP 精确 target | 替换 |
| 安装器 | installer.iss per-user + AppMutex + 卸载清理 | installer-electron.iss 同款（AppId 不同，可共存） | 对齐 |
| 版本来源 | build_config | build_config（/D 传入） | 对齐 |

## 7. 启动性能与排障（实测档案）

### 实测基线（2026-09，Win11 + Defender，暖机 = 文件已进 OS 缓存）

| 场景 | 前端页面加载完 | 后端全就绪（agent+gateway TCP） |
|---|---|---|
| 打包版·冷启动（装后首启，AV 扫描新文件） | ~2.7s | ~14s |
| 打包版·暖启动 | **~1.0s** | ~7.3s |
| dev `npm run dev` | ~8s（vite 按需 transform 占 ~5.5s，打包版无此项） | ~9.5s |

### 时间花在哪（关键认知，防止误判优化方向）

- **PyInstaller bootloader + 解释器仅 ~26ms**——PyInstaller 本身不是瓶颈
- 冻结 web 服务启动 ~750ms ≈ 模块导入 + main()。**历史包袱已清除**：`jiuwenswarm/gateway/__init__.py` 曾在包初始化时急切导入整个 gateway 运行时（1674 个模块 / ~1.4s），而 web 静态服务只需要 `gateway.routing.agent_http_bridge` 里一个纯 stdlib 的函数，却被迫付全款。已改为 **PEP 562 惰性导出**（`from jiuwenswarm.gateway import ChannelManager` 等用法完全兼容），效果：app_web 导入 1476ms→45ms、冻结 web 启动 1693ms→750ms。**不变量（有测试保护）：不要往 `gateway/__init__.py` 加回急切导入**；该修复惠及 Electron/Python 桌面/Web 部署全部形态
- 静态资源本身不构成瓶颈：index.html 首字节 4ms、3.65MB 主 bundle 传输 17ms（回环）。优化"加载 dist"没有意义，见 §4 缓存策略（管的是二次启动的往返次数）
- agent/gateway 各自完整导入运行时（暖机各 ~2-3s，冷启 ×2-3）——这部分不可消除，它们真需要全部模块
- 冷暖差 ≈ 杀软扫描新文件的代价；根治手段是代码签名（未做，发布流程事项）
- Electron 侧探测轮询 100ms（`waitForTcp`/`waitForHttp`，对齐 Python 桌面的 0.1s sleep）

### 构建实操（踩坑记录）

- **直连 PyPI 会卡死构建**：claude/codex extras 含 134MB `openai-codex-cli-bin` + 77MB `claude-agent-sdk`，直连下载实测 45 分钟未完成。国内环境用环境变量 `UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/` + `npm_config_registry=https://registry.npmmirror.com`（子进程整树继承），同样的下载 <1 分钟
- 构建全程 ~10 分钟（PyInstaller ~7 分钟是大头）；建议用 detached 后台进程跑 + 轮询日志，避免被交互式工具超时杀掉
- 静默安装（`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-`）完成后 Inno `[Run]` postinstall **会自动启动应用**（与 Python 安装器一致）。自动化测量时注意：你的第二个实例会被单实例锁 0.2s 拒绝（干净退出属正常）
- 安装路径 `%LOCALAPPDATA%\Programs\JiuwenSwarm`，卸载跑 `unins000.exe`

### 排障手册

| 要看什么 | 位置 |
|---|---|
| **主进程日志**（启动失败的完整原因/退出码/堆栈） | `~/.jiuwenswarm/logs/electron-main.log` |
| 各后端服务日志 | `~/.jiuwenswarm/logs/electron-{web,agent,gateway,workspace-prepare}.log` |
| Python 子进程未捕获异常 | `~/.jiuwenswarm/logs/workswarm_exe_error.log` |
| 启动计时 | spawn exe + 逐行时间戳 stdout；关键日志行：`web ready, navigating early`（首屏起点）、`services ready`（后端就绪） |

### 测试套件已知问题（Windows 本机）

- `tests/unit_tests/channels/web/test_web_file_download.py`、`tests/unit_tests/test_app_web_raw_file.py`：collection error（`os.getuid`，Windows 不兼容，**既有问题**）
- `tests/unit_tests/gateway/test_app_web_handlers.py` 的 optional-dependency 用例：**会真实 spawn `uv pip install openjiuwen[claude]` 并持有 venv 锁**，把后续所有 `uv run` 挂死（测试缺陷；恢复 = kill 该 uv 进程）。跑 gateway 测试建议 `--ignore` 该文件
- 既有失败（已用 stash 对比证实与 Electron/惰性导入改动无关）：`test_harmonyos_dev`（缺 devecocli）、`test_upload_storage` 一例、`test_trajectory_frontend_artifacts`

## 8. 开发与验证

```powershell
# Electron 开发（vite HMR + uv 起 Python 服务）
cd jiuwenswarm/channels/desktop/electron
npm install
npm run dev            # = node launch.cjs --vite-dev
npm run dev:build      # 先 build:web 再起

npm run check          # 四个 cjs 的 node --check

# 前端
cd jiuwenswarm/channels/web/frontend
npx tsc --noEmit
npm run test:team-panel-state

# Python 侧契约测试
uv run pytest tests/unit/test_electron_browser_target.py -q      # argv/env 透传 + 启动顺序源码断言
uv run pytest tests/unit_tests/test_desktop_port_resolve.py -q   # dotenv 保留键
uv run pytest tests/unit/deep_agent/test_browser_subagent_integration.py -q  # interface_deep electron 分支
```

注意：`npm run smoke` 及 main.cjs 内的 smoke 断言代码已整体删除（原类名断言 `electron-browser-open` 与前端实际 `electron-desktop` 不匹配，从未跑通过）。启动顺序不变量由 `test_electron_waits_for_navigated_cdp_target_before_starting_services` 的源码断言保护。

## 9. 已知遗留（按优先级）

1. **macOS 构建**（`build-electron-exe.sh`）：未签名/公证。DMG 版本号、`CFBundleIdentifier`、`uv sync` extras 已接 build_config（与 ps1 对齐，含 `--sync` 与 A2UI 自检）。应用内更新在 macOS 仍走 `shell.openPath` 手动安装（Python 桌面有完整的 DMG 自动换壳 helper 脚本，未移植）。
2. **CDP 无认证**：已声明为已接受风险（§5）。
3. **Windows 强杀关闭**：与 Python 桌面一致；gateway 单例锁需依赖 PID 存活检查自愈。
4. **发布验证**：内置 MCP 运行时（exe 以 ELECTRON_RUN_AS_NODE 跑 wrapper）尚未在真实打包版上跑过端到端浏览器 Agent 任务，发布前务必实测；README（electron 目录）末尾有完整手工验证矩阵。同样需实测：应用内更新走 `--desktop-install-update` helper（§4）。已实测通过的新机制：`DevToolsActivePort` 端口回读（真实打包版多轮）、启动性能基线（§7）、启动失败页与主进程日志落盘（真实打包版验证）。
5. **Gateway 单例预检未移植**：Python 桌面 `start_services` 先 `_preflight_gateway_singleton()`（等 15s 旧 gateway 释放锁）；Electron 无此预检，依赖 gateway 自身单例锁 + 成对退出兜底（Electron 自带 requestSingleInstanceLock，双开场景已大幅收窄）。
6. **启动失败诊断体系部分对齐**：失败已改为**窗口内失败页**（详情/日志路径/退出按钮，应用不自动退出，见 §4）+ 主进程 console 落盘 `electron-main.log`；但 startup diagnostics 目录 / doctor 流程 / loading 状态轮询页仍未移植（子进程拿不到 `STARTUP_DIAGNOSTICS_DIR` env），失败原因的可诊断深度仍弱于 Python 桌面，属已知的可接受取舍。
7. **安装后首次启动的瞬时子进程崩溃**：实测新装包首启曾出现后端子进程无输出原生崩溃（零 traceback、零 WER，符合杀软扫描新文件 + 冷缓存竞争特征；同 binary 二启正常）。失败页会呈现原因，用户重启即恢复。发布前把"全新机器首启"列入验证矩阵。

## 10. 关键文件索引

| 文件 | 职责 |
|---|---|
| `jiuwenswarm/channels/desktop/electron/main.cjs` | Electron 主进程：窗口、服务、CDP、IPC |
| `jiuwenswarm/channels/desktop/electron/launch.cjs` | dev 启动器：保留 CDP 端口、优雅关闭转发 |
| `jiuwenswarm/channels/desktop/electron/preload.cjs` | contextBridge：jiuwenDesktop + pywebview 兼容层 |
| `jiuwenswarm/channels/desktop/electron/target_mcp_wrapper.cjs` | 精确 TargetID 的 Playwright MCP 适配器 |
| `jiuwenswarm/channels/desktop/electron/README.md` | 英文版设计说明 + 验证矩阵 + 风险声明 |
| `scripts/build-electron-exe.ps1` / `.sh` | Electron 打包（Win/macOS） |
| `scripts/installer-electron.iss` | Electron Inno 安装器 |
| `scripts/build-exe.ps1` / `jiuwenswarm.spec` / `installer.iss` | Python 打包（被替换方） |
| `scripts/build_config.py` | 版本/产物名单一来源（`--emit-json` / `--emit-shell`） |
| `scripts/jiuwenswarm_exe_entry.py` | 冻结 exe 分发入口（两套打包共用） |
| `jiuwenswarm/channels/desktop/desktop_app.py` | Python 桌面运行时（pywebview，对齐参照物） |
| `jiuwenswarm/gateway/__init__.py` | PEP 562 惰性导出（启动性能关键不变量，见 §7；勿加回急切导入，有测试保护） |
| `tests/unit_tests/gateway/test_gateway_lazy_exports.py` | gateway 惰性导出 + app_web 轻导入不变量 |
| `tests/unit_tests/channels/web/test_web_static_cache_headers.py` | 静态资源 Cache-Control 头行为 |
| `jiuwenswarm/dotenv_early.py` | env 保留键（Electron browser 键集） |
| `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py` | `_sync_browser_runtime_environment` electron 分支 |
| `src/components/DesktopBrowserPane/` | 前端浏览器面板 |
| `src/features/browserAgentActivity.ts` | "浏览器 Agent 已调用"信号 hook |
| `src/features/singleAgentPanelState.ts` / `teamPanelStateNormalize.ts` | tab 状态（含 'browser'） |
| `src/utils/env.ts` | apiBase/wsBase（Electron FrontendOnly 走 jiuwenDesktop） |
| `src/types/electron.ts` | jiuwenDesktop 类型 |
