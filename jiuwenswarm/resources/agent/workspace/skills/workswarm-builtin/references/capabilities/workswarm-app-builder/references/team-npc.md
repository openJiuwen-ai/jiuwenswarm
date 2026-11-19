# Team NPC 接入指南

以海岛议事为例，玩家只和镇长对话。镇长、渔夫和医师都可以在同一轮以各自身份发言。应用服务端管理一个 Team 会话，宿主为各角色保留各自的上下文。页面只接收应用投影出的角色发言和回合状态。

## 定义角色与创建会话

先定义谁接收玩家输入、哪些角色需要发言、各自知道什么，以及输出格式。leader 的 `title` 和 `role_instructions` 描述 leader 本人。成员用稳定的 `name` 标识，`title` 用作署名，`desc` 描述可供 leader 分工的专长，`role_instructions` 描述该成员自己的发言方式。让每个角色输出自己的台词。应用需要结构化数据时，为每个角色分别定义并校验 schema。

应用服务端通过 `WORKSWARM_GATEWAY_API_URL` 连接 Web Gateway，等待 `connection.ack`。用 `WORKSWARM_SOURCE_SESSION` 调用 `session.get_metadata`，将来源会话返回的 `project_id` 和 `work_mode` 填入创建请求。以下使用 code 项目作为示例，实际取值由来源项目决定。

```json
{
  "type": "req",
  "id": "create-island-council",
  "method": "session.create",
  "params": {
    "create_token": "stable-token-for-this-binding",
    "execution_profile": "app_role",
    "mode": "team",
    "project_id": "<来源项目 ID>",
    "work_mode": "code",
    "persist_session": false,
    "title": "镇长",
    "role_instructions": "你是岛上镇长。安排渔夫和医师各自就本轮场景发言。最终输出只写镇长自己的台词。",
    "npc_team": {
      "members": [
        {"name": "fisherman", "title": "渔夫", "desc": "了解海况和码头", "role_instructions": "以渔夫身份给出自己的观察，直接输出台词。"},
        {"name": "doctor", "title": "医师", "desc": "负责岛上的救护", "role_instructions": "以医师身份说出自己的判断，直接输出台词。"}
      ]
    }
  }
}
```

- 名册含 1–5 个成员。`name` 匹配 `^[a-z][a-z0-9_]{1,31}$`、不重复，避开 `leader`、`team_leader`、`human_agent`、`user`。每个角色的 `role_instructions` 为 1–12000 字符。
- 检查匹配创建请求的成功响应，保存真实 `session_id`、返回的 `mode` 和创建时的 `work_mode`。确认 `execution_profile` 为 `app_role`，且 `mode` 是 `team.code.normal` 或 `team.work.normal`。能力确认失败时提示实际错误或升级宿主。原生 Team 的成员由宿主在首次对话时装配，应用只保管这一份绑定。
- 对同一个待创建绑定复用 `create_token`。角色规则与名册是创建参数，修改后应按新绑定创建，并走原有清理流程。团队玩法使用 `mode:"team"`，不使用旧版轻量咨询的 `ask_<name>` 或 `npc.member`。

## 提交玩家输入

应用页面只调用应用自己的同源业务接口。服务端在发送前登记进行中的回合，并保持一个 Gateway 接收循环，将响应和事件分别分发。带 `session_id` 的请求会改变 WebSocket 的会话路由。活动回合期间，查询其他会话的元数据等操作使用独立连接。以下是发送到 Gateway 的帧：

```json
{
  "type": "req",
  "id": "turn-42",
  "method": "chat.send",
  "is_stream": true,
  "params": {
    "session_id": "<创建响应的 Team 会话 ID>",
    "mode": "team.code.normal",
    "work_mode": "code",
    "query": "{\"player_text\":\"今晚的船能靠岸吗？\",\"scene\":{\"weather\":\"大雾\"}}"
  }
}
```

`query` 使用玩家文本或序列化后的场景 JSON 字符串。每轮只传当前需要的场景变化；角色规则已在创建时固定。沿用创建返回的模式。成员任务由 leader 在 Team 内安排，应用发送一次 `chat.send` 即可开始一轮。页面只有面向 leader 的输入入口。

## 收取并展示角色发言

以下摘出同一轮的两条事件。渔夫先发言，回合结束时医师缺席。发言事件中的 `request_id` 仍可能来自前一轮订阅。

```json
[
  {"type": "event", "event": "chat.final", "payload": {"session_id": "team-session", "request_id": "turn-41", "turn_request_id": "turn-42", "member_name": "fisherman", "display_name": "渔夫", "content": "我看不清岸标，先让船在外海等一等。"}},
  {"type": "event", "event": "chat.processing_status", "payload": {"session_id": "team-session", "turn_request_id": "turn-42", "is_processing": false, "npc_missing_members": ["doctor"]}}
]
```

在服务端处理事件，然后只向页面输出发言和必要状态。

| 收到的帧 | 应用行为 |
|---|---|
| 对应请求的成功 `res` 或 `accepted` | 保持等待。发言和结束状态由后续事件驱动 |
| 当前 Team 的 `chat.final` | 使用 `payload.turn_request_id` 关联玩家回合，缺省时仅接受与在途请求匹配的 `payload.request_id`。取预期角色的非空 `content`，以 `member_name` 归属角色、以 `display_name` 署名。leader 标识为 `team_leader`。按到达顺序分别显示 |
| 同一回合的 `chat.processing_status` 且 `is_processing:false` | 结束等待。用 `npc_missing_members` 或预期角色集合检查缺席。保留已收到的真实发言并标记回合不完整 |
| 当前请求失败、`chat.error` 或超时 | 显示适当的失败状态。保留已经收到的发言，回合状态与业务提交结果分开记录 |
| 推理、增量文本、工具、任务、内部消息 | 留在宿主侧，应用只转发最终文本和必要状态 |

实现时用 `session_id + turn_request_id` 隔离不同会话和回合。同一 Team 流可能跨轮复用，外层请求 ID 可能属于旧订阅，所以优先使用事件的 `turn_request_id`。每条发言独立渲染，先到的成员可以先显示，首条 `chat.final` 不触发回合完成。保留角色身份与原文；相同台词可能来自不同角色。用页面组件的纯文本渲染能力处理模型输出。

每个绑定同时只提交一轮，应用在服务端拦截多标签页的并发提交。掉线或等待超时后，先判断旧回合状态或发送 `chat.interrupt` 并确认停止，再允许显式重试。相同业务操作使用应用自己的操作 ID 去重，Gateway 请求 ID 用于关联传输。清除整队上下文时中断并删除旧 Team 会话，确认后再建新绑定。退出清理同样只针对应用保存的 Team 会话 ID。

## 验收

1. 从市场启动应用，用一主两从的真实名册创建会话，确认创建响应和实际发送的 Gateway 帧。每次玩家操作只发一条 `chat.send`。
2. 连续对话至少两轮。核对三个角色各自的 `member_name`、署名和原始最终文本。确认首条发言到达后仍保持等待，回合结束后才解除输入锁。
3. 用事件样本重放成员先于 leader、旧轮迟到、某成员缺席和回合出错。检查页面只出现当前轮真实发言，缺席或失败时已有发言仍可见。
4. 验证刷新复用、回合取消、重开和停止。确认旧会话不再接收提交，已删除绑定不被复用。

验收报告区分真实 Gateway 与模型跑通、事件样本验证和页面检查。需要宿主代码契约时，查阅宿主仓库的 `docs/zh/AppRoleSessions.md`。
