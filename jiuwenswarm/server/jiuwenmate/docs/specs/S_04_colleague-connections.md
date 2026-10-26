# 同事配置与连接规约

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | S_04 / colleague-connections |
| 关联模块 | `repository.py`、`service.py`、`web_adapter.py`、`agent_manager.py`、`feishu_group_fanout.py`、`dingtalk_inbound.py`；Gateway `app_web_handlers.py` 的飞书、钉钉绑定与同事删除 |
| 最近一次修订日期 | 2026-10-26 |
| 关联 feature | [F_02_02 同事配置与连接聚合](../features/F_02_02_colleague-connections.md)、[F_02](../features/F_02_colleague-management.md)、[F_21_02](../features/F_21_02_dingtalk-direct-message.md) |
| 设计依据 | [架构](../designs/jiuwenmate-architecture.md)第 2.1、4.1、9.2 节 |

状态：已实现，提交前的本地工作区；组件与接线（替身）验证见 F_02_02 第 8 节，真实飞书、钉钉联调未进行。

## 1. 范围、术语与适用条件

- **同事（配置）：** 用户看到的一位 AI 员工，包含设置、人设、草稿与发布标记。配置保存在同事表中它自己的一行，这一行同时是它的 Web 运行 Agent，因此配置 ID 等于 Web Agent 的 `agent_id`。
- **连接：** 同事工作的一处地方：Web 页面，或一个飞书、钉钉应用。隔离单位是连接；换一个 app_id / client_id 就是新连接。
- **运行 Agent：** 服务一个连接的执行身份，拥有自己的版本、办公目录、Session、窗口、群访问与飞书 CLI 资料。Web 连接的运行 Agent 是配置行本身；每个 IM 连接另有一行。
- **登记：** IM 运行 Agent 与其连接的专属关系，即 `(agent_id) → (config_id, channel, connection_key)`。

适用于命名空间 `local`、平台 `feishu`（`channels.feishu.apps`）与 `dingtalk`（`channels.dingtalk.apps`）。企业飞书（`channels.feishu_enterprise`）不登记，例外见第 3 节。

## 2. 数据结构与事实归属

| 事实 | 权威位置 |
| --- | --- |
| 配置的设置、revision、发布标记、启停、隐藏 | `colleagues` 中没有登记的行；`hidden`、`published_settings_json`（含人设）两列 |
| 人设草稿 | `templates/<config_id>/` |
| IM 运行 Agent | `colleagues` 中有登记的行：设置只记录当前版本所用的设置，`revision = published_revision` 为对应的配置修订 |
| 登记 | `colleague_connections`，主键 `(namespace_id, agent_id)`，唯一 `(namespace_id, config_id, channel, connection_key)` |
| 某个应用当前由谁服务 | `config.yaml` 应用条目的 `persistent_agent_id` |
| 版本 | `config_versions`，按运行 Agent 分别编号 |
| 已删除的配置 | `removed_colleagues`，主键 `(namespace_id, agent_id)`；与配置行在同一事务中写入，用于识别重复的删除 |

## 3. 不变量

1. **专属：** 一个运行 Agent 至多登记一个连接；同一同事的同一平台应用至多一个运行 Agent。
2. **入站一致：** 飞书与钉钉只受理 YAML 绑定到的 Agent 恰好登记为本平台、本接收应用的事件；否则拒绝，不进入普通助手。读取登记失败时拒绝。例外：企业飞书绑定在 Agent 未登记时照旧受理；已登记给其他连接的 Agent 仍被拒绝。
3. **发布一致：** 一次发布在一个事务内切换同事全部运行 Agent（Web 与全部已登记 IM Agent）的版本；任一 Agent 准备失败、配置 revision 变化或 Agent 集合变化时，全部保持原版本。
4. **新连接不带草稿：** 新 IM Agent 的首个版本由已发布设置生成；同事从未发布时，新 Agent 也无版本。
5. **运行视图：** IM Agent 的名称、描述、群回复方式与启停来自其同事的当前设置；`agent_id`、当前版本与办公目录是它自己的。
6. **管理只作用于同事：** 保存设置、发布、启停、查询配置只接受配置 ID；对 IM Agent 返回 `BAD_REQUEST`。配置级启停作用于全部连接。
7. **删除先断流：** 两种删除都先移除 YAML 中绑定到该同事任一 Agent（含其 Web Agent）的应用条目，再改动 AgentServer 中的数据。
8. **无孤立连接：** 添加连接的事务核对同事存在、不是 IM Agent、未隐藏，否则 `NOT_FOUND` 且不写入任何记录；同事行在仍有连接时不能删除（`CONFLICT`）。添加连接在准备版本后发现自己的 Agent 已被删除时，移除其重新建立的办公目录并返回 `NOT_FOUND`。
9. **删除可重试且不误报：** 每个运行 Agent 的清理全部完成、并核对模板与办公目录已不存在后，才删除它的登记与同事表行；任何一步失败都返回错误，未删完的 Agent 仍能被查询到。已完成的 `unbind` / `purge` 被原样重试时返回成功；从未存在的 ID 返回 `NOT_FOUND`。
10. **绑定与删除串行：** 在同一个 Gateway 进程中，同一同事的 `channel.feishu.bind_agent`、`channel.dingtalk.bind_agent` 与 `jiuwenmate.colleague.delete` 逐个执行，每个操作覆盖询问 AgentServer、写 YAML 与重启渠道的整段，失败、超时或取消时释放。因此绑定成功时其应用指向仍存在的连接 Agent，删除成功时 YAML 中没有指向该同事任一 Agent 的应用；删除之后到达的绑定得到 `NOT_FOUND`，不写 YAML。不同同事不共用编排锁；渠道自身的重启协调不变。

