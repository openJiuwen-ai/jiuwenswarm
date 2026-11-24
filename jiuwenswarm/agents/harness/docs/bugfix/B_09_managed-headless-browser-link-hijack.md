# 升套餐跳转被无头托管浏览器劫持，后续外链跳转全部无效

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-24 |
| 范围 | `common/tools/browser-move/src/playwright_runtime/`（drivers/managed_browser.py、service.py）、`common/browser_env.py` |
| 关联 spec | 无（托管浏览器运行时尚未归档 spec；本文件为该域首份 bugfix 归档） |
| 测试基线 | 本地 `.venv` `py_compile` 全过 + 针对性冒烟（identifier/版本键/架构排序/append 路径加载）+ dev 环境端到端验证 |

## 现象与证据

2026-11-18 会话 `officeclaw_c508fab74cecc035b34eec59`（req `57874845`）11:15:46 触发 web-dev 浏览器任务，11:17:40 拉起 `browser_agent` 子代理，按 `profiles.json`（managed + `--headless=new`，CDP 9333）启动系统 Google Chrome 无头实例；任务约 11:30 结束，**进程不回收**，无头 Chrome 存活至 15:57 被人工 kill（约 4h40m，常驻约 250MB）。期间用户点击"升级套餐"等外链均无任何浏览器窗口弹出；kill 残留进程后跳转立即恢复。

dev 环境复现（2026-11-23，源码启动 + `BROWSER_RUNTIME_MCP_ENABLED=1`）：frontend-dev 任务拉起 managed 无头 Chrome（PID 47049，系统 Chrome.app + 9333）；任务运行中点击"升级套餐"时，relay-claw 侧 Electron 外链兜底（`reclaimStaleManagedHeadlessChrome`）先杀掉 47049 再走系统浏览器，用户看到可见窗口——原始劫持被兜底拦截，但任务浏览器被误杀、服务被迫重启（新 PID 47574）；任务停止后 47574 残留占 9333，泄漏行为与事发一致。

附带证据：自动化启动系统 Chrome 触发 macOS 钥匙串弹窗（"找不到用于储存"Chrome"的钥匙串"）——无头模式"不打扰用户"的设计目标同样被破坏。

## 根因

四个既有设计在 macOS 组合后爆发，非单点故障：

1. `_candidate_chrome_binaries()` darwin 分支首选系统 `Google Chrome.app`，无随包候选——托管无头实例与用户默认浏览器**同属一个应用 Bundle**。
2. `config.yaml` `headless: true` 默认无头，页面加载不可见。
3. `BROWSER_MANAGED_CLOSE_AFTER_TASK` 默认 `False`，无空闲超时、无孤儿清理、无退出兜底——任务结束进程常驻。
4. 直接成因：外链经宿主壳层 `shell.openExternal`/`NSWorkspace.open` 交给 macOS LaunchServices，**按应用 Bundle 路由** http(s)——存活的无头实例截走所有外链，在不可见标签页打开。事发时用户个人 Chrome 未运行，无头实例为唯一存活 Chrome。

附带缺陷：`_build_managed_profile()` 的 `kill_existing` 分支会把 user-data-dir 指向**用户默认 Chrome 目录**，可能操作用户真实 profile（登录态/书签锁）。

## 决策

1. **随包浏览器优先（治本）**：新增 `_bundled_chromium_candidates()` 解析随包 Playwright Chromium / chrome-headless-shell（扫 `PLAYWRIGHT_BROWSERS_PATH` 与平台默认缓存，兼容新旧布局，构建号降序 + CPU 架构优先）；darwin 候选列表随包优先、系统 Chrome 仅兜底并打 warning；`browser_env.py` sync 在 darwin 且 chrome_path 为空或 `<auto>` 时解析随包路径写入 `BROWSER_MANAGED_BINARY`。独立 App Bundle 从根上消除 LaunchServices 劫持。
2. **生命周期回收**：桌面端（darwin/Windows）`BROWSER_MANAGED_CLOSE_AFTER_TASK` 默认 True；任务结束空闲超时（默认 300s，`BROWSER_MANAGED_IDLE_TIMEOUT` 可配）自动关闭，新任务到达自动取消；启动时按专用 user-data-dir 清孤儿进程及 singleton 锁；`atexit` + SIGTERM 退出兜底（宿主已注册 SIGTERM 处理器时让位不覆盖，保住 server 优雅停机链）。
3. **禁止指向用户默认目录**：删除 `kill_existing → _default_chrome_user_data_dir()` 分支，任何情况只用专用 `.browser-profiles/` 目录；复用持久化 profile 与孤儿清理前校验 `user_data_dir` 必须位于专用根（`_runtime_state_root()/.browser-profiles` 或显式 `BROWSER_MANAGED_USER_DATA_DIR`）之下——老版本 `kill_existing=1` 写入用户默认目录的存量脏 profile 会被重建覆盖，孤儿清理对其跳过并告警，绝不作用到用户真实 Chrome。
4. **一致性配套**（检视意见）：`_is_chrome_identifier` 扩展接受 `chromium`（旧布局 `Chromium.app` 走显式 binary 不再被拒）；driver 新增公开 `pid`/`is_running()` 消除跨类 `_process` 访问；宿主兜底导入路径 `sys.path` 改 append 不遮蔽宿主模块。

## 拒绝的方案

- **依赖宿主外链兜底**（relay-claw `reclaimStaleManagedHeadlessChrome` 每次外链前探测回收）：仅拦截 Electron 一条通道，Swift 壳及其它通道不设防；dev 复现实测会误杀运行中任务的浏览器；治标不治本，保留为保险不作为方案。
- **managed 模式改用 Playwright 库内 `p.chromium.launch()`**：现架构为 CDP attach 驱动独立进程，改库内启动牵动 runner 与失败恢复链路，超出本缺陷修复范围。
- **默认关闭 headless**：仅消除"不可见"，同 Bundle 劫持与泄漏均在，且弹窗打扰用户。

## 验证

dev 环境端到端（macOS 源码启动）：任务实际拉起 `ms-playwright/chromium-1223/.../Google Chrome for Testing`（不再是系统 Chrome.app）；任务运行中点击"升级套餐"默认浏览器正常弹窗跳转支付页；任务结束后无 headless 残留、9333 释放。冒烟：identifier 6 用例、版本键提取、架构排序、宿主无 `playwright_runtime` 时 append 路径加载——全过。
