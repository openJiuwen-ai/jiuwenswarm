# 渠道请求的 A2UI 禁用标记随模型调用传递

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-26 |
| 范围 | AgentServer 请求输入、ResponsePromptRail |
| 关联 spec | [S_07 系统提示词分层](../specs/S_07_system-prompt-layering.md) |

## 现象与根因

OfficeAce 转发的飞书消息走 `agent.plan`，入口日志显示 `metadata.skip_a2ui=true`，
模型调用日志却显示 `skip_a2ui=False` 和 `injected=True`，对应回复在 OfficeAce 渲染出表单。
DeepAgent 把原始输入归一化为 `InvokeInputs` 时会丢掉顶层扩展字段。ReAct 的模型回调使用独立的
上下文，只有通过 `RunContext` 传递的扩展字段会随它进入门控。

## 修复与验证

请求适配器将标记同时写入 `RunContext.extra`，保留已有运行上下文。提示词门控在 DeepAgent
调用和 ReAct 模型回调中都读取该值，命中时移除 A2UI 主提示段和强化段。
team 成员从构建上下文读取请求标记，TaskTool 子代理从继承的 rail 读取适配器每轮绑定的标记。

回归用例通过请求适配器和 DeepAgent 输入转换进入提示词门控。修复前复现了渠道请求误注入；
修复后，测试覆盖普通 Web → 渠道 → 普通 Web 三个连续请求，以及已有和缺省运行上下文两种情况。
A2UI 单元和系统测试共 148 项通过，1 项因浏览器工作流功能开关关闭跳过。真实飞书收发待在测试实例复验。
