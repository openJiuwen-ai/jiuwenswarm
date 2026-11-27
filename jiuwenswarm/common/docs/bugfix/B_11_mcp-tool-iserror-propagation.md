# MCP 工具失败被上报为成功：remote 适配器丢弃 `isError`

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-26 |
| 严重程度 | 高：工具级失败（`isError`）在 remote 路径上被抹掉，模型与统计都按成功处理 |
| 范围 | `mcp_config.py`：`_RemoteMcpCallAdapter.call_tool`；跨仓 `agent-core` 的 MCP client 侧同步修复 |
| 测试基线 | Python 3.11；`tests/unit_tests/common/` 683 项（671 通过 / 4 skipped / 8 项在 dev-stable 上同样失败的 GBK 解码与 SSE 清理用例） |
| 关联 spec | S_09_mcp-integration.md，N3 / 修改检查单 |

## 现象与根因

真实会话里，PPT 的工具调用返回：

```json
{"success": true, "tool_name": "apply_ops", "result": {"result": "{\"applied\":false,\"status\":\"rejected\",...}"}}
{"success": true, "result": "MCP error -32602: Input validation error: ..."}
```

服务端确实用 `CallToolResult.isError = true` 声明了失败（`pptx-slides` 的 `fail()` 走的就是这条），
但上层拿不到该标志：

1. `_RemoteMcpCallAdapter.call_tool` 把 openjiuwen remote client 的裸返回值重新包成
   `CallToolResult(content=[TextContent(...)])` 时**没有带 `isError`**，默认 `False`。
2. `RequestScopedOfficeClawMcpTool.invoke` 的失败补偿层判断的是
   `getattr(result, "isError", False) is True`——适配器永远是 `False`，补偿层形同虚设，
   于是返回 `{"result": text}`，`success` 由归一化补成 `true`。
3. 同一缺陷在 openjiuwen 客户端层：`extract_mcp_tool_result_content` 只取文本，
   `MCPTool.invoke` 也只返回 `{"result": ...}`，`isError` 在 client 边界就丢了。

## 决策

- openjiuwen 客户端新增并列 API `call_tool_with_meta(...) -> (content, is_error)`，四个内置
  transport（stdio / sse / streamable-http / playwright）逐个实现；`call_tool` 退化成薄包装，
  第三方 client 与测试替身继续只实现 `call_tool`（基类给默认实现，返回 `(content, False)`）。
- `MCPTool.invoke` 优先走 `call_tool_with_meta`，失败时输出
  `{"result": ..., "is_error": True}`；沿用既有键名 `is_error`，下游 stream / 归一化已经认识它。
- `_RemoteMcpCallAdapter.call_tool` 透传 `isError`，并优先调用 client 的
  `call_tool_with_meta`；缺失时回退 `call_tool` 且视为成功（保持旧行为，不误报）。
- 失败标志只在这里**透传**，不在适配器里改写文本或伪造 `error` 键：失败信封仍由
  `RequestScopedOfficeClawMcpTool.invoke` 的补偿层统一产出（`status=error` /
  `error_code=mcp_tool_error`），避免同一语义两处拼装。
- `mcp_call_timeout_patch` 同步覆盖 `call_tool_with_meta`：调用点从 `call_tool` 换成
  meta 版本后，超时截止、`_client_io_lock` 串行、死会话重连重试、ToolMgr owner 路由
  都必须继续生效——否则一次挂死的 remote 调用会把整轮对话拖死。openjiuwen 侧两个
  公开入口共用同一个私有实现（各自只保留一层 `@with_reconnect`，不叠加重试）；补丁
  侧用 ContextVar 记录已进入的 client IO 临界区，任一入口嵌套调用另一个时只做单次
  I/O，避免不可重入的 `asyncio.Lock` 死锁。旧版 openjiuwen 没有 meta 方法时包装
  自动跳过。
- `MCPTool` 只在 `call_tool_with_meta` 与生效的 `call_tool` 定义在同一个类时才走
  meta 版本：浏览器移动端等子类各自重写了 `call_tool`（重连 + 多段内容提取），
  用继承来的 meta 会静默绕过它们。

## 拒绝的方案

- 把 `call_tool` 的返回类型改成 tuple：破坏公开 API 与所有第三方 client，且 openjiuwen 内
  多处按"返回值即内容"消费。
- 在适配器里用文本前缀（`MCP error`）猜失败：`isError` 是协议级权威标志，猜文本只作兜底
  （文本兜底放在 harness 侧，见
  [B_11_business-state-tool-failure](../../../agents/harness/docs/bugfix/B_11_business-state-tool-failure.md)）。
- 只在 jiuwenswarm 侧打补丁：`vendor/jiuwenclaw` 是上游副本，每次同步会被覆盖；必须同时落
  agent-core。

## 验证

- 新增 `test_mcp_tool_is_error.py`（agent-core）：`extract_mcp_tool_result` 保留标志、
  `MCPTool.invoke` 输出 `is_error`、无 meta API 的旧 client 回退、streamable-http client
  的 meta 版本与纯文本版本。
- 新增 `test_tool_result_business_failure.py`（jiuwenswarm）：适配器透传 `isError=True`、
  旧 client 默认成功、`CallToolResult.content[0].text` 形态不变。
- 新增 `TestMetaVariantCoverage`（`test_mcp_call_timeout_patch.py`）：meta 版本被同一套超时
  包装覆盖、`call_tool → call_tool_with_meta` 只进一次 IO 临界区（旧版 openjiuwen 无 meta
  方法时用等价 stub 注入，契约不依赖安装版本）、ToolMgr owner 也路由 meta 版本。
- `tests/unit_tests/common/` 共 683 项：671 通过 / 4 skipped / 8 failed。8 例失败全部先于本次
  改动存在（已在 base 上逐一复现）：`test_mcp_patch_test_isolation` 的子进程输出在 GBK
  控制台解码失败、`test_todo_integration` 同样因 GBK 读取 UTF-8 文本失败、
  `test_registry_scan_timeout_and_shutdown[sse]` 见 B_10。

## 已知遗留

- 前端工具卡红叉仍不会亮：relay-claw 只为 deepresearch 生成 `toolResultMeta`，且
  `useAgentMessages` 会丢弃该字段。本次只修模型侧 / 统计侧 / wire 帧 `is_error`；
  UI 闭环需要 relay-claw 的 api + web 改动。
- **上线顺序**：`_RemoteMcpCallAdapter` 依赖 openjiuwen 的 `call_tool_with_meta`（本仓依赖
  `agent-core@dev-stable`，无需改版本号，但必须先合入 agent-core）。旧版 openjiuwen 下适配器
  走 `call_tool` 回退并视为成功——不回归，但 remote 路径的 `isError` 仍会丢，直到依赖同步。
- `isError` 只在 `RequestScopedOfficeClawMcpTool` 的补偿层落成 `status=error`；若某个
  第三方 client 既不实现 `call_tool_with_meta` 又把失败写进文本，只能靠 harness 的文本兜底判失败。
