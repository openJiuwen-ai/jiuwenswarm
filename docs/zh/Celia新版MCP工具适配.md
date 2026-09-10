# Celia 提示词与 Celia MCP 工具

Swarm 使用通用 MCP 流程中已注册的 `celiamcp` 服务。模型收到的工具名为 `mcp_celiamcp_*`，工具描述、参数 schema 和执行均沿用该服务。

## 挂载入口

`memory.external.provider: celia` 现在只挂载 `CeliaMcpPromptRail`，负责注入静态 Memory 段，优先级仍为 15。普通、代码、设计和集群模式共用此入口；模式切换后恢复提示词，卸载时移除该段。

旧 `CeliaMemoryProvider` 和 `CeliaMemoryRail` 不再从这个入口创建，因此不注册无前缀的 `memory_*` 工具，不启动旧私有 MCP 客户端。旧模块保留在仓库中，本次不做模块清理。

## 配置与职责

```yaml
memory:
  engine: external
  external:
    provider: celia
mcp:
  servers:
    - name: celiamcp
      enabled: true
      transport: stdio
      command: ${CELIA_MCP_EXE}
```

Celia 服务在部署环境的 `mcp.servers` 中使用服务名称 `celiamcp`，通用 MCP 流程据此生成 `mcp_celiamcp_*` 工具名。上例的 `CELIA_MCP_EXE` 指向部署环境的 MCP 可执行文件；使用 HTTP 等传输时填写对应的连接参数。项目 ID 的环境变量兜底名称为 `CELIA_CELIAWORK_PROJECT_ID`。

自动摘要读取和对话入库由现有 Celia 接入负责；Swarm 的提示词 Rail 不再重复执行。现有扩展 Hook、参数处理、连接管理及工具白名单保持原有流程。

旧 Celia 私有客户端的配置不参与新的提示词挂载。没有新增连接适配层、schema 表或生命周期切换配置。

## 验证

测试使用真实 DeepAgent 和 MCP 工具执行器，验证模型仅收到已注册的 Celia 工具、参数 schema 与服务端一致、工具能够执行，以及移除提示词 Rail 后 MCP 工具仍然可用。同时覆盖各模式挂载、流式请求、模式切换和 MCP 缺失场景，并断言旧客户端与预检查均未调用。

实际 Celia 数据库和自动记忆效果仍需部署环境联调。
