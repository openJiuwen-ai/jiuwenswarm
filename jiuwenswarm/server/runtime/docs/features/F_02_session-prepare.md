# F_02 会话准备：首条消息路由后按对话身份提前构建会话实例

| 项 | 值 |
|---|---|
| 类型 | feature |
| 编号 / slug | F_02 / session-prepare |
| 关联模块 | `jiuwenswarm/server/handlers/session_prepare.py`、`handlers/_default.py`、`handlers/_shared.py`、`runtime/tenant_agent_pool.py`、`runtime/session_prepare_tracker.py`、`runtime/agent_adapter/interface.py`、`runtime/agent_adapter/interface_deep.py`、`common/schema/message.py`、`dispatch.py`、`agent_http_routes.py` |
| 日期 | 2026-11-12 |
| 关联 spec | runtime [[S_02]] N10–N16、P6–P7；server [[S_02]] 分发表 session_prepare 域 |

## 背景

OfficeClaw 新对话的首轮里，会话实例构建（create_instance + 启动交互会话）在本地约 0.14~0.2s，测试环境 0.44~1.41s，
全部压在「收到 chat.send → 发起模型调用」这一段上。Relay 在首条消息路由完成后、发出 chat.send 之前还有自己的处理
（排队、调用组装、配置同步，本地 0.6~2.4s），这段时间 AgentServer 是空闲的。

单 Agent 对话的会话 ID 由 Relay 按「渠道 + 用户 + 智能体 + 线程」计算，目标智能体在路由完成时才确定。所以 Relay 在路由
之后发 `session.prepare`，AgentServer 用与随后 chat.send 完全相同的身份在后台构建实例，chat.send 到达时直接用。

## 接口

`session.prepare`（WS 分发表；HTTP `POST /sessions/{session_id}/actions/prepare`），帧的路由字段与 chat.send 相同，
`params` 只看 `mode` 与工作目录类字段。立即应答 `{session_id, accepted, status}`：

| accepted | status | 含义 |
|---|---|---|
| true | scheduled | 已入队或开始构建 |
| true | already_running | 该会话已有准备在排队或构建 |
| false | already_ready | 该会话的实例已存在 |
| false | queue_full | 队列已满（上限 8） |
| false | mode_not_supported | 解析结果不是普通 agent / flash |
| false | unknown_agent | 智能体尚未同步到 AgentServer |
| false | disabled | `JIUWENSWARM_SESSION_PREPARE=0` |

错误应答：`INVALID_PARAMS`（缺字段、非 officeclaw 渠道）、`invalid_tenant_id`。

## 设计要点

| 点 | 做法 | 为什么 |
|---|---|---|
| 解析与 chat 同源 | 从 `_prepare_code_mode_chat_turn` 拆出 `resolve_chat_turn_routing` / `resolve_readonly_project_dir`，两边调同一函数；`_uses_explicit_officeclaw_mode` 认 `SESSION_PREPARE` | 解析不一致不会报错，只会静默落到另一个根实例上；漏认方法时准备会按全局 Flash 开关解析 |
| 只读 | 模式与工作目录走已有的只读分支，不写 metadata.json、不写对话历史；未带工作目录时，构建实例按现有逻辑建默认工作区 `projects/<session_id>`（与 chat 首轮、session.create 预热同源） | 准备之后消息可能根本没发出去，不能留下会被当成会话的元数据和历史 |
| 按请求身份构建 | `prepare_session(request=...)`：门面层绑定 `_bind_tenant_request_context`，请求传入 `_get_or_create_session_adapter` | 线程 ID、租户环境变量命名空间与 chat 建出的实例一致；session.create 预热不传，行为不变 |
| 复用预热任务表 | 与 session.create 预热共用 `_session_prewarm_tasks`，chat 经现有 `wait_for_session_prewarm` 加入 | 「对话加入进行中的构建」不需要新机制 |
| 低优先级 | 全进程同时构建 1 个、排队 8 个；chat 到达时把同会话排队中的准备移出队列、自己构建；worker 等一个构建最多 60s，超时转去下一个、不取消它 | 准备是尽力而为，不能让前台对话排在别人的准备后面；一个卡住的构建不能堵死队列 |
| 未使用回收 | 准备新建的实例 600s 内没被取用，按空闲淘汰同款守卫回收，守卫不满足每 60s 重试；取用一次转为普通实例 | 现有空闲回收是 2 小时，且只在请求路径上顺带执行 |
| 配置同步后热更新 | 根实例标记待重载后，在后台持会话锁对未使用的准备实例做懒重载 | chat 到达时实例已是新配置；仍在进行就在同一把锁上等 |

