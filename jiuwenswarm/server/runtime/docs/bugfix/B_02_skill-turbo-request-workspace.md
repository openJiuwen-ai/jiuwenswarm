# 宿主project_dir未转换为SkillTurbo输出工作区

## 元信息

| 项 | 值 |
|---|---|
| 类型 / 编号 | bugfix / B_02 |
| 日期 | 2026-11-16 |
| 模块 | agent_adapter/interface_deep.py请求期metadata绑定；Flash继承同一路径 |
| 契约 | skill_turbo/docs/specs/S_09_outer-integration.md N5；S_10任务身份与输入装配 |
| 状态 | 本地修复与回归验证；未提交、未重启业务服务、未真实模型复测 |

## 现象与证据

用户在源码启动的OfficeAce发送“帮我制作一份5页的介绍PPT，主题是人工智能”。
会话officeclaw_b981c2df38c54f0e36657a7b中，skill_tool已返回含四段schema和批量规则的附录；
模型两次以skill=pptx-craft、accelerator=prep进入新单段入口，均返回
blocked / workspace_missing / executed=false。实际输出工作目录存在。
模型随后用普通PPT MCP工具制作、保存并成功发送5页文件；没有执行后续加速段或批量。

事件证据为Relay只读文件：
`E:/relay-claw-zqh/relay-claw/data/conversation-events/8d9ceda94b3a0335a13ca3cbe18f9e4964466019e7a4823a1109dec35d3b3e69/1-565.json`，
seq28/29及68/69为两次调用/阻断结果；seq523为保存成功，seq532为发送成功。
未据模型总结推断段已执行或新交付确认已接通。

## 根因

Relay buildRequest将宿主workingDirectory放在params.project_dir；
adapter把它用于当前工作区和effective_project_dir，但未补齐metadata.output_dir。
段引擎及只读进度按既定契约只从metadata.output_dir读取输出工作区，因此业务执行前拒绝。
此前段测试直接提供output_dir，缺少真实宿主只传project_dir的适配测试。

## 修复与边界

按用户确认仅修改jiuwenswarm：在请求metadata副本构造处，output_dir未提供/None/空白字符串时，
仅采用本次RuntimeConfig明确传入、去首尾空白后非空且为绝对路径的project_dir补齐。
已有非空output_dir不覆盖，后续继续沿用路径/来源校验；不以适配为由扩大允许的文件边界。
不采用adapter._project_dir缓存、effective_project_dir、cwd、内部/默认工作区推断输出位置。

归一化发生在set_current_request_metadata及StreamEventRail/TaskExecutionRail副本同步之前，
保证请求任务、工具异步任务及只读进度消费同一字段；不原地修改wire metadata，不缓存上次补齐值。
Deep与Flash均复用该基类绑定；正常调用和恢复均重新核验，原断点工作区变化仍按原契约拒绝。
执行器、PPT四段业务、公共工具参数及legacy内部执行逻辑未修改。
Relay源码/配置/结果消费补丁、SDK及业务持久化数据均未修改。

## 回归矩阵

新增`tests/unit_tests/server/runtime/skill_turbo/test_request_workspace_bridge.py`。
测试通过实际AgentRequest形态、真实_update_runtime_config及StreamEventRail.before_tool_call进入SDK工具，
使用既有真实PlanNode、临时Checkpointer和临时文件；外部模型身份及业务探针沿用受控边界。
与工作区归一化无关的工具/模型注册和运行时状态落盘替换为测试边界，未启动业务服务。

