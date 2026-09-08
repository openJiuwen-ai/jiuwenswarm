# Celia 新版 MCP 工具适配

本次适配依据 2026-09-08 提供的九工具输入契约。服务端源码和实际响应结构尚未核验；测试使用该契约以及本地真实工具包装器。

## 工具分工

`jiuwenswarm/agents/harness/common/memory/celia/tools.py` 的 `mcp_tool_schemas()` 返回完整九工具传输契约，`tool_schemas()` 生成模型可见的工具定义。

| 工具 | 调用入口 |
| --- | --- |
| `memory_add` | 每轮结束的自动入库钩子；不交给模型重复调用 |
| `memory_store` | 模型显式记忆；直接调用后端持久化 |
| `memory_record_search` | 模型检索；必须指定 `atomic_fact` 或 `raw_conv` |
| `memory_global_load` | 自动预加载及模型按需加载 |
| `memory_scene_load` | 模型按 `sceneIds` 加载，单次 1–5 个 |
| `memory_scene_search` | 模型按 `subSceneTag` 查找场景 |
| `memory_backup` | 显式配置启用且后端声明支持后注册 |
| `memory_restore` | 同上；`dryRun=1` 仅预览，默认 0 会写入 |
| `memory_update_config` | 同上；修改运行配置，属于写操作 |

旧的 `memory_open`、`memory_search_l2/l3`、`memory_load_l1`、场景索引接口和用量上报接口不再用于此协议。旧二进制不兼容时，初始化日志会报告缺少的工具；Rail 的静态提示词仍可挂载。

## 身份与隔离

模型只填写业务参数。`userId`、`sessionId`、`traceId`、`requestScope`、`scope`、`scopeFilter` 由适配层注入，模型传入这些字段会被拒绝。

- 用户和租户来自请求上下文，配置值作为回退；集群成员也接收序列化的请求 metadata。
- `request_scope` 配置与可信请求 metadata 的 `celia_request_scope` 合并。`tenantId` 固定取当前租户身份，保留原租户隔离。
- `memory.external.scope_id` 支持 `user`/`1`、`global`/`0`、`session`/`3`。旧的 `__default__` 按用户范围处理；agent 范围不受新版契约支持。
- 用户范围检索省略 `sessionId`，以便检索历史会话。会话范围检索携带当前 conversation ID；写入也携带它作为 `sessionId`。
- 缓存按数据库、用户、租户、授权范围和动态隔离键区分；会话范围还区分 conversation ID。

## 配置与记忆开关

```yaml
memory:
  engine: external
  external:
    provider: celia
    scope_id: user
    celia:
      preflight_enabled: false
      request_scope: {}
      advanced_tools: []
```

需要扩展工具时，在 `advanced_tools` 中明确填写相应名称。服务端仅声明支持不会自动启用这些工具。

`memory.engine` 决定是否挂载 Rail；`.xiaoyiruntime` 的 `MEMORYSTATE` 独立控制提取及提取后记忆的使用：

- 开启：自动写入使用 `skipExtraction=0`，可显式存储、检索原子事实并加载摘要/场景。
- 关闭：自动写入使用 `skipExtraction=1`，仍可检索 `raw_conv`；直接提取存储和原子事实/场景检索返回关闭状态。
- 后端不可用时，`memory_store` 返回失败，不再通过本地缓冲返回持久化成功。

自动写入按用户/助手角色分开发送；长文本按 UTF-8 边界分块，每块最多 81920 字节。显式 `memory_store` 超过该字节限制会被拒绝。

## 响应与验证范围

业务工具保留后端完整结果结构。全局预加载保留原始响应，避免猜测字段名而丢失导航或场景 ID；无法从导航获得 ID 时，prompt 指引模型先调用 `memory_scene_search`。

测试覆盖参数边界、身份覆盖拒绝、动态隔离、直接存储失败、关闭状态下原文检索、UTF-8 分块、工具注册、真实 `LocalFunction` 参数转换，以及集群和代码/设计模式挂载。真实 Celia 二进制、HTTP 认证和服务端返回结构仍需部署环境联调。