## 4. 接口契约

公开 Web 方法（Gateway 转发到 AgentServer，删除除外）：

| 方法 | 契约 |
| --- | --- |
| `jiuwenmate.colleague.list / get / ensure_default / create` | 只返回未隐藏的同事；每项含 `config_id` 与 `connections`：第一项为 `{channel: "web", connection_key: "web", agent_id, workspace_dir}`，其后为 YAML 当前绑定到登记 Agent 且启用的 IM 连接。`im_configured` 表示至少有一个 IM 连接 |
| `jiuwenmate.colleague.settings.get / settings.update / publish / set_enabled` | `agent_id` 必须是配置 ID（不变量 6） |
| 聊天、会话、请求、群记录、执行轨迹 | `agent_id` 可以是同事的任一运行 Agent |
| 定时任务、邮件授权、活跃统计 | 按 Web Agent 工作，不随连接切换 |
| `jiuwenmate.colleague.delete {agent_id, mode}` | Gateway 本地处理，`mode` 为 `unbind` 或 `purge`，见第 5 节 |

Gateway 渠道方法：

| 方法 | 契约 |
| --- | --- |
| `channel.feishu.bind_agent {config_id, app_id, app_secret}` | 先取得连接 Agent，再写入应用条目并重启飞书渠道 |
| `channel.dingtalk.bind_agent {config_id, client_id, client_secret, robot_code?}` | 同上，写入规则见 S_03 |
| `channel.feishu.unbind_agent {app_id}` / `channel.dingtalk.unbind_agent {client_id}` | 删除该应用条目，仅当它绑定了 Agent；登记与数据保留；条目不存在时成功 |

AgentServer 拒绝时，Gateway 原样返回其错误码；无法回答时返回 `SERVICE_UNAVAILABLE`，不写 YAML。

内部 E2A 方法（Gateway → AgentServer，不对 Web 公开）：

| 方法 | 输入 | 成功结果 |
| --- | --- | --- |
| `jiuwenmate.connection.attach` | `config_id`、`channel`（`feishu` / `dingtalk`）、`connection_key` | `{agent_id}`；同一同事同一应用重复调用返回同一 Agent |
| `jiuwenmate.colleague.connections` | `config_id` | `{connections: [{channel, connection_key, agent_id}], removed}`，含 YAML 已不引用的登记；已隐藏的同事照常列出；已被 `purge` 删除的同事为空列表且 `removed` 为真 |
| `jiuwenmate.colleague.remove` | `config_id`、`mode` | `{removed: true}`；对已隐藏同事的 `unbind`、对已删除同事的两种模式直接成功 |

失败时 `ok=false`，载荷为 `{error, code}`：配置不存在为 `NOT_FOUND`（`attach` 对已隐藏的同事也是 `NOT_FOUND`），传入 IM Agent 或不支持的平台为 `BAD_REQUEST`，并发发布或删除期间持续新增连接为 `CONFLICT`，文件未能删除为 `CLEANUP_FAILED`。

## 5. 生命周期

- **添加连接：** 已有登记则返回原 Agent；否则在一个事务中插入 Agent 行与登记行，并按不变量 4 发布。发布期间同事被重新发布、新 Agent 已由该次发布得到版本时，添加连接仍算成功；否则返回 `CONFLICT`。
- **改绑到另一同事：** 另一同事得到新 Agent；原 Agent 的登记与数据保留，因 YAML 不再引用而不再收到消息。
- **解除连接：** 只删除 YAML 应用条目。同一同事再次绑定同一应用时沿用原 Agent。
- **`unbind`：** 同事设为隐藏并停用；全部 Agent、版本、办公目录与会话保留，页面不提供恢复。
- **`purge`：** 逐个 IM Agent、最后 Web Agent。每个 Agent：先在 IM 运行时与 Web 运行时结束其排队与执行中的请求，再清理会话列表、定时任务、CLI 资料、邮件授权、模板、办公目录与窗口，核对目录已不存在，最后在一个事务中删除版本、登记、群访问与同事表行。删除完已读到的连接后重新读取，处理期间新增的连接，最多三轮，仍有新增时返回 `CONFLICT`。配置行与删除记录一起写入。执行记录（Session、Request、执行事件与投递）、权限审批与群检索游标沿用现有删除规则，不清理。
- **重试：** Gateway 删除的每一步都可重复：已删除的应用条目不会再被找到；AgentServer 一侧按不变量 9 识别已完成的删除。

## 6. 限制与兼容

- 在登记出现之前保存的同事自动成为只有 Web 连接的同事；YAML 中指向它们的飞书、钉钉绑定被不变量 2 拒绝，须重新绑定，重新绑定得到新的 IM Agent。
- 企业飞书未登记，跨连接隔离缺口见 F_02_02 第 9.2 节。
- 不变量 10 只在单个 Gateway 进程内成立；多个 Gateway 进程同时写同一份渠道配置不在支持范围内。按应用解除（`unbind_agent`）不参与按同事的串行。
- 同一群中同一同事的两个飞书连接会以两个同名成员出现。
- 已解除连接、已隐藏同事的数据没有查看入口。
- 旧版本代码会把 IM Agent 行当作普通同事列出，且不认识新列和新表，不能直接回滚。

## 7. 验证索引

验证场景与执行记录见 F_02_02 第 8 节，不在本规约维护进度。
