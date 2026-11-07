# IM 消息绑定与结果投递规约

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | S_03 / im-direct-message-and-delivery |
| 关联模块 | Gateway 钉钉渠道与投递选择、AgentServer 钉钉入站、jiuwenmate 请求与投递账本 |
| 最近一次修订日期 | 2026-10-31 |
| 关联 feature | [F_21_02 钉钉单聊](../features/F_21_02_dingtalk-direct-message.md)、[F_02_02 配置与连接](../features/F_02_02_colleague-connections.md)、[F_08](../features/F_08_feishu-group-fanout.md)、[F_09](../features/F_09_web-and-feishu-result-delivery.md) |

本规约维护 Swarm 宿主的钉钉单聊、群聊接线契约，不改变 PA 执行协议。同事、连接与运行 Agent 的关系及飞书、钉钉共用的连接检查见 [S_04](S_04_colleague-connections.md)。群聊回复模式复用 [F_23](../features/F_23_group-reply-mode.md)。源码与替身验证通过不等于平台联调通过，证据见 F_21_02。

## 1. 范围、术语与适用条件

适用于企业内部应用机器人的钉钉单聊、群聊文本及文本结果投递，并维护员工 OAuth 群历史摄入与此消息入口的交接。用户授权和 CLI 执行细节见 [S_06](S_06_employee-dingtalk-cli.md)。附件、文件与媒体发送、第三方企业应用不在本规约范围内。

- **应用**：`channels.dingtalk.apps[]` 中的一项，以 `client_id` 标识。
- **JM 应用**：配置了 `persistent_agent_id` 的应用；其余为普通应用。
- **默认应用**：承接不带应用身份的普通消息（cron、心跳）的应用。
- **连接**：一个应用与一个 Agent 的绑定。隔离单位是连接。

## 2. 数据结构与事实归属

| 事实 | 权威位置 |
| --- | --- |
| 应用、凭据、绑定、robot_code | `channels.dingtalk.apps[]`；旧平铺配置读取时视为单个默认应用，首次保存迁入列表 |
| 共享键 | `channels.dingtalk` 顶层：API 地址、文件设置及默认应用的 `last_*` |
| Session / Request / 投递状态 | `ExecutionRepository` 现有表，未新增表 |

字段取值：文本字段为字符串；YAML 空值按空字符串处理。`allow_from` 必须是字符串列表，未设置或为空值时视为空列表（不限制）；其他类型或含非字符串项属于配置错误：原值保留，该应用不启动；明确提交的非法白名单被拒绝，未提交白名单的编辑保留原值，绝不按空列表放行。设置页对这类应用显示需修正的状态，用户重新填写前不提交白名单，也不能更换 client_id。新增应用（含更换 client_id 后的应用）必须明确提交 `allow_from`，省略即拒绝保存。未设置的 `enabled` 在凭据齐全时视为启用。

身份与路由：

| 对象 | 规则 |
| --- | --- |
| 接收应用 | 来自收到事件的通道实例的 client_id，不由消息内容决定 |
| Session 键 | 单聊为 `im_direct`，channel `dingtalk`，key 为 JSON `[client_id, conversationId]`；群聊使用 `group_chats` 中按 namespace、平台和 `conversationId` 解析的群身份 |
| `source_ref` | `dingtalk:message:` + JSON `[client_id, msgId]`；msgId 为业务消息 ID，不用 Stream 推送 ID |
| 发送目标 | 单聊为受理时的 `senderStaffId`；群聊为受理时的 `conversationId`；不回退加密 senderId |
| 路由快照 | `channel=dingtalk`、`app_id`、`target_id`、`source_message_id` 均必填，受理后不随配置变化 |

## 3. 不变量

