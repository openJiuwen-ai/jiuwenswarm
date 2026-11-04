---
id: B_20261031_1340_draft-dispatch-lifecycle-events
title: TUI draft dispatch and cross-window lifecycle events
type: B
author: Yanqing-Shi
author_email: toddsyq@gmail.com
modules:
  - jiuwenswarm/channels/tui/frontend
  - gateway/channel_manager/tui
  - gateway/channel_manager/web
  - jiuwenswarm/server/runtime/session
files:
  - jiuwenswarm/channels/tui/frontend/src/app-state.ts
  - jiuwenswarm/channels/tui/frontend/src/core/commands/helpers.ts
  - jiuwenswarm/channels/tui/frontend/tests/draft-session.test.mjs
  - jiuwenswarm/channels/tui/frontend/tests/archive-command.test.mjs
  - jiuwenswarm/channels/tui/frontend/tests/delete-command.test.mjs
  - gateway/channel_manager/tui/tui_connect.py
  - gateway/channel_manager/web/lifecycle_handlers.py
  - jiuwenswarm/server/runtime/session/lifecycle.py
  - jiuwenswarm/server/runtime/session/session_archive.py
  - tests/unit_tests/server/test_archive_lifecycle.py
designs: []
design_note: Narrow repair of the TUI draft and session lifecycle behavior in PR 10243; no independent D document.
---

# TUI 草稿发送顺序与跨窗口生命周期事件修复

## 现象与影响

草稿首条消息等待 `session.create` 时，Ctrl+C 和补充输入立即用草稿 token 发送中断帧，可能先于真实 `chat.send` 到达服务端。另一个 TUI 窗口归档或删除当前会话时，Gateway 的生命周期轮询使用 WebChannel 接口，在 TUI 路由上无法找到客户端并退出；被关闭窗口不能及时进入新草稿。

## 关联设计与当前契约

本次按 PR #10243 的草稿会话和生命周期行为修复。TUI 的 WebSocket 入站宿主是 `GatewayServer`；会话与用户绑定以其 `_session_to_client` 和 `_connection_user_id` 为准。草稿首次发送必须先创建真实会话，后续会话帧按用户操作顺序发送。

## 根因与修复决策

- 之前仅让 `chat.send` 等待草稿创建，其他 `sendEventOnly` 帧独立发送；Ctrl+C 还会把在途 `session.create` 当作普通命令请求取消。现在将草稿创建期间的出站帧放入同一发送队列，创建成功后按原顺序发送；本地中断跳过该创建请求。创建失败时丢弃排队帧并恢复空闲状态。
- 生命周期处理器原本从 WebChannel 的 `clients` 与 `connection_user_id` 取客户端。TUI 注册时显式提供 GatewayServer 的连接、用户与目标会话映射；会话绑定后启动轮询。归档或删除的即时事件也送达同用户中绑定目标会话的窗口；初次轮询可送达当前已阻断执行的会话状态。TUI 不广播其他会话的轮询事件。

## 回归证据与适用范围

基于 `710ee1399` 加本任务工作区改动：定向运行 Gateway 生命周期与 TUI 路由测试，5 项通过。使用本机 WorkSwarm 附带的 Node 22.11.0，将修改的 TypeScript 临时转译到既有 `dist` 后运行 `tests/draft-session.test.mjs`，通过；原 `dist` 文件已恢复。用例覆盖草稿创建未完成时的首条消息、Ctrl+C、补充帧顺序，以及同用户目标窗口接收事件、其他用户窗口不接收。未进行真实多窗口服务联调或全量测试。

## 资料同步与遗留

中英文 TUI 使用指南已同步发送顺序和关闭会话的窗口行为。

## 后续复查：草稿期的会话请求

草稿期执行 `/rewind`、`/compact` 等命令时，之前会把尚未落库的 create token 作为 `session_id` 传给服务端。现于请求入口拦截需要已保存会话的 RPC，并提示先发送消息；`/resume` 使用明确的目标会话 ID，继续允许从草稿切换。`tests/draft-session.test.mjs` 覆盖直接请求及三个命令入口，验证没有发出草稿 ID 请求，首次聊天仍正常建会话。对应定向测试已通过。

