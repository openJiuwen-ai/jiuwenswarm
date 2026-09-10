# Celia 提示词与 GaussPD MCP 工具

Swarm 使用通用 MCP 流程中已注册的 `gausspdmcp` 服务。模型收到的工具名为 `mcp_gausspdmcp_*`，工具描述、参数 schema 和执行均沿用该服务。

## 挂载入口

`memory.external.provider: celia` 现在只挂载 `CeliaMcpPromptRail`，负责注入静态 Memory 段，优先级仍为 15。普通、代码、设计和集群模式共用此入口；模式切换后恢复提示词，卸载时移除该段。

旧 `CeliaMemoryProvider`、`CeliaMemoryRail`、私有 MCP 客户端及静态 schema 已删除，无前缀的 `memory_*` 工具不再注册。

## 配置与职责

```yaml
memory:
  engine: external
  external:
    provider: celia
```

GaussPD 服务继续使用部署环境已有的 `mcp.servers` 配置，其服务名称为 `gausspdmcp`。自动摘要读取和对话入库由现有 GaussPD 接入负责；Swarm 的提示词 Rail 不再重复执行。现有扩展 Hook、参数处理、连接管理及工具白名单保持原有流程。

配置模板已移除旧 Celia 私有客户端参数，仅保留小艺记忆开关使用的 `celia.runtime_state_path`。小艺的状态文件、记忆查询和历史展示继续保留；初始化不再创建旧客户端的二进制目录、数据库或日志，已有用户数据仍受保护。

## 验证

测试使用真实 DeepAgent 和 MCP 工具执行器，验证模型仅收到已注册的 GaussPD 工具、参数 schema 与服务端一致、工具能够执行，以及移除提示词 Rail 后 MCP 工具仍然可用。同时覆盖各模式挂载、流式请求、模式切换和 MCP 缺失场景，以及小艺记忆开关、展示和强制初始化时的数据保留。

实际 GaussPD 数据库和自动记忆效果仍需部署环境联调。
