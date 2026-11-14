# Notes Vault（速记保险箱）

按**会话隔离**的速记插件：每个聊天会话有自己的一份笔记，落盘为 JSON，
并提供 Markdown 导出。

## 能力

| 类型 | 内容 |
|---|---|
| 工作区页面 | 「扩展 → Notes Vault」（bundled 页面，`position=130`） |
| 本地 RPC | `notes.list` / `.get` / `.create` / `.update` / `.delete` / `.stats` / `.export` |
| 管理 RPC | `notes.settings.get` / `.update` / `.set_enabled`（`available_when_disabled=true`） |
| WebSocket 路由 | 无 |
| Hook 订阅 | 无 |

## 数据模型

每个 scope（默认取调用方的 `session_id`）对应一个 JSON 文件：

```json
{
  "version": 1,
  "scope": "<session_id>",
  "notes": [
    {
      "id": "9f2c1a4b7e30",
      "title": "周会要点",
      "body": "- 插件机制要写文档\n- 前端补测试",
      "created_at": "2025-01-01T08:00:00Z",
      "updated_at": "2025-01-01T08:05:00Z",
      "tags": ["meeting", "swarm"],
      "pinned": false
    }
  ]
}
```

## 存储位置与配置

优先级：插件 `config.yaml` → 环境变量 → `~/.jiuwenswarm/notes_vault`。

插件目录下的 `config.yaml`（可选）：

```yaml
storage_dir: /var/lib/jiuwenswarm/notes
max_notes: 100
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `NOTES_VAULT_ENABLED` | `true` | 设为 `0`/`false`/`no`/`off` 可禁用插件 |
| `NOTES_VAULT_DIR` | `~/.jiuwenswarm/notes_vault` | 覆盖存储目录 |

`config.yaml` 解析失败**不会**导致启动失败：插件会退回默认值，避免一个笔误
把 Gateway 拖down。

## 安全与健壮性

* **scope 白名单**：scope 必须首字符为字母/数字，且只含 `[A-Za-z0-9._-]`，
  显式拒绝 `..`，并在解析后再次校验目标路径仍在存储目录内——双重防目录穿越；
* **原子写**：先写 `<scope>.json.tmp` 再 `os.replace()`，进程被杀不会留下半截文件；
* **字段校验**：标题 ≤200、正文 ≤20000、标签 ≤16 个且每个 ≤32 字符、
  每 scope 笔记数 ≤200（默认），越界返回 `BAD_REQUEST` 而不是 500；
* **损坏文件不静默丢数据**：JSON 解析失败会返回明确错误，由使用者决定如何处置，
  而不是当作空库覆盖掉；
* **时间戳**：使用 `datetime.now(timezone.utc)` 而非已废弃的 `datetime.utcnow()`，
  以适配项目 pytest 的 `filterwarnings = error`；
* **所有方法 `local_only`**，不转发给 AgentServer。

## 目录结构

```
notes_vault/
├── extension.yaml            # 清单（id / 版本 / config_schema）
├── extension.py              # 入口：register_extensions + 插件类
├── backend/
│   ├── store.py              # 纯标准库持久化层（可脱离运行时单测）
│   └── handlers.py           # RPC 注册、scope 解析与参数校验
├── frontend/
│   ├── index.tsx             # bundled 页面（导出 applicationPluginId + default）
│   ├── NotesVaultPanel.tsx
│   └── notesVault.css
└── docs/README.zh-CN.md
```

`backend/` 不引入任何第三方依赖，因此该插件可以在没有 `openjiuwen` / `fastapi`
的环境里被导入和测试。