| 场景 | 验证 |
|---|---|
| Deep/Flash；缺失、空和空白output_dir | 明确project_dir补齐，请求及两个rail副本一致，不改原wire metadata |
| 工具在独立空Context异步任务执行 | 经实际rail重新绑定后，公开单段入口成功并独立读取产物 |
| 已有显式output_dir，与project_dir不同且均在允许范围内 | 保留output_dir，任务目录不改绑到project_dir |
| project_dir缺失/空/相对路径，只有缓存项目或effective_project_dir | 仍workspace_missing，零业务/零新段任务目录 |
| 同一adapter先后不同会话/工作区及随后缺失 | metadata副本不沿用前一请求output_dir |
| 单段暂停恢复 | 保留工作区和已完成叶子，完成revision一次，清本次断点 |
| 批量第2段暂停恢复 | 前段/当前叶子不重做，后段消费事实，revision及断点清理正确 |
| 恢复时project_dir变化 | failed，零新业务/新工作区任务，不自动fresh |

修改前映射正例缺字段/空白断言失败，公开单段复现blocked/workspace_missing；日志为
`.agents/testing/workspace-bridge/before-confirmed.log`。
初始测试装配曾缺SDK ToolCall.type及暂停所需模型身份，另一个用例将项目放在输出区之外触发既有路径保护；
已修正测试装配并保留历史日志，不放宽生产路径校验或断点模型身份规则换绿灯。
扩展定向矩阵24 passed（14.86s）。随后加入独立空Context异步任务验证，完整相关回归中这24项全部通过。

完整相关回归日志：`.agents/testing/workspace-bridge/regression.log`。
命令包括turbo全模块、Deep/Flash披露、工具/HITL/恢复、runtime-config时序、projectless工作区、
chat_interrupt_tenant_pool/team_cancel及AutoHarness渠道上下文；保留asyncio-mode=auto。
最终结果为946 passed、1 failed、1 warning，142.02s。唯一失败是
`test_auto_harness_channel_context.py::test_runtime_config_syncs_channel_and_task_workspace`：
Windows实际规范路径为`E:\different\project`，旧断言写死为`/different/project`。
在独立测试进程中仅加载本次修改前备份的_apply_runtime_config_stages方法，未改工作区源码或运行服务，
复跑该用例仍为同一断言失败（1 failed，2.16s），证据为
`.agents/testing/workspace-bridge/auto-harness-before-confirmed.log`。
未修改该既有测试或放宽断言，不宣称全套947项均通过。
使用既有.venv及安装SDK openjiuwen 0.1.16；Python/SDK未升级。
未执行Python格式化或静态检查，未提交、推送或发布。

最终命令（引擎仓）：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ tests/unit_tests/agentserver/test_flash_turbo_discovery.py tests/unit_tests/agentserver/test_foreign_stream_source.py tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py tests/unit_tests/agentserver/test_skill_turbo_hitl_tie_rewrite_repro.py tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py tests/unit_tests/agentserver/test_skill_turbo_resume_usage.py tests/unit_tests/agentserver/test_skill_turbo_tools.py tests/unit_tests/agentserver/test_skill_turbo_prompt_disabled.py tests/unit_tests/agentserver/test_deep_adapter_runtime_config_timing.py tests/unit_tests/agentserver/test_projectless_agent_workspace.py tests/unit_tests/agentserver/test_chat_interrupt_tenant_pool.py tests/unit_tests/agentserver/test_chat_interrupt_team_cancel.py tests/unit_tests/a2ui/test_auto_harness_channel_context.py -o addopts="--asyncio-mode=auto" -q -o log_cli=false --tb=short
```

git diff --check通过（uv.lock仅既有LF/CRLF提示），Relay packages/api和packages/web的git diff为空。

## 交接与限制

实施前adapter文件备份：`C:/Users/13653/AppData/Local/Temp/skill-workspace-bridge-before-dyngkxxj/interface_deep.py`。
工作区仍包含前七步未提交修改，不能把相对HEAD的全部差异算作本次修复。
本次不重新执行真实用户PPT或发送文件，不把临时SDK恢复测试称为产品端到端验收。
运行中的Python sidecar需重新加载修改后的模块再进行新请求复测；本次未操作现有服务进程。
源码、依赖、请求装配或恢复协议发生变化时应重验相应矩阵；尚未进行独立代码检视。
