# 同事配置与连接规约

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | S_04 / colleague-connections |
| 关联模块 | `repository.py`、`service.py`、`web_adapter.py`、`agent_manager.py`、`feishu_group_fanout.py`、`dingtalk_inbound.py`；Gateway `app_web_handlers.py` 的飞书、钉钉绑定与同事删除 |
| 最近一次修订日期 | 2026-10-30 |
| 关联 feature | [F_02_02 同事配置与连接聚合](../features/F_02_02_colleague-connections.md)、[F_02](../features/F_02_colleague-management.md)、[F_21_02](../features/F_21_02_dingtalk-direct-message.md) |
| 设计依据 | [架构](../designs/jiuwenmate-architecture.md)第 2.1、4.1、9.2 节 |

状态：三渠道 canonical 身份已在本地实现并完成定向回归；真实渠道联调待验收。当前实现见 F_02_02 第 12 节；此前连接登记的历史验证仅适用于当时版本。

## 1. 范围、术语与适用条件

- **同事（配置）：** 用户看到的一位 AI 员工，包含设置、人设、草稿与发布标记。配置保存在同事表中它自己的一行，这一行同时是它的 Web 运行 Agent，因此配置 ID 等于 Web Agent 的 `agent_id`。
- **连接：** 同事工作的一处入口。Web、新式飞书和新式钉钉应用共用配置行及 canonical Agent ID；历史独立连接保留兼容。
- **运行 Agent：** Web、飞书和新式钉钉绑定共用配置行中的同一个 Agent 身份、版本和 workspace；Session 与窗口按会话隔离。历史独立连接行保留兼容。
- **登记：** 历史运行 Agent 与连接的关系；新式飞书和钉钉绑定不新增连接 Agent 或登记，当前应用绑定由 YAML 指定。

适用于命名空间 `local`、平台 `feishu`（`channels.feishu.apps`）与 `dingtalk`（`channels.dingtalk.apps`）。企业飞书（`channels.feishu_enterprise`）不登记，例外见第 3 节。

## 2. 数据结构与事实归属

| 事实 | 权威位置 |
| --- | --- |
| 配置的设置、revision、发布标记、启停、隐藏 | `colleagues` 中没有登记的行；`hidden`、`published_settings_json`（含人设）两列 |
| 人设草稿 | `templates/<config_id>/` |
| Feishu Agent | 配置行，即 Web canonical Agent；新绑定不新增 `colleagues` 行 |
| DingTalk Agent | 新绑定使用配置行，即 Web canonical Agent；历史独立连接保留 |
| 登记 | `colleague_connections` 保留历史连接；主键 `(namespace_id, agent_id)`，唯一 `(namespace_id, config_id, channel, connection_key)`；本轮不新增多绑定表 |
| 某个应用当前由谁服务 | `config.yaml` 应用条目的 `persistent_agent_id` |
| 版本 | `config_versions`，按运行 Agent 分别编号 |
| 已删除的配置 | `removed_colleagues`，主键 `(namespace_id, agent_id)`；与配置行在同一事务中写入，用于识别重复的删除 |

## 3. 不变量

