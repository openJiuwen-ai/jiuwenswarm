# ToolCallResilienceRail 契约

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_07 / tool-call-resilience |
| 关联模块 | openjiuwen/harness/rails/tool_call_resilience_rail.py（agent-core） |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无 |

## 范围 / 边界

- **管**：tool-mgmt 文档体系内对 **调用韧性 rail** 的协作描述——priority、默认开关、幂等/非幂等 retry 规则、与 swarm `invoke_tool` 上 resilience 属性的交界。
- **不管**：具体 timeout 网络栈、各 toolkit 自行重试逻辑。

## 概要

| 项 | 值 |
|---|---|
| 类 | `ToolCallResilienceRail` |
| priority | 70 |
| 默认启用 | harness factory `enable_tool_resilience_rail=true`（以 core 代码为准） |
| 核心规则 | **非幂等（non-idempotent）工具永不 retry** |

## 不变量

- **N1（非幂等不重试）**：改 retry 策略时不得绕过 non-idempotent 判定；回归 `test_tool_call_resilience_rail.py`（agent-core 仓）。
- **N2（与 invoke_tool 交界）**：swarm `InvokeToolTool` ToolCard 带 `properties.resilience.timeout_s: None`——代理调用时 resilience rail 按 core 规则处理**底层 tool**；改 invoke 卡片属性需评估 resilience 行为。
- **N3（非 progressive 专属）**：resilience 作用于 tool call 管线，与 [[S_02]] 渐进模型正交——load_tools 流与 invoke_tool 流均可能触发。
- **N4（swarm 文档不复制 core 实现）**：retry 次数、退避、错误分类以 agent-core 源码 + 单测为准；本 spec 只锁定**产品级不变量**。

## 接口契约（索引）

- 工厂参数：`enable_tool_resilience_rail: bool = True`
- Rail 挂载于 DeepAgent harness 链（与 Progressive 80、Mcp 95 等顺序独立）

## 执行路径

### P1：普通 tool call

```
模型 direct call eager/skill-gated tool
  → ToolCallResilienceRail 包装
  → 若 tool 标记 non-idempotent → 失败不重试
```

### P2：invoke_tool 代理

```
invoke_tool → rail 内调用 deferred tool
  → 同样经 resilience 链（若已装配）
```

## 修改检查单

- 改 core resilience 默认 → 评估 swarm 全产品 agent 行为；更新本 spec 元信息日期
- 改 InvokeToolTool resilience 属性 → invoke 单测 + resilience 单测

## 与其它 spec 的关系

- [[S_03]] invoke_tool card properties
- [[S_02]] 与 progressive 正交
