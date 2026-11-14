# Host Diagnostics（宿主诊断）

一个**只读**的应用插件，把当前 Jiuwen 宿主进程的运行时信息暴露成工作区页面与
Gateway 本地 RPC。

## 能力

| 类型 | 内容 |
|---|---|
| 工作区页面 | 「扩展 → Host Diagnostics」（bundled 页面，`position=120`） |
| 本地 RPC | `host.diagnostics.ping` / `.summary` / `.paths` / `.plugins` |
| 管理 RPC | `host.diagnostics.settings.get` / `.update` / `.set_enabled`（`available_when_disabled=true`） |
| WebSocket 路由 | 无 |
| Hook 订阅 | 无 |

## 为什么是只读的

插件运行在 Jiuwen 信任边界内，能读到宿主的进程信息与环境变量。为了把风险压到最低：

* 不写任何宿主状态，唯一会写的变量是它自己的 `HOST_DIAGNOSTICS_ENABLED`；
* 环境变量默认**不下发**，需要显式打开 `include_env`；
* 敏感变量（名字含 `KEY`/`TOKEN`/`SECRET`/`PASSWORD`/`AUTH`/`COOKIE`/`SESSION` 等）
  的值一律替换为 `******`，只回传变量名；
* 环境变量条目数有硬上限（默认 64，最大 512），避免一次性把整份环境搬到浏览器；
* 所有方法都是 `local_only`，只由 Gateway 自己应答，绝不会转发给 AgentServer；
* 任何采集异常都被吞掉并降级为 `null`，不会把异常抛回通道循环。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `HOST_DIAGNOSTICS_ENABLED` | `true` | 设为 `0`/`false`/`no`/`off` 可禁用插件 |
| `HOST_DIAGNOSTICS_INCLUDE_ENV` | `false` | 是否默认带上环境变量快照 |

运行时可通过 `host.diagnostics.settings.update` 调整 `include_env` 与 `env_limit`，
通过 `host.diagnostics.settings.set_enabled` 切换启用状态。

## RPC 返回示例

`host.diagnostics.summary`：

```json
{
  "platform": { "system": "Windows", "release": "11", "machine": "AMD64", "hostname": "..." },
  "python":   { "version": "3.13.15", "implementation": "CPython", "frozen": false },
  "process":  { "pid": 1234, "parent_pid": 1000, "cwd": "...", "uptime_seconds": 12.5, "threads": 9 }
}
```

`host.diagnostics.plugins`：

```json
{
  "application_plugins": [
    { "plugin_id": "host-diagnostics", "name": "Host Diagnostics", "version": "1.0.0",
      "package_type": "application", "permissions": [], "enabled": true }
  ],
  "typed_extensions": {
    "agent_server_client": null,
    "third_agent": null,
    "crypto_utility": null
  }
}
```

`typed_extensions` 只回传已注册扩展点的**类名**，用于运维判断「南向客户端装了没」，
不做任何内部结构反射。

## 目录结构

```
host_diagnostics/
├── extension.yaml            # 清单（id / 版本 / 权限）
├── extension.py              # 入口：register_extensions + 插件类
├── backend/
│   ├── collector.py          # 纯标准库采集层（可脱离运行时单测）
│   └── handlers.py           # RPC 注册与参数校验
├── frontend/
│   ├── index.tsx             # bundled 页面（导出 applicationPluginId + default）
│   ├── HostDiagnosticsPanel.tsx
│   └── hostDiagnostics.css
└── docs/README.zh-CN.md
```

`backend/` 不引入任何第三方依赖，因此该插件可以在没有 `openjiuwen` / `fastapi`
的环境里被导入和测试。
