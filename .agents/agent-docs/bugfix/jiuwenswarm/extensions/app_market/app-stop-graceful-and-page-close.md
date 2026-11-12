---
id: B_20261105_0941_app-stop-graceful-and-page-close
title: 应用市场停止强杀跳过应用清理与页面关闭无感知
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/extensions/app_market
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/extensions/app_market/app_market/static_server.py
  - jiuwenswarm/extensions/app_market/app_market/status_manager.py
  - jiuwenswarm/extensions/app_market/app_market/market_service.py
  - jiuwenswarm/extensions/app_market/app_market/process_supervisor.py
  - jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx
designs: []
design_note: 修复依据既有契约（app-builder SKILL.md 的"应用负责优雅退出和会话清理、市场只终止进程"与扩展 README 的运行描述），无独立 D；新增契约已固化为 specs/jiuwenswarm/extensions/app_market/S_02_app-market-stop-and-page-lifecycle.md。
---

# 应用市场停止强杀跳过应用清理与页面关闭无感知修复记录

## 现象与影响

用户反馈两个现象（2026-11-05，Windows 桌面端，应用"常晴镇 · Sunnyville"，自带 `scripts/app_server.py` 的 AI 应用）：

1. 在应用市场点击停止后，应用"没有真正关闭"：应用的 5 个 NPC 角色会话（`常晴镇·阿澄` 等）仍留在会话列表，需要重启宿主才消失。
2. 直接关闭应用页面（桌面 browser 页签）后，应用市场无任何状态变化，卡片持续显示"运行中"。

影响：应用侧按技能契约实现的会话清理永不执行，产生孤儿角色会话；页面与实例生命周期脱钩，用户关闭页面后进程与端口持续占用。

## 关联设计与当前契约

无独立 D。依据：app-builder SKILL.md 第"会话与请求规范"节——"应用负责优雅退出和会话清理，市场只终止进程；在停止脚本或进程退出处理里中断、删除并确认"；扩展 README 的运行描述。修复后契约固化于 [S_02_app-market-stop-and-page-lifecycle](../../../../specs/jiuwenswarm/extensions/app_market/S_02_app-market-stop-and-page-lifecycle.md)。

## 根因与修复决策

**根因 1（停止强杀）**：`MarketService.stop_app` 对模板静态应用（`_stop_script` 判定停止脚本为生成模板后返回 None）调用 `ProcessSupervisor.stop(stop_script=None)`，Windows 分支直接 `force_kill`（Job Object `TerminateJobObject`），没有 Unix `killpg(SIGTERM)` 那样的优雅阶段。应用注册在 `app_server.py` 里的 SIGTERM/SIGINT 退出清理（删除角色会话）永远没有机会执行。日志证据（`gateway.log` 2026-11-05 08:59:32）："通过 Job Object 终止进程树 pid=31792"——进程与端口回收成功，但应用 `runtime/session_state.json` 的 `pending_delete` 为空、5 个会话残留于 `~/.jiuwenswarm/agent/sessions/web_1a1a936e*`，证明退出清理未运行。

**根因 2（页面无感知）**：实例生命周期只绑定服务进程 pid 存活与 push/pull 状态，与页面是否打开完全无关；关闭页面不影响进程，市场 3 秒轮询继续显示"运行中"。链路上没有任何页面存活信号。

**修复决策**：

- 停止链路增加优雅阶段：市场先 POST `/_workswarm/shutdown`（状态令牌鉴权，2s 超时），等待应用自行退出最多 8 秒，超时或未实现端点回退既有强杀路径。Windows 没有跨进程 SIGTERM 等价物（`CREATE_NO_WINDOW` 下控制台事件不可达，`os.kill(pid, SIGTERM)` 即 TerminateProcess），HTTP 端点是模板与服务端应用都已具备的基础设施，与 pull 状态协议同构。
- `static_server.py` 模板实现该端点；由于模板经 `WORKSWARM_STATIC_SERVER` 以仓库内路径注入，存量纯静态应用无需重新发布即获得该能力。
- 页面联动走前端：桌面 browser 页签关闭事件（`desktopBrowserFile`）补充会话载荷，模块级 `appMarketPageLifecycle` 记录"实例 ↔ 打开会话"并在页签关闭时自动调用 `app_market.stop`。监听必须放模块级——用户关页签时在聊天视图，市场面板未挂载。反向联动：手动停止/删除成功后，经 `getState` 确认页签仍显示该应用入口时关闭页签。

**备选与拒绝**：给 `static_server` 注入页面心跳脚本、由服务端在无页面时自行退出——需向用户应用 HTML 注入脚本且对自定义 `app_server` 无效，侵入性强，拒绝；`window.open` 回退路径放弃 `noopener` 以跟踪窗口关闭——重新引入 reverse tabnabbing 风险（应用页面含任意生成内容），拒绝，浏览器回退场景维持手动停止。

## 回归证据与适用范围

候选：本工作区（feat/app-market，基于 96fe7b777）。环境：Windows 11，仓库 `.venv`。

