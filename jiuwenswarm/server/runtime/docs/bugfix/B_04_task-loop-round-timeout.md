# B_04 · Code 长任务在一小时后被取消

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_04 / task-loop-round-timeout |
| 修复日期 | 2026-11-27 |
| 关联模块 | `agent_adapter/interface_code.py`、`agent_adapter/interface_deep.py` |
| 配置契约 | [common S_08 · N17](../../../../common/docs/specs/S_08_config-and-environment.md) |

## 现象与根因

问题报告中，`code.normal` 请求在持续调用模型和工具后返回
`Task loop round timed out after 3600 seconds.`。Code 创建实例时给缺失的
`completion_timeout` 补了 3600 秒，Deep 的创建和重载则补了 21600 秒。
SDK 从该轮开始等待直到任务完成，期间的工具输出不会延长时限。

## 修复与验证

三个装配点共用 `resolve_task_loop_completion_timeout`，并在出厂模板的
`react` 段声明该键，使现有用户的稀疏配置可以覆盖默认值。
Code 的默认时限从 1 小时延长至 6 小时，沿用 Deep 原有的兜底值。
需要执行更长任务的部署可以设置更大的秒数，或显式设为 `null` 取消单轮总时限。
`tests/unit_tests/test_task_loop_config.py` 覆盖配置校验，并通过模板合并和
Code 会话创建检查传给 SDK 的参数。Deep 的创建和重载配置取值由
`tests/unit_tests/agentserver/test_a2x_client_init.py` 覆盖。

在锁定版本的 agent-core 上用短轮次验证了两条等待路径：不限时设置下，任务完成时
正常返回；显式设置 0.01 秒时，挂起的轮次返回 `completion_timeout`。

热重载先校验有效配置中的时限，再替换适配器快照并传给会话。非法时限会使本次重载报错，
后续会话仍能使用旧快照；修正配置后可以再次重载。
`test_subagent_execution_timeout.py` 使用 SDK 的 `TaskTool` 从不限时的父实例委托给
`general-purpose`，验证子实例具有独立的 1800 秒时限并返回离线模型的回答。