1. **身份：** Web、飞书和新式钉钉绑定共用 canonical `agent_id`；各平台保留独立应用绑定。历史连接仍对应其原应用。一个配置每个平台最多一个当前应用绑定。
2. **入站一致：** 新式绑定的 `persistent_agent_id` 必须解析为可见配置 Agent，且接收应用唯一绑定到该 ID。历史运行 Agent 仅在登记的平台及 app 匹配时兼容。错误时拒绝，不进入普通助手。
3. **发布一致：** 发布切换配置 Agent 与其下全部历史登记 Agent 的版本；新式飞书和钉钉共享配置 Agent 版本。任一 Agent 准备失败、配置 revision 变化或 Agent 集合变化时，全部保持原版本。
4. **新连接不带草稿：** 新式飞书和钉钉绑定复用 canonical Agent 的已发布版本；同事从未发布时仍无版本，不在绑定时发布草稿。
5. **运行视图：** 新式飞书和钉钉都使用配置 Agent 的同一视图与 workspace；历史独立连接的配置和启停来自同事，但保留自己的版本及历史目录。
6. **管理只作用于同事：** 保存设置、发布、启停、查询配置只接受配置 ID；对 IM Agent 返回 `BAD_REQUEST`。配置级启停作用于全部连接。
7. **删除先断流：** 两种删除都先移除 YAML 中绑定到该同事任一 Agent（含其 Web Agent）的应用条目，再改动 AgentServer 中的数据。
8. **无孤立连接：** 添加连接的事务核对同事存在、不是 IM Agent、未隐藏，否则 `NOT_FOUND` 且不写入任何记录；同事行在仍有连接时不能删除（`CONFLICT`）。添加连接在准备版本后发现自己的 Agent 已被删除时，移除其重新建立的办公目录并返回 `NOT_FOUND`。
9. **删除可重试且不误报：** 每个运行 Agent 的清理全部完成、并核对模板与办公目录已不存在后，才删除它的登记与同事表行；任何一步失败都返回错误，未删完的 Agent 仍能被查询到。已完成的 `unbind` / `purge` 被原样重试时返回成功；从未存在的 ID 返回 `NOT_FOUND`。
10. **绑定与删除串行：** 在同一个 Gateway 进程中，同一同事的 `channel.feishu.bind_agent`、`channel.dingtalk.bind_agent` 与 `jiuwenmate.colleague.delete` 逐个执行，每个操作覆盖询问 AgentServer、写 YAML 与重启渠道的整段，失败、超时或取消时释放。因此绑定成功时其应用指向仍存在的连接 Agent，删除成功时 YAML 中没有指向该同事任一 Agent 的应用；删除之后到达的绑定得到 `NOT_FOUND`，不写 YAML。不同同事不共用编排锁；渠道自身的重启协调不变。

## 4. 接口契约

公开 Web 方法（Gateway 转发到 AgentServer，删除除外）：

| 方法 | 契约 |
| --- | --- |
| `jiuwenmate.colleague.list / get / ensure_default / create` | 只返回未隐藏的同事；`connections` 展示 Web、当前飞书／钉钉及仍有效的历史连接；新式三渠道 ID 相同。`im_configured` 表示至少一个 IM 渠道已绑定 |
| `jiuwenmate.colleague.settings.get / settings.update / publish / set_enabled` | `agent_id` 必须是配置 ID（不变量 6） |
| 聊天、会话、请求、群记录、执行轨迹 | `agent_id` 可以是同事的任一运行 Agent |
| 定时任务、邮件授权、活跃统计 | 按 Web Agent 工作，不随连接切换 |
| `jiuwenmate.colleague.delete {agent_id, mode}` | Gateway 本地处理，`mode` 为 `unbind` 或 `purge`，见第 5 节 |

Gateway 渠道方法：

| 方法 | 契约 |
| --- | --- |
| `channel.feishu.bind_agent {config_id, app_id, app_secret}` | 校验配置后将同一 `config_id` 写入 Feishu 应用项的 `persistent_agent_id`，重启飞书渠道；不新建 Agent 或连接登记 |
| `channel.dingtalk.bind_agent {config_id, client_id, client_secret, robot_code?}` | 校验配置后返回同一 `config_id`，再保存钉钉绑定；不创建 Agent、版本或 workspace；旧连接未完成请求阻止切换 |
| `channel.feishu.unbind_agent {app_id}` / `channel.dingtalk.unbind_agent {client_id}` | 删除该应用条目，仅当它绑定了 Agent；登记与数据保留；条目不存在时成功 |

AgentServer 拒绝时，Gateway 原样返回其错误码；无法回答时返回 `SERVICE_UNAVAILABLE`，不写 YAML。

内部 E2A 方法（Gateway → AgentServer，不对 Web 公开）：

