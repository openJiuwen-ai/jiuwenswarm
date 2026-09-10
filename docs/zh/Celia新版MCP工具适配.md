# Celia 提示词与 GaussPD MCP 工具

Swarm 使用通用 MCP 流程中已注册的 `gausspdmcp` 服务。模型收到的工具名为 `mcp_gausspdmcp_*`，工具描述、参数 schema 和执行均沿用该服务。

## 挂载入口

`memory.external.provider: celia` 现在只挂载 `CeliaMcpPromptRail`，负责注入静态 Memory 段，优先级仍为 15。普通、代码、设计和集群模式共用此入口；模式切换后恢复提示词，卸载时移除该段。

旧 `CeliaMemoryProvider` 和 `CeliaMemoryRail` 不再从这个入口创建，因此不注册无前缀的 `memory_*` 工具，不启动旧私有 MCP 客户端。旧模块保留在仓库中，本次不做模块清理。

## 配置与职责

```yaml
memory:
  engine: external
  external:
    provider: celia
```

GaussPD 服务继续使用部署环境已有的 `mcp.servers` 配置，其服务名称为 `gausspdmcp`。自动摘要读取和对话入库由现有 GaussPD 接入负责；Swarm 的提示词 Rail 不再重复执行。现有扩展 Hook、参数处理、连接管理及工具白名单保持原有流程。

旧 Celia 私有客户端的配置不参与新的提示词挂载。没有新增连接适配层、schema 表或生命周期切换配置。

## 验证

测试使用真实 DeepAgent 和 MCP 工具执行器，验证模型仅收到已注册的 GaussPD 工具、参数 schema 与服务端一致、工具能够执行，以及移除提示词 Rail 后 MCP 工具仍然可用。同时覆盖各模式挂载、流式请求、模式切换和 MCP 缺失场景，并断言旧客户端与预检查均未调用。

实际 GaussPD 数据库和自动记忆效果仍需部署环境联调。