1. **入站接管：** JM 应用的全部入站由 JM 认领；拒绝、失败或不支持都不进入普通 Agent，也不下载附件、不写 `last_*`。
2. **身份完整：** 机器人回调只受理带 msgId、conversationId、senderStaffId 与非空文本的单聊或群聊；事件带 robotCode 时须与配置（缺省为 client_id）一致；`senderCorpId` 与 `chatbotCorpId` 都存在时必须相同；`allow_from` 非空时按 staffId 检查。宿主历史读取入口按第 8 节保留开放身份，不冒充 staffId。
3. **连接不复用：** 每个钉钉应用由同事专属的运行 Agent 服务。入站只受理 YAML 绑定到、且登记为本钉钉应用连接的 Agent，其余一律拒绝（S_04 不变量 2）；飞书入站执行同一检查。
4. **普通应用唯一：** 最多一个普通应用；第二个普通应用的保存被拒绝。
5. **默认应用：** 唯一的普通应用是默认应用；没有普通应用时只保留已标记为默认的应用，不按位置提升 JM 应用。默认应用路由键为 `default`，其余为 client_id。找不到默认应用时，不带应用身份的钉钉消息被丢弃，不回退到任一实例。
6. **cron 身份：** 默认应用换成另一 client_id 或不再存在时，`last_*` 从配置文件删除；密钥轮换保留。
7. **精确投递：** 钉钉结果与等待提示只经 client_id 等于路由 `app_id` 的通道发送（默认应用的路由键为 `default` 不影响匹配）；单聊走私聊发送接口，群聊走群消息发送接口并投递到冻结的群 `target_id`；飞书结果按原规则匹配且不选钉钉通道。
8. **投递安全：** 只有被 final/paused 引用并持久化的候选输出进入账本；unknown 不自动重发；投递失败不重跑 Agent；`delivered` 只表示平台接受发送。

## 4. 接口契约

- `channel.dingtalk.bind_agent(client_id, client_secret, config_id, robot_code?)`：先经内部方法 `jiuwenmate.connection.attach` 取得该同事这个应用的运行 Agent（S_04），再把它写入应用的 `persistent_agent_id`；绑定普通应用即把它转为 JM；新应用以空白名单创建，其他应用的已保存字段（含格式错误的白名单）不变。AgentServer 拒绝时原样返回其错误码，无法回答时返回 `SERVICE_UNAVAILABLE`，都不保存；违反不变量 4 返回 `BAD_REQUEST`；落盘成功但通道重启失败返回 `CHANNEL_APPLY_FAILED`。
- `channel.dingtalk.unbind_agent(client_id)`：删除该应用条目，仅当它绑定了 Agent；其运行 Agent 与数据保留。
- `channel.dingtalk.get_conf`：返回共享键与归一化的 `apps[]`。
- `channel.dingtalk.set_conf({apps})`：只接受 `apps[]`；每项先按 client_id 合并到已保存的应用，再推导与校验，所以未提交的字段（含 `enabled`、应用级 API 地址、白名单、`persistent_agent_id`、`robot_code`）保持原值，空的 `client_secret` 表示不变；未保存过的 client_id 没有原值，必须提交 `allow_from`，否则返回 `BAD_REQUEST`；默认标志由服务端推导。列表中不存在的应用被删除，不影响其他应用和 Agent 数据。
- 保存语义（`set_conf` 与 `bind_agent` 共用）：在配置写锁内以文件中的 `channels.dingtalk` 为基础合并、校验并写回，再用写回结果重启钉钉通道；重启串行执行，每次在旧实例停止后读取最新配置，停止旧实例不按 channel_id 注销，因此重叠的重启不会让已保存的应用缺少通道；并发保存不会互相覆盖，普通渠道写入文件的 `last_*` 不会被内存中的旧值覆盖。`bind_agent` 取得运行 Agent 在读取配置之前完成。
- `jiuwenmate.dingtalk.inbound`：Gateway 到 AgentServer 的内部 E2A 方法，不对 Web 公开。入站的处置结果仅用于日志。连接相关的内部方法见 S_04。
- 投递推送：载荷携带 `channel`、`app_id`、`chat_id`、`is_direct_message`；缺少 `channel` 的旧推送按飞书处理。ACK 信封渠道与推送一致。
- 已确认的群聊文本回复写入该平台群的公开记录，平台回执作为消息 ID，源消息作为 `reply_to`；飞书与钉钉均支持重启后只重试归档、不重发消息。钉钉 processQueryKey 与用户历史 messageId 不同；完整有界回读后，仅对确认同一机器人、相同非空无附件文字、时间差不超过十秒且双向唯一的回执/回读记录归并，保留真实 messageId/平台时间及本地员工身份和 reply_to。两侧各自前后十秒必须处于完整读取窗口内，歧义或未覆盖窗口不归并；delivery-echo 来源阻止归档重试复活回执重复行，原发送回执保留在投递日志中，不触发重发。

`send_persistent_result` 的钉钉结果分类：