| 方法 | 输入 | 成功结果 |
| --- | --- | --- |
| `jiuwenmate.connection.attach` | `config_id`、`channel`（`feishu` / `dingtalk`）、`connection_key` | `{agent_id}`；两平台都返回 `config_id`，不写新连接表；历史连接仍有排队、执行或暂停请求时返回 `CONFLICT` |
| `jiuwenmate.colleague.connections` | `config_id` | `{connections: [{channel, connection_key, agent_id}], removed}`，含 YAML 已不引用的登记；已隐藏的同事照常列出；已被 `purge` 删除的同事为空列表且 `removed` 为真 |
| `jiuwenmate.colleague.remove` | `config_id`、`mode` | `{removed: true}`；对已隐藏同事的 `unbind`、对已删除同事的两种模式直接成功 |

失败时 `ok=false`，载荷为 `{error, code}`：配置不存在为 `NOT_FOUND`（`attach` 对已隐藏的同事也是 `NOT_FOUND`），传入 IM Agent 或不支持的平台为 `BAD_REQUEST`，并发发布或删除期间持续新增连接为 `CONFLICT`，文件未能删除为 `CLEANUP_FAILED`。

## 5. 生命周期

- **添加飞书：** 校验可见配置后返回 `config_id`，Gateway 将其写为 `persistent_agent_id`；不新增 Agent 行、连接登记、版本或 workspace。
- **添加钉钉：** 校验后返回 canonical `config_id`，Gateway 将其写入钉钉应用；不新增独立运行身份。
- **改绑到另一同事：** 应用改指另一同事现有的 canonical Agent；原 Agent 的历史数据保留。旧连接未完成工作须先完成或结束，不自动迁移。
- **解除连接：** 只删除 YAML 应用条目。同一同事再次绑定同一应用时沿用原 Agent。
- **`unbind`：** 同事设为隐藏并停用；全部 Agent、版本、办公目录与会话保留，页面不提供恢复。
- **`purge`：** 逐个清理历史独立 IM Agent，最后清理三渠道共用的 canonical Agent。每个 Agent：先结束其排队与执行中的请求，再清理会话列表、定时任务、CLI 资料、邮件授权、模板、办公目录与窗口，核对目录已不存在，最后在一个事务中删除版本、登记、群访问与同事表行。处理独立连接期间重新读取连接列表，最多三轮，仍有新增时返回 `CONFLICT`。配置行与删除记录一起写入。执行记录（Session、Request、执行事件与投递）、权限审批与群检索游标沿用现有删除规则，不清理。
- **重试：** Gateway 删除的每一步都可重复：已删除的应用条目不会再被找到；AgentServer 一侧按不变量 9 识别已完成的删除。

## 6. 限制与兼容

- 旧飞书、钉钉绑定在重新绑定前仍可使用原登记 Agent；重新绑定后改为 canonical `config_id`，旧数据和长期授权不自动迁移或删除。
- 新式 Feishu 绑定使用配置 Agent；`channels.feishu_enterprise` 的历史手工绑定继续按其 Agent 身份解析，并须通过可见配置校验。
- Gateway 与 AgentServer 各自按同事 ID 串行绑定和删除，服务端删除未完成时 attach 等待；Gateway 超时不会结束服务端的串行保护。钉钉重绑另有跨进程应用锁，与接收回调的校验／持久入队互斥；停止／重载 Stream 前释放应用锁。多个 Gateway 进程同时写同一份渠道配置仍不在支持范围内；按应用解除（`unbind_agent`）不参与按同事的串行。
- 一个同事每个平台最多一个当前应用；Web、飞书和钉钉共用 Agent 工作空间，会话仍按平台及会话身份隔离。
- 已解除连接、已隐藏同事的数据没有查看入口。
- 旧版本代码会把 IM Agent 行当作普通同事列出，且不认识新列和新表，不能直接回滚。

## 7. 验证索引

验证场景与执行记录见 F_02_02 第 8 节，不在本规约维护进度。
