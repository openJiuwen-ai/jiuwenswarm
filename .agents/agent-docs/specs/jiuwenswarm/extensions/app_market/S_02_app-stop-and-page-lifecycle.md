---
id: S_02_app-stop-and-page-lifecycle
title: 应用停止协议与页面联动
type: S
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
  - jiuwenswarm/extensions/app_market/manager.py
  - jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/appMarketViews.ts
  - jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md
---

# 应用停止协议与页面联动

## 停止顺序

市场停止实例（`app_market.stop`、删除应用与关闭清理共用同一路径）时按固定顺序回收：先请求应用自行退出，再回退到既有强制路径。

1. 进程存活时，市场向 `http://127.0.0.1:<port>/_workswarm/shutdown` 发起 POST，携带 `Authorization: Bearer <WORKSWARM_STATUS_TOKEN>`，请求超时 2 秒。
2. 应用返回 200 后，市场轮询主进程存活最长 8 秒；主进程退出即完成应用的优雅退出，但仍需回收残留子进程后才能释放端口与标记 `stopped`。退出前保留子进程身份；退出后终止所属 Windows Job Object / Unix 进程组，并按保留身份补充回收子进程（包括 Windows 未绑定 Job Object 的路径），确认回收后才释放追踪。回收失败保留 `stopping` 与端口分配，不能报告成功。
3. 端点不存在、鉴权失败、超时或应用未在宽限期内退出时，回退既有路径：有自定义停止脚本先执行脚本（模板停止脚本按端口杀进程，被市场跳过改为终止自有进程树），随后 Windows 经 Job Object `TerminateJobObject`/`taskkill /T` 终止进程树，Unix 先 `killpg(SIGTERM)` 再强制终止，强制回收超时 10 秒。

应用侧契约由应用开发技能维护：自带 `scripts/app_server.py` 的应用必须实现 `/_workswarm/shutdown`（校验状态令牌，清理角色会话后退出）；纯静态托管由市场 `static_server.py` 模板内置实现。强杀不触发应用任何退出处理，因此应用的会话清理只能依赖优雅路径或下次启动的待清理任务。

`static_server.py` 的 `/_workswarm/shutdown`：无令牌返回 401，非该路径 POST 返回 404，鉴权通过返回 200（`state:"stopping"`）并在响应写回后于独立线程调用 `server.shutdown()` 有序退出，`main()` 在 `serve_forever` 返回后关闭监听 socket。优雅退出的包装进程退出码为 0，被强杀的进程树退出码非 0，测试据此区分两条路径。

## 页面联动

页面容器遵循 [S_01_app-market-windows](../../channels/desktop/S_01_app-market-windows.md)：普通 Web 使用市场内嵌 iframe，Electron 使用按 `app_id` 隔离的独立窗口。

- 关闭、隐藏应用窗口或页面不停止服务；聊天 browser 页签关闭不控制应用实例。应用页面不记录与聊天会话共享页签的关联。
- 停止或删除 RPC 成功后只关闭该 `app_id` 的容器，不关闭或停止其他应用。失败时保留容器；停止成功后的窗口清理异常不回滚已停止的服务状态。
- 页面绑定实际打开的 `instance_id`。轮询发现该实例已停止或消失时清理对应页面，包括隐藏页面和离开市场后的页面。新实例打开时清理同一应用或同一端口的旧实例页面，过期轮询不得关闭已替换的页面。
- `app_market.start` 和 `app_market.instances` 返回 `web_url` 作为 Web 内嵌入口，其主机为按 `app_id` 派生的 `workswarm-app-<摘要>.localhost`。端口和入口路径与 `url` 一致。Electron 沿用 `url` 及按应用隔离的持久分区。

原共享 browser 页签自动停止方案在 rebase 到 `feat/app-market` 的独立容器实现后退役；其历史验证仅见故障记录，不作为当前页面生命周期契约。

## 不变量

- 市场停止始终回收进程树与端口，最终状态为 `stopped`；优雅请求只影响应用能否执行退出清理，不影响回收结果。
- 状态令牌仅用于状态与停止端点鉴权，不构成 Gateway 会话接口凭据。
- 应用展示与聊天会话、browser 页签及 headless 配置解耦，关闭页面不会隐式停止服务。
- Web 应用使用独立来源，端口复用时仍分隔 HTML、相对路径资源及浏览器存储。市场静态模板对所有响应发送 `Cache-Control: no-store`，自带 `app_server.py` 的应用同样应禁用缓存并接受对应的 Web 来源。