- 后端：`pytest tests/unit_tests/test_app_market_runtime.py -q -o addopts=""` 13 通过（新增 3 例：shutdown 端点 401/200 与自行退出、stop_app 优雅退出退出码为 0、无端点时回退强杀退出码非 0 且 `request_shutdown` 被调用）；`test_app_market_publish/static_capabilities/gateway_config` 11 通过。
- 前端：`npm run test:app-market` 15 通过（新增 4 例：页签关闭自动停止、其他会话页签关闭不停止、停止成功关闭仍显示应用的页签、页签显示其他内容时不关闭）。
- 未覆盖：真实 Electron 壳中的 `getState`/页签关闭端到端行为（依赖 jiuwenclaw 桌面壳，单测以 mock 验证）；带自定义 `app_server.py` 的存量应用（如 Sunnyville）需按新契约补 `/_workswarm/shutdown` 实现后才能优雅清理，未补时行为与修复前一致（强杀，不劣化）。重验条件：宿主重启后走"启动 → 关闭页签/点停止 → 会话列表与端口回收"全链路。

## 防错模式与资料同步

平台差异防御：跨平台生命周期操作不能假设 Unix 信号语义可用，Windows 子进程需先约定应用层退出协议（HTTP 端点）再考虑强杀。已写入 S_02 spec。本次同步：扩展 README、app-builder SKILL.md、S_02 spec、本记录；索引经 `sync_documents.py` 刷新。

## 检视后修订（2026-11-05）

针对 `7b1d10c5d` 的两个检视问题续写本记录，原始验证仅证明前述场景；以下证据对应本次 amend 前的修复工作区（基于该提交）。

- **主进程优雅退出后子进程残留**：原 `ProcessSupervisor.stop` 在主 PID 消失后直接释放追踪，绕过了原强杀路径的整树回收。修订为优雅请求前及等待期间保留子进程身份；停止收尾时继续终止所属 Job Object / 进程组并回收保留的子进程，确认退出后才释放追踪与端口。保留 `psutil.Process` 身份以避免只按可能复用的 PID 回收其他进程。主进程原有退出码保持不变，不因整树收尾把优雅退出误判成强杀。回收失败不释放资源，也不标记停止成功。
- **共享页签历史关联导致误停**：同一会话依次打开 A、B 后，旧实现同时保留两条记录；停止 B 触发页签关闭时额外停止 A。修订为同一会话只关联当前应用；打开其他应用或产物时移除旧关联，后台实例继续运行。
- **验证**：Windows、仓库 `.venv`；`python -m pytest tests/unit_tests/test_app_market_runtime.py -k 'stop_app or shutdown_endpoint or graceful_stop' -q -o addopts=`，5 通过、10 未选中。新增真实进程用例在启用 Windows Job Object 和禁用 Job 的两条路径中验证：主进程退出码 0、残留子进程退出、状态 `stopped`、端口分配释放。`npm run test:app-market` 19 通过，新增同会话两应用的关闭/停止/删除隔离与产物替换关联场景。
- **限制**：未在 macOS/Linux 实机验证进程组路径；未验证真实 Electron 壳交互。当前证据不替代原记录中自定义应用 shutdown 端点的迁移限制。

## 遗留与后续

### Rebase 到独立应用容器（2026-11-05）

目标基线 `feat/app-market` 的 `bb2d61992` 已实施 [S_01_app-market-browser-containers](../../../../specs/jiuwenswarm/channels/desktop/S_01_app-market-browser-containers.md)：Web 内嵌 iframe、Electron 独立窗口，手动关闭页面继续运行服务。此次 rebase 保留该当前契约，移除本记录前述共享聊天 browser 页签关联、自动停止模块与对应旧用例；共享页签方案及其两次前端验证仅保留为历史事实。后端优雅请求、静态服务端点、整树回收及其回归用例继续有效。停止/删除仍使用目标基线按 app_id 关闭容器的实现，不依赖聊天页签。

Rebase 工作区验证：`npm run test:app-market` 22 通过（含两个应用并存时停止/删除隔离，以及聊天 browser 页签事件不触发应用停止）；`node --test tests/electron/app_market_windows.test.cjs` 13 通过。后端实现与 `59d0bb77d` 完全相同，复用上述 5 个定向用例证据，不重复执行。未进行真实 Electron UI 或 macOS/Linux 实机验证。

- `AppMarketManager.list_instances` 的恢复逻辑存在重复 stop 任务竞态（本次日志中同实例两次"实例已停止"），幂等无害，未在本修复范围处理。
- 存量自定义 `app_server` 应用需各自补 shutdown 端点（应用代码在仓库外，用户自行更新）。

## 实例级页面清理（2026-11-05）

`04e7cacf7` 的实例轮询只更新卡片，实例外部停止后旧 iframe 仍会保留。页面现按 `instance_id` 跟踪，轮询在市场面板卸载后持续到相关页面关闭。已停止、被替换或消失的实例只清理自身容器。收到新实例的打开请求时提前清理仍占用同一端口的旧页面，过期轮询不影响新打开的容器。

`npm run test:app-market` 22 例通过，覆盖两张卡片打开不同地址、桌面停止和删除只关闭目标窗口、Web 实例停止和替换只清理对应 iframe，以及离开市场后的清理。`node --test tests/electron/app_market_windows.test.cjs` 13 例通过，覆盖窗口隔离和复用。未运行真实 Electron UI。
