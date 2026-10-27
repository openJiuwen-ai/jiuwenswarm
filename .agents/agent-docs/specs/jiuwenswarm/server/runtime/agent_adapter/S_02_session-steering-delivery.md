---
id: S_02_session-steering-delivery
title: 当前执行补充输入的回答交付契约
type: S
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/agents/harness/common/prompt
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/server/runtime/agent_adapter/session_input.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/agents/harness/common/prompt/priority_registry.py
  - jiuwenswarm/channels/web/frontend/src/features/historyRestore.ts
---

# 当前执行补充输入的回答交付契约

## 执行与输入边界

`steer` 向当前执行补充要求，不独立创建一轮，也不隐式撤销原任务。用户明确取消、替换或改变原要求时尊重新要求。图片输入只接受当前会话的已上传图片引用，服务端完成校验后向 Core 的文本队列提供路径和读图提示。

`chat.input_received` 标记输入进入当前输出流；`chat.output_phase.applied_input_ids` 标记后续生成实际消费了哪些输入。输入身份和顺序以请求 ID 为准，不能用相同正文匹配不同输入。

## 未完整交付的回答

补充要求到达后，服务端可将旧生成阶段的输出标记为 `output_suppressed`。Core 上下文中已存在的 assistant 消息不等于用户已经看到该回答。

当在途模型产生的回答受到这条输入边界影响时，服务端保持当前执行的原请求、已消费补充输入和相关旧回复为待交付状态。默认 FULL 提示模式通过公开 system prompt builder 的 `session_input_delivery` section（固定优先级 66）向每次后续模型调用提供这些交付约束，包括读图后的工具续接。不能用一次普通 UserMessage 提醒代替持续状态。最终可见回答应覆盖当前执行中仍有效的原任务结果和补充要求，用户明确撤销的要求除外。已完成工具的调用及结果保持原有上下文顺序，不为补答重复执行工具。

交付 section 的原请求和全部补充正文分别限制为 4,000 字符，旧回复共限制为 8,000 字符，截断时显式标记；引用内容是数据，不是更高优先级指令。不得包含模型 reasoning 或工具请求，不写入用户气泡、回执或产品聊天历史。

状态受当前执行和输入批次约束。工具及模型重试不清除当前执行的义务；执行真正结束、卸载或新的普通请求时清理状态并移除 section。同一执行等待权限或用户回答时保留，恢复后随原队列继续处理。自定义 MINIMAL/NONE 提示模式由 Core 过滤 section，不在默认 FULL 模式的交付约束保证范围内。

跨会话 Agent 输入继续通过原有带来源的工具消息进入上下文，system section 只记录有限来源引用，不复制 Agent 正文或将其解释为人类取消、替换或直接授权。

## Web 展示与持久化

实时 Web 客户端与历史恢复均忽略 `output_suppressed` 的旧阶段文本。后续阶段交付的联合回答是可见结果，不通过重新展示隐藏旧答案补齐结果，不把两个阶段的 final 无条件拼接。

同一组事件在实时展示及刷新后恢复时，应具有相同的用户问题、补充图片关联、工具顺序及最终可见回答。明确取消原问题的补充输入不应在刷新后恢复旧答案；连续补充不得重复展示已经由最终阶段交付的答案。

## 验证入口

- `tests/unit_tests/agentserver/test_session_input_delivery.py`：交付说明、批次身份、失败和生命周期清理。
- `tests/integration/test_session_steering_sdk.py`：锁定 Core 的实际模型流与 steer 生命周期。
- `tests/integration/test_steer_image_paths_sdk.py`：上传引用、实际读图工具与模型图片上下文。
- `jiuwenswarm/channels/web/frontend/tests/sessionInput.test.mjs`：实时 React 展示与历史恢复一致。
