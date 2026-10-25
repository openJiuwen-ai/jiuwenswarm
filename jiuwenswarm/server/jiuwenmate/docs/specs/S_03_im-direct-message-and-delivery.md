# IM 单聊绑定与结果投递规约

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | S_03 / im-direct-message-and-delivery |
| 关联模块 | Gateway 钉钉渠道与投递选择、AgentServer 钉钉入站、jiuwenmate 请求与投递账本 |
| 最近一次修订日期 | 2026-10-26 |
| 关联 feature | [F_21_02 钉钉单聊](../features/F_21_02_dingtalk-direct-message.md)、[F_08](../features/F_08_feishu-group-fanout.md)、[F_09](../features/F_09_web-and-feishu-result-delivery.md) |

本规约维护 Swarm 宿主的钉钉单聊接线契约，不改变 PA 执行协议，也不改变飞书入站与绑定契约。状态：本地实现；源码与替身验证通过不等于平台联调通过，证据见 F_21_02。

## 1. 范围、术语与适用条件

适用于企业内部应用机器人的钉钉单聊文本及文本结果投递。群聊、附件、文件与媒体发送、用户授权 CLI、第三方企业应用不在范围内。

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
| Session 键 | `im_direct`，channel `dingtalk`，key 为 JSON `[client_id, conversationId]` |
| `source_ref` | `dingtalk:message:` + JSON `[client_id, msgId]`；msgId 为业务消息 ID，不用 Stream 推送 ID |
| 发送目标 | 受理时的 senderStaffId；不回退加密 senderId |
| 路由快照 | `channel=dingtalk`、`app_id`、`target_id`、`source_message_id` 均必填，受理后不随配置变化 |

## 3. 不变量

1. **入站接管：** JM 应用的全部入站由 JM 认领；拒绝、失败或不支持都不进入普通 Agent，也不下载附件、不写 `last_*`。
2. **身份完整：** 只受理带 msgId、conversationId、senderStaffId 与非空文本的单聊；事件带 robotCode 时须与配置（缺省为 client_id）一致；`senderCorpId` 与 `chatbotCorpId` 都存在时必须相同；`allow_from` 非空时按 staffId 检查。
3. **连接不复用：** 钉钉绑定接口与入站都拒绝已绑定飞书（含停用绑定）的 Agent，以及已有群会话、其他平台单聊或其他钉钉应用单聊的 Agent；绑定时由 AgentServer 依据已有会话判断，无法判断时拒绝绑定。飞书路径不做对应检查：一个已服务钉钉的 Agent 被绑定到飞书后，钉钉入站被拒，但飞书照常运行且可读到该 Agent 原有的钉钉输出文件。该缺口由连接登记统一关闭，见第 6 节合入依赖。
4. **普通应用唯一：** 最多一个普通应用；第二个普通应用的保存被拒绝。
5. **默认应用：** 唯一的普通应用是默认应用；没有普通应用时只保留已标记为默认的应用，不按位置提升 JM 应用。默认应用路由键为 `default`，其余为 client_id。找不到默认应用时，不带应用身份的钉钉消息被丢弃，不回退到任一实例。
6. **cron 身份：** 默认应用换成另一 client_id 或不再存在时，`last_*` 从配置文件删除；密钥轮换保留。
7. **精确投递：** 钉钉结果与等待提示只经 client_id 等于路由 `app_id` 的通道发送（默认应用的路由键为 `default` 不影响匹配）；飞书结果按原规则匹配且不选钉钉通道。
8. **投递安全：** 只有被 final/paused 引用并持久化的候选输出进入账本；unknown 不自动重发；投递失败不重跑 Agent；`delivered` 只表示平台接受发送。

## 4. 接口契约

- `channel.dingtalk.bind_agent(client_id, client_secret, persistent_agent_id, robot_code?)`：创建或更新该应用的绑定；同一 Agent 原有的其他钉钉应用被移除；绑定普通应用即把它转为 JM；新应用以空白名单创建，其他应用的已保存字段（含格式错误的白名单）不变。保存前经内部方法 `jiuwenmate.dingtalk.binding_check(agent_id, app_id)` 询问 AgentServer，返回 `{allowed}`。违反不变量 3、4 返回 `BAD_REQUEST`；AgentServer 无法回答时返回 `SERVICE_UNAVAILABLE` 且不保存。落盘成功但通道重启失败返回 `CHANNEL_APPLY_FAILED`。
- `channel.dingtalk.get_bindings`：返回有效绑定的 Agent ID，不返回凭据。
- `channel.dingtalk.get_conf`：返回共享键与归一化的 `apps[]`。
- `channel.dingtalk.set_conf({apps})`：只接受 `apps[]`；每项先按 client_id 合并到已保存的应用，再推导与校验，所以未提交的字段（含 `enabled`、应用级 API 地址、白名单、`persistent_agent_id`、`robot_code`）保持原值，空的 `client_secret` 表示不变；未保存过的 client_id 没有原值，必须提交 `allow_from`，否则返回 `BAD_REQUEST`；默认标志由服务端推导。列表中不存在的应用被删除，不影响其他应用和 Agent 数据。
- 保存语义（`set_conf` 与 `bind_agent` 共用）：在配置写锁内以文件中的 `channels.dingtalk` 为基础合并、校验并写回，再用写回结果重启钉钉通道；重启串行执行，每次在旧实例停止后读取最新配置，停止旧实例不按 channel_id 注销，因此重叠的重启不会让已保存的应用缺少通道；并发保存不会互相覆盖，普通渠道写入文件的 `last_*` 不会被内存中的旧值覆盖。`bind_agent` 的 AgentServer 检查在读取配置之前完成。
- `jiuwenmate.dingtalk.inbound`、`jiuwenmate.dingtalk.binding_check`：Gateway 到 AgentServer 的内部 E2A 方法，不对 Web 公开。入站的处置结果仅用于日志。
- 投递推送：载荷携带 `channel`、`app_id`、`chat_id`、`is_direct_message`；缺少 `channel` 的旧推送按飞书处理。ACK 信封渠道与推送一致。

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

合入依赖：不变量 3 的飞书侧缺口会让飞书读取已服务钉钉的 Agent 的工作文件。在配置聚合层的连接登记交付前，本规约所述钉钉单聊不应单独合入发布；或须由飞书负责人在飞书绑定处补充对应检查。

## 7. 验证索引

组件、共享接线（替身）与真实平台分别验收，命令与结果记录在 F_21_02 第 8 节；不在本规约维护进度。
