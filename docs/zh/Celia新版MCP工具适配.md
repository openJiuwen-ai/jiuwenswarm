# Celia 记忆接入与旧实现切换

默认使用新 Celia 接入。旧客户端、工具、文件记忆、初始化和历史查询代码保留，通过配置选择是否启用。

| provider | 提示词和工具 | 对话投递 |
|---|---|---|
| `celia`（默认） | `CeliaMcpPromptRail` 注入新提示词；通用 MCP 提供 `mcp_celiamcp_*` 工具与原始 schema | 部署侧现有扩展负责投递给记忆二进制 |
| `old-celia` | 原 `CeliaMemoryRail`、`CeliaMemoryProvider` 和私有客户端，保留无前缀的 `memory_*` 工具及独立旧提示词 | 旧 Rail 的 `sync_turn` 调用私有客户端 |

## 新 Celia 配置

```yaml
auto_memory_enabled: false  # Swarm 旧提取器
memory:
  mode: cloud              # 触发现有记忆扩展 Hook
  engine: external         # 只挂外接记忆 Rail
  external:
    provider: celia
  dreaming:                # Swarm 旧 Sweeper，不控制 Celia 二进制
    agent:
      enabled: false
    code:
      enabled: false
modes:
  agent:
    memory:
      enabled: false       # Swarm 旧文件记忆
  code:
    memory:
      enabled: false       # 旧 CodingMemory / ProjectMemory
      auto_coding_memory: false
mcp:
  servers:
    - name: celiamcp
      enabled: true
      transport: stdio
      command: ${CELIA_MCP_EXE}
```

`CELIA_MCP_EXE` 指向部署环境的 MCP 可执行文件；HTTP 等传输应填写已有连接参数。模板的 `mcp.servers` 保持空列表，服务由部署环境配置。项目 ID 的环境变量兜底名称为 `CELIA_CELIAWORK_PROJECT_ID`。

Celia 二进制内部负责自动提取和 Dreaming。上述 Swarm 旧实现开关不配置二进制内部功能。`mode: cloud` 只触发记忆 Hook；实际召回、对话投递需要部署环境安装并注册对应扩展。

普通、代码、设计和集群成员共用新的提示词入口。Memory 优先级为 57，位于运行时 Skills（56）之后；模式切换重建提示词时恢复该段，避免重复。新提示词不包含 `USER.md` / `MEMORY.md` 的读写指令。

旧本地记忆关闭时，请求处理不会重新注册旧写入工具；普通模式上下文也跳过旧记忆文件内容。新 Celia 和 `engine: none` 默认不创建 `USER.md`、`MEMORY.md`、`memory/daily_memory/`，启动及集群成员的 SDK 初始化也遵循此判断。显式启用 `old-celia` 或本地内置 Agent 记忆后才启用旧文件初始化；`coding_memory/` 由本地内置代码记忆开关控制。初始化、迁移和历史查询实现继续保留，已有文件不删除。配置默认值可被用户配置或环境变量覆盖，应以运行时最终配置为准。

## 切换为 old-celia

修改以下配置并重启 Swarm，同时把现有 `celiamcp` 服务的 `enabled` 设为 `false`：

```yaml
memory:
  mode: local              # 关闭部署侧记忆 Hook，避免与旧 Rail 重复投递
  engine: external
  external:
    provider: old-celia
    celia:                 # 复用旧私有客户端的原配置段
      server_binary_path: ${CELIA_MEMORY_BINARY_PATH}
      # db_path、embed、chat 等继续使用原参数
```

`old-celia` 使用 `resources/memory/old-celia/AGENTS.md`，与旧工具名及旧文件同步规则匹配。修改 provider 不会自动关闭通用 MCP 服务；切换时应同时应用上述配置，避免两套工具或两条入库路径并存。

Swarm 内置文件记忆独立于 `old-celia`。如需启用，使用 `mode: local`、`engine: builtin` 及 `modes.agent.memory.enabled: true`；代码记忆、旧自动提取、旧 Sweeper 分别由各自原开关启用。

## 验证范围

回归覆盖新旧 provider 构建、原身份隔离和旧客户端契约、新 MCP schema 透传及执行、各模式挂载、流式请求、模式切换、Skills/Memory 顺序、旧工具恢复开关以及旧文件上下文的禁用和重新启用。MCP 后端由测试替身提供，真实二进制和部署侧投递扩展需要联调。
