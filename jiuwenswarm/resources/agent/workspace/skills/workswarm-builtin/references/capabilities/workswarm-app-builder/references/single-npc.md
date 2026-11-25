# 单角色 AI 接入示例

以「阿海」的文本对话为例。页面请求应用自己的同源业务接口，由 `scripts/app_server.py` 管理角色会话和 Gateway 连接。

## 从市场启动

服务端监听分配的 `WORKSWARM_APP_HOST` 和 `WORKSWARM_APP_PORT`。读取 `WORKSWARM_INSTANCE_CONTEXT` 指向的 JSON 文件，取得 `app_id` 和 `instance_id`。准备就绪后，响应带有效 `Authorization: Bearer <WORKSWARM_STATUS_TOKEN>` 的 `GET /_workswarm/status`，返回 HTTP 200 和下列结构。

```json
{"app_id":"<app_id>","instance_id":"<instance_id>","seq":1,"state":"running"}
```

实现同样鉴权的 `POST /_workswarm/shutdown`。停止接受新消息并在 2 秒内返回 HTTP 200，随后在退出宽限期内中断和删除该应用持有的会话，确认后退出。未清理的绑定保存到下次启动完成。页面和同源业务接口都不提供状态令牌。

## 创建与发送

用 `WORKSWARM_GATEWAY_API_URL` 连接 Web Gateway，收到 `connection.ack` 后发请求。示例适用于允许本地连接的个人版。需要登录的部署使用部署提供的 WebChannel 鉴权，身份不可用时明确报错。应用市场的状态令牌仅用于应用的生命周期接口。

项目 ID 和工作模式分别来自 `WORKSWARM_APP_PROJECT_ID` 与 `WORKSWARM_APP_WORK_MODE`。以下示例使用 code 项目。给这次待创建的绑定分配一个稳定 `create_token`，创建超时时用同一个 token 重试。

```json
{
  "type": "req",
  "id": "create-ahai",
  "method": "session.create",
  "params": {
    "create_token": "<stable-create-token>",
    "execution_profile": "app_role",
    "mode": "agent",
    "project_id": "<project-id>",
    "work_mode": "code",
    "persist_session": false,
    "title": "阿海",
    "role_instructions": "你是岛民阿海。用简短中文回答玩家的提问。"
  }
}
```

匹配同 `id` 的成功 `res`。确认 `payload.execution_profile` 为 `app_role`，保存实际 `payload.session_id`、`payload.mode` 及项目工作模式。`mode` 以下以 `agent.code.normal` 为例，发送时使用创建返回的值。

```json
{
  "type": "req",
  "id": "turn-1",
  "method": "chat.send",
  "is_stream": true,
  "params": {
    "session_id": "<session-id>",
    "mode": "agent.code.normal",
    "work_mode": "code",
    "query": "{\"player_text\":\"晚上能出海吗？\",\"scene\":{\"weather\":\"大雾\"}}"
  }
}
```

服务端通过一个接收循环分发 `res` 和事件，按 `session_id` 和 `request_id` 匹配当前回合。活动回合期间，查询其他会话请用独立连接，以免切换当前 WebSocket 的会话路由。下面示例省略了与展示无关的字段。

```json
[
  {"type":"res","id":"turn-1","ok":true,"payload":{"accepted":true,"session_id":"<session-id>"}},
  {"type":"event","event":"chat.delta","payload":{"session_id":"<session-id>","request_id":"turn-1","content":"今晚雾很大。"}},
  {"type":"event","event":"chat.final","payload":{"session_id":"<session-id>","request_id":"turn-1","content":"今晚雾很大。建议等天亮再出海。"}}
]
```

`accepted` 表示受理，`chat.delta` 可供预览，`chat.final` 的文本用于提交业务结果。失败的 `res` 或本轮 `chat.error` 结束等待。应用用 JSON 时，解析并验证最终文本后再改业务状态。

## 超时、重开和验收

1. 等待超时后先检查原回合状态。原回合仍在运行时，发送 `chat.interrupt`，参数使用 `session_id`、实际 `mode`、`work_mode` 和 `intent:"cancel"`。等待对应 `chat.interrupt_result`，并在需要时通过 `session.get_metadata` 确认 `is_processing:false`。
2. 重试使用新请求 ID，保留同一业务操作 ID。应用已保存结果时直接返回已有结果。初始化尚未确认时，重试仍包含必要的初始场景。服务端拒绝同一绑定并发提交，丢弃已取消或过期回合的迟到结果。
3. 新局需要清空上下文时，可取消并删除旧会话，查到不存在后重新创建。刷新仍在运行的应用时按既有绑定复用当前会话。
4. 从市场启动，验证两条真实 AI 请求、一次超时或取消、一次刷新和停止后的清理。核对「AI 角色」详情中的会话简介与实际行为。
