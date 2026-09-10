# Celia 提示词与 Celia MCP 工具

Swarm 使用通用 MCP 流程中已注册的 `celiamcp` 服务。模型收到的工具名为 `mcp_celiamcp_*`，工具描述、参数 schema 和执行均沿用该服务。

## 挂载入口

`memory.external.provider: celia` 现在只挂载 `CeliaMcpPromptRail`，负责注入静态 Memory 段，优先级仍为 15。普通、代码、设计和集群模式共用此入口；模式切换后恢复提示词，卸载时移除该段。

旧 `CeliaMemoryProvider`、`CeliaMemoryRail`、私有 MCP 客户端及静态 schema 已删除，无前缀的 `memory_*` 工具不再注册。

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

配置模板已移除旧 Celia 私有客户端参数，仅保留小艺记忆开关使用的 `celia.runtime_state_path`。小艺 `MemoryQuery` 仅保留 `MemoryStateGet` / `MemoryStateSet`；`UserMdQuery`、`MemoryMdQuery` 和 `MemoryHistory` 已移除。工作区根目录的 `USER.md`、`MEMORY.md` 及 `.memory.log` 不再创建或读取，旧标记区同步模块已删除。内置文件记忆已下线：不再创建、迁移、索引或注入 `memory/MEMORY.md`、`memory/daily_memory/*.md`，SDK 默认工作区及上下文清单也不再包含这些文件和 `USER.md`。旧文件读写工具及请求期间重新注册它们的逻辑已删除；日报技能不再读取每日文件。磁盘上的历史文件不自动删除。代码记忆的项目索引继续保留。

`memory.engine` 仅支持 `external` / `none`，默认 `external`；无效配置不挂载记忆 Rail。SDK 的默认目录策略由仓库中的 `agents/harness/workspace_policy.py` 统一设置，覆盖普通 Agent、集群和子代理，安装本仓库即可生效。

## 验证

测试使用真实 DeepAgent 和 MCP 工具执行器，验证模型仅收到已注册的 Celia 工具、参数 schema 与服务端一致、工具能够执行，以及移除提示词 Rail 后 MCP 工具仍然可用。同时覆盖各模式挂载、流式请求、模式切换和 MCP 缺失场景，以及小艺记忆开关、已移除查询的错误响应和初始化不再创建旧记忆文件。最终请求检查包含真实工作区、运行时目录说明与上下文组装，并放入旧文件验证内容不会被读取或注入。

实际 Celia 数据库和自动记忆效果仍需部署环境联调。