| 观察结果 | 结果 |
| --- | --- |
| 通道未注册而应用仍在配置中且启用 | pending |
| 应用已删除或停用 | failed（`DINGTALK_APP_NOT_CONFIGURED`） |
| Token 接口 400/401/403 | failed（`DINGTALK_CREDENTIAL_REJECTED`） |
| Token 接口不可达 | pending，30 秒内不再请求 |
| 缓存 Token 发送收到 401 | pending 一次并清除缓存；新 Token 仍 401 则 failed |
| 连接建立前失败 | pending |
| 带错误码的 4xx、401/403/404/413/422/429，目标在 invalid、flowControlled 或 filtered 名单 | failed |
| 200、有效 `processQueryKey`、目标未被拒绝 | delivered，句柄作为不透明 `external_message_id` |
| 读超时、中断、5xx、408、`unknown.send.result`、无错误码的其他 4xx、成功缺少句柄 | unknown |
| 文件或 @ 交接 | failed（`DINGTALK_DELIVERY_UNSUPPORTED`），不读取文件 |

## 5. 生命周期与恢复

Stream 回调把认领的事件调度到后台后立即 ACK；ACK 不代表受理落库，期间崩溃会丢失消息，不承诺恰好一次。

飞书或钉钉任一存在有效绑定时启动现有 IM manager；IM manager 负责 `feishu`、`dingtalk` 两个渠道的 Request 恢复、投递认领和 `sending` 置 unknown。Web manager 不变，两者并发计数各自独立。

## 6. 兼容与限制

未绑定钉钉应用、普通文件与 cron 行为保持原状。已保存 `dingtalk` 路由的 Request 不能被不认识该渠道的旧版本解析。真实钉钉的重推规则、会话 ID 稳定性、robotCode 对应、401 与限流语义仍待联调。

合入依赖（2026-10-26 已关闭）：原先飞书路径不检查跨连接复用，会读取已服务钉钉的 Agent 的工作文件。S_04 的连接登记让飞书入站执行同一检查，本规约与 S_04 一同生效。

## 7. 验证索引

组件、共享接线（替身）与真实平台分别验收，命令与结果记录在 F_21_02 第 8 节；不在本规约维护进度。

## 8. 钉钉普通群文字的保守摄入

- 仅对已登记且访问权 active、员工启用、精确连接仍有效并开启 adaptive 的群周期读取。使用员工自己的 OAuth；同 namespace、同授权账号摘要、同群合并读取，回复仍由路由中的应用机器人发回原群。
- 每次读取前核对实时群机器人列表的 robotCode。不能将 openBotId 当作 robotCode；机器人缺席时撤销相应缓存成员权并停止摄入。查询失败不能撤销权限或使用旧缓存继续读取。新机器人回调可恢复成员权和可见起点，旧回调不能推翻较新的离群证据。
- 历史入口独立于机器人回调，不能凭用户能读群创建机器人群成员权。senderOpenDingTalkId 以 `dingtalk-open:` 命名空间保存，不进入 staffId-only 日历、群文件上传或 DOCX 编辑路径；受限员工白名单不能靠猜测身份映射绕过。
- 已验证人类发送、正文不含 ASCII 或全角 @、创建时间晚于该次观察执行下限的新增文字，才复用既有 Session/Request/scheduler 参与判断。机器人消息、旧消息、含 @ 文字只归档；passive 不主动参与。此为文本保守过滤，不保证完整提及语义。
- 首次出现或恢复权限前的历史只作上下文。进度私有原子持久化，成功处理后才前移，完整批次保留 30 秒重叠；部分结果不冒充完整。跨来源保留平台消息 ID，按现有每员工 source_ref 幂等受理，迟到的可信机器人回调优先于历史展示。
- 钉钉近期上下文首屏已读到非空消息、且唯一的不完整原因是已知采集缺口时，宿主返回 `found` 和“仅已观测消息”的原因，允许参与判断使用这些事实。`found` 不承诺全历史完整；归档缺口不清除，空结果、游标错误、后续分页及搜索的完整性语义不变。系统提示必须说明未观测到的发言不能当作不存在或同意。
- 轮询归属共享 IM runtime，关闭时先取消并等待轮询任务，再关闭 Manager；不增加 PA 协议或个人版入口。

English summary: Employee-authorized polling accepts only new human text without ASCII/full-width @ for adaptive participation. Robot replies retain each employee's bot identity. Live bot membership, scoped reader identities, persistent overlapping checkpoints and existing request deduplication protect ingestion; this conservative fallback does not provide complete mention semantics.