## 后续复查：创建在途时切换会话

草稿首条消息触发的 `session.create` 在途期间若切换会话，响应到达时旧逻辑直接返回，已创建的空会话无人认领。原发送队列还可能把旧草稿消息发到切换后的会话。现为排队帧记录所属会话，只在原草稿成功转成真实会话时迁移归属；切换后丢弃旧帧，并在后台请求删除已创建的旧空会话。删除请求失败时在当前会话显示错误。服务端接受删除请求后仍可能在后台继续清理，不将受理响应视为物理删除完成。未接入的 `findReusableEmptySession` 及其模拟复用的测试路径已移除。

使用本机 WorkSwarm 附带的 Node 运行 TypeScript 构建与 `draft-session.test.mjs`、`session-registration.test.mjs`、`archive-command.test.mjs`、`delete-command.test.mjs`，四个脚本均通过。新增用例覆盖切换到已保存会话和切换到另一草稿时的旧帧隔离与空会话清理。尚未进行真实 Gateway 与 TUI 多窗口联调。

## 后续复查：创建响应超时或断线

首个 `session.create` 响应可能在服务端创建会话后丢失。客户端现暂存原始 `create_token` 和创建参数：若草稿仍是当前会话，保留原有失败提示，等待后续发送重试；若已切走，在连接可用时以相同参数重试取回会话 ID，然后请求删除。断线期间等待重连，结果仍不明时每 5 秒重试；TUI 停止时取消定时器。确定性恢复失败和删除请求失败会显示错误。

此恢复依赖当前 TUI 进程仍运行，以及 AgentServer 内存中的 `create_token` 映射。若 AgentServer 重启并丢失映射，重试可能得到新 ID，无法凭该 token 找到旧的空会话；TUI 退出后也不再重试。删除受理后的最终清理仍由服务端生命周期任务负责，本地用例不证明磁盘文件已经移除。

本次用 WorkSwarm 附带的 Node 构建并运行上述四个 TUI 测试脚本，均通过；新增用例覆盖切换前后超时、断线后重连、相同 token/参数重试和旧消息不转发。另在 Python 3.14 环境定向运行服务端 `test_persist_session_is_create_identity_but_not_warm_key`，1 项通过，证实同一进程内相同 token 复用领取结果。未执行真实断线联调或物理删除完成验证。

## 后续复查：非当前会话删除失败提示

`/delete` 删除非当前但正在运行的会话时，服务端批量结果返回 `SESSION_BUSY`，此前 TUI 直接拼接服务端英文错误。现按错误码展示中英文可操作提示，并保留其他错误的原有回退路径。`delete-command.test.mjs` 增加中文场景，确认错误提示不含服务端英文且不发生会话重定向。基于 `cdf456f91` 与上游 `b825ac938` 的未提交合并工作区，用 WorkSwarm 自带 Node 22.11.0 临时转译改动文件后运行该脚本，通过；原有 `dist` 文件已恢复。

## 后续复查：未受理的 idle 检查不产生关闭事件

归档或删除的 idle 检查先建立执行围栏与 `check_idle` operation；这一步尚未受理用户操作。旧逻辑将这个阶段当作已归档，并在 `SESSION_BUSY` 时用普通 `completed` 结束 operation，Gateway 轮询据此发出错误的 `session.archived` 或 `session.deleted` 事件，TUI 可能离开仍可用的会话。

现在 `check_idle` 不参与归档可见性或生命周期事件投影。探测失败和重启恢复均以同一次状态写入释放围栏，并记录 `phase=not_accepted`、`result.accepted=false`；事件快照跳过未受理记录，包括旧版本留下的已完成 `check_idle` 记录。成功通过探测后才进入可发布的操作阶段。基于 `cdf456f91` 与 `b825ac938` 的未提交合并工作区，完整运行 `test_archive_lifecycle.py` 和 `test_lifecycle_handlers.py`，114 项通过；回归覆盖探测期间的活跃/归档列表、busy 拒绝后两轮 Gateway 轮询、重启恢复和正常完成事件。
