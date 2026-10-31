# Context 工程 Rails（Assemble / Processor）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_03 / context-engineer-rails |
| 关联模块 | agent-core `openjiuwen/harness/rails/context_engineer/` + interface_deep 注册逻辑 |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

### 本规约管什么

- **ContextAssembleRail** / **ContextProcessorRail** 的职责分割、priority、swarm 注册条件。
- 与 **ContextOverflowRecoveryRail**（priority=100，[[S_05]]）及 prompt rails（priority=5，[[S_07]]）的相对顺序语义。
- Processor 链 **bailout** 与 `AbortError` / `StatusCode.CONTEXT_EXECUTION_ERROR`（agent-core 契约，swarm 不得破坏）。

### 本规约不管什么

- Assemble 内部 tools 段 markdown 模板 → agent-core。
- 非 context 的 DeepAgentRail（skill / memory 等）→ 各姊妹模块。

## 不变量

- **N1（门控分离）**：`ContextAssembleRail` 在 agent 模式 `_update_agent_rails` 中构建并注册，**不**以 `context_engine_config.enabled` 为条件；`ContextProcessorRail` **仅当** `enabled` 为真注册，为假则 unregister。Good：只关压缩时不误卸 Assemble。Bad：用一个开关同时控制两者。
- **N2（循环中止）**：ReasoningToolLoopCompact 等 processor 的 bailout 必须抛 **AbortError** 且 status **CONTEXT_EXECUTION_ERROR**（core 约定）；swarm rail 不得 catch 后继续本轮模型调用。
- **N3（Priority 表）**：Context 工程 core rails **priority=85**；OverflowRecovery **100**（更高先执行 `on_model_exception`）；Runtime/Response prompt **5**。改 priority 必须评估与 StreamEventRail（80）等相对顺序。
- **N4（Core 导出面）**：`openjiuwen.harness.rails.context_engineer.__all__` **仅** `ContextAssembleRail`、`ContextProcessorRail`——swarm 不应从该包期待其它符号。
- **N5（构造兼容）**：`_build_context_assemble_rail` 对缺少 `tool_name_allowlist` 的旧 core 走 TypeError 分支 + `set_tool_name_allowlist` 回退；改构造参数时须保留兼容路径或明确 bump core 版本（注释引用 agent-core !2840）。

## 接口契约

### ContextAssembleRail（swarm 构建）

```text
_build_context_assemble_rail(
  disabled_tools: list[str] | None,
  tool_name_allowlist: list[str] | None,  # 通常 ProgressiveToolRail.eager_tools
) -> ContextAssembleRail | None
```

- `disabled_tools`：来自 DisabledToolsRail 或 `react.disabled_tools`。
- `tool_name_allowlist`：与渐进式工具 eager 列表对齐（[[S_06]] N2）。

热更新：`_sync_context_assemble_tool_allowlist(assemble, progressive)`。

### ContextProcessorRail（swarm 构建）

```text
_build_context_processor_rail(config: dict) -> ContextProcessorRail | None
```

- `config` 为 react 段（含 `context_engine_config` 嵌套 dict）。

### 注册点（agent 模式）

`interface_deep._update_agent_rails`：

1. 重建/注册 Assemble（带 disabled + allowlist）
2. 若 `context_enabled`：构建并注册 Processor；否则卸载 Processor

## 数据结构

- Assemble rail 实例状态：`disabled_tools` / `tool_name_allowlist`（或 `_` 前缀私有字段），以实例 getter 为准记录日志（interface_deep 构建后 introspection）。

## 执行路径全景

### P1：冷启动 rails（deep）

```
_build_agent_rails → _RailBuildInfo ContextOverflowRecoveryRail
_instantiate_rails
_update_agent_rails (mode=agent) → Assemble + Processor 动态注册
```

### P2：Team 成员

```
team_runtime_inheritance.get_context_engine_enabled
  → 为真时 build_member_rails / ContextProcessorInput 注入 processor rail
（配置形状与 deep 一致：caller 剥离 react 外层）
```

### P3：Code 模式

```
interface_code 固定 rail 表含 ContextAssembleRail / ContextProcessorRail
_build_context_processor_rail 复用 interface_deep 函数
```

## 修改检查单

- 改 Assemble/Processor 注册条件 → 同步 [[S_06]] 与单测（assemble allowlist compat）。
- 改 `context_engineer` import 路径或类名 → grep 全仓 adapter + team + swarm providers。
- 新增第三个 context engineer rail → 先扩 agent-core `__all__` 与产品需求，再开 spec 修订或新 S_NN。

## 与其它 spec 的关系

- [[S_02]]：Processor 链成员与 FullCompact
- [[S_05]]：OverflowRecovery 与 Processor GET 联动
- [[S_06]]：enabled 双默认、flash 裁剪
- [[S_07]]：prompt rails 与 Assemble 产出窗口的关系（Assemble 管 messages/tools 段，prompt rails 管 system sections）