## 命中日志

每条 officeclaw chat.send 第一次取得会话实例时，记一条 `[session.prepare] chat result=...`，按 `session_id` 与 Relay 的
发送 / 跳过日志对照：

| result | 含义 | 附带 |
|---|---|---|
| hit | 准备好的实例直接使用 | `wait_ms`（取锁耗时） |
| joined | 加入了进行中的准备构建 | `wait_ms` 含加入等待 |
| hit_reloaded | 取用时配置已变，等了后台热更新或自己做了懒重载 | `wait_ms` 含重载等待 |
| miss | 新建实例 | `reason`：`not_prepared` / `cancelled_in_queue` / `queue_full` / `prepare_failed` / `reclaimed` / `lost_after_<状态>` |

准备各步骤的状态由 `session_prepare_tracker` 在进程内记录（上限 1024 条）。后台热更新本身另记
`prepared session reloaded ... duration_ms`。

## 拒绝的方案

- **预建无身份的备用实例、消息到达时认领（方案 A）。** 需要在认领时改绑 12 类构建时固化的会话状态，以后新增插件都要
  记得处理认领路径，出错表现为偶发的会话串数据。保留为后续增强。
- **在创建对话时触发。** 此时目标智能体还没路由出来，会话 ID 算不准，猜错只会白建一次。
- **复用 `JIUWENSWARM_SESSION_CREATE_PREWARM` 开关。** 两者触发方与范围不同，关掉一个不应影响另一个。

## 边界

- 只覆盖 officeclaw 单 Agent 的普通 agent / flash；团队、编码（本地 / 云端）不准备。
- 任何经租户池 `wait_for_session_prewarm` 的请求都会把同会话排队中的准备移出队列，不只 chat。
- 未带工作目录的准备会留下空的默认工作区 `projects/<session_id>`；chat 首轮与 session.create 预热本来如此，不是准备引入的新文件。
- 回收守卫不满足时（等待审批、锁被持有、有存活子代理）暂不回收，每 60s 重试，直到回收成功或被对话取用。
- `params.mode` 必填：chat.send 不带 mode 时会回退会话元数据，准备没有元数据可回退，缺 mode 应答 `INVALID_PARAMS`。
- 解析出子模式（如进入 plan）时应答 `mode_not_supported`，这类会话首轮不受益，以命中日志核对实际命中率。
- 会话被 2 小时空闲淘汰后再次对话，命中日志会记 `miss reason=not_prepared`；统计命中率时只看该会话第一条消息。

## 验证

- `tests/unit_tests/server/handlers/test_session_prepare.py`：应答状态与错误码、与 chat 的选型等价、不落盘、加入构建、
  队列并发 / 上限 / 抢占、回收与守卫、配置同步后热更新、四种命中结果。
- `tests/integration/test_session_prepare_equivalence.py`：子进程内用 WarmupModelClient 真实构建，准备路径与 chat 路径
  建出的会话实例在 rail（43 个）、工具（55 个）、线程 ID、工作目录、模式、权限、模型上逐项一致（agent / flash）；
  准备时不传请求即在线程 ID 上比出差异。
