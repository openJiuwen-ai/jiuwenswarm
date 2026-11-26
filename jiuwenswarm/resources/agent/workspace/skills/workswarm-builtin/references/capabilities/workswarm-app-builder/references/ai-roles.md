# AI 角色清单

在入口 HTML 所在目录创建 UTF-8 文件 `app-ai-roles.json`。文件随应用发布，用于市场详情展示和 `ai` 能力声明。

```json
{
  "schema_version": 1,
  "session_groups": [
    {
      "id": "island_council",
      "mode": "team",
      "session_description": "同一局内复用团队会话并保留对话。刷新页面继续当前局。开始新局时新建会话，停止应用后清理会话。",
      "leader": "mayor",
      "roles": [
        {
          "id": "mayor",
          "name": "镇长",
          "description": "接收玩家提问，组织岛民讨论。",
          "avatar": "assets/roles/mayor.png"
        },
        {
          "id": "fisherman",
          "name": "渔夫",
          "description": "根据海况和码头情况给出建议。"
        }
      ]
    }
  ]
}
```

- `session_groups` 至少包含一组。组 ID 在应用内唯一，角色 ID 在组内唯一，应用更新时沿用已有稳定 ID。
- 单角色用 `mode:"agent"`，组内恰好一个角色，省略 `leader`。Team 用 `mode:"team"`，含一位 leader 和 1–5 名成员。`leader` 填对应的角色 ID。
- `session_description` 用用户语言说明何时复用会话与对话历史、刷新能否继续、何时重置，以及应用停止后能否恢复。`persist_session:false` 仍可保留普通多轮历史。
- 每个角色填写 `id`、`name`、`description`。后端使用的业务角色也可声明，例如「摘要助手」。`avatar` 可省略，市场根据角色名展示占位头像。图片使用包内相对路径，支持 PNG、JPEG、WebP 和 GIF，建议每张不超过 256 KiB。
- 清单不超过 64 KiB，只填写可发布的展示信息。角色提示词、实际会话 ID、用户对话和凭据由应用各自管理。

发布前，从已安装应用详情打开「AI 角色」，核对角色卡片、会话组和头像。对照一次刷新、重开和停止的实际结果检查会话简介。
