# B_07 — skill_turbo 刷新 send_file_to_user 误删父 agent 实例，管道结束后父 agent 发文件报 instance not found

## 元信息
| 项 | 值 |
|---|---|
| 日期 | 2026-11-03 |
| 严重程度 | 高——企业级部署下所有进入 skill_turbo 的会话，技能管道结束后父 agent 的文件投递必现失败（issue #5908） |
| 范围 | `environment.py`（`_refresh_send_file_tools` 重写、新增 `release_registered_tools`）；`agent.py`（委托方法）；`skill_turbo_tools.py`（invocation finally 接线） |
| 测试基线 | `tests/unit_tests/server/runtime/skill_turbo/` 全目录通过（含本缺陷新增用例）；`tests/unit_tests/agentserver/test_skill_turbo_tools.py` 新增 finally 接线三路径用例 |
| 关联 spec | [[S_01]]（执行路径 P2 register_tools）、[[S_02]]（不变量 13 / load_send_file_tools 契约 / 修改检查单）已同步修订 |

## 现象

企业级架构部署下，经 skill_turbo（skill_acceleration_exec）生成 PPT 的会话中：

- 技能管道**开始前**父 agent 发送文件：正常；
- 管道**内**由 turbo 自有实例发送文件：正常；
- 管道**结束后**父 agent 再发送最终交付文件：报 `[120001] Tool instance not found in resource_mgr: send_file_to_user_jiuwenswarm_s_<session>`，且此后该会话每次发送都失败（复现日志中连续 3 次）。

含 HITL resume 的会话（1 次调用 + 2 次 resume = 3 次 turbo invocation）100% 复现。

## 根因

失效链条（证据见 issue #5908 附件日志）：

1. `tool_ownership.py` 注册父 agent 工具时对卡片做原地带限定：`card.id = qualify_tool_id(card, owner_id)` → 父实例 id 形如 `send_file_to_user_jiuwenswarm_s_<session>`，实例注册在**进程级** `Runner.resource_mgr`；
2. `build_skill_turbo_config()` 把父 agent 的工具卡片原样传给 turbo 环境，环境由此继承携带父限定 id 的 send_file 卡片；
3. 每次 turbo invocation（含每次 HITL resume）新建 `SkillTurboEnvironment`，`register_tools` 走 `_refresh_send_file_tools`；旧实现把继承卡片从环境表弹出后，**对父限定 id 调用了 `Runner.resource_mgr.remove_tool`**——父 agent 在同一 run 内后续发送文件依赖的正是这个实例，被删后 `get_tool` 解析为 None，ProgressiveToolRail 报 instance not found；
4. 删除后无恢复路径：父 agent 的工具实例只在 run 启动时注册一次；
5. 同根因的次生缺陷：旧实现在移除后为技能注册一个新的 uuid id 实例，该实例挂在进程级注册表上，环境随 invocation 结束销毁却不回收——每次调用（含每次 resume）泄漏一个 uuid 实例，长期运行持续累积。

flash 模式与 agent（DeepAgent）模式同样复现：两者共享进程级 `Runner.resource_mgr`，flash 仅错误表面不同（ProgressiveToolRail 捕获后打 WARNING，不透出原始 120001）。

## 决策

1. **刷新语义收窄**：`_refresh_send_file_tools` 只把继承卡片从**环境本地**工具表弹出让位，注册技能自有 uuid 实例补充全局注册表；对全局注册表的 `remove_tool` 仅允许作用于**环境自注册的旧实例**（幂等刷新的清旧路径），**严禁移除继承的父实例**——父实例是父 agent 同一 run 内后续发文件的唯一凭据。
2. **自注册实例记账回收**：环境以 `_self_registered_tool_ids` 记录本次注册进全局注册表的工具 id；新增 `release_registered_tools()`（幂等）统一移除，`agent.py` 委托透传，`skill_turbo_tools.py` 在 invocation 包装的 `finally` 中调用——正常完成、异常、AbortError（HITL 中断）三条退出路径全覆盖，resume 不再泄漏。
3. **add 失败不记账不绑定**：`add_tool` 为 Result 风格（不抛异常），返回 Err 时（如重复 id）不得记账、不得绑定进环境表——否则 release 回收一个未注册的 id，executor 按绑定解析实例得到 None。

## 拒绝的方案

- **注册时克隆卡片，不原地带限定（tool_ownership 侧修）**：能消除"继承卡片与父实例同 id"这一耦合，是更彻底的长期方案，但改动面覆盖全仓所有工具注册路径，风险与验证成本远超本缺陷所需，故作为独立后续项（issue 修复评审中已记录），本缺陷不搭车。
- **父 agent 定期重注册 send_file 实例**：治标且引入新的竞态窗口（重注册前后 id 漂移），放弃。
- **release 失败保留记账 id 供重试**：`remove_tool` 返回 Err 实际只有"id 本就不存在"一种现实形态（实例已不在即无泄漏），保留重试入口属过度设计，仅保留 debug 日志。

## 验证

- 单测：刷新不移除父实例 / 重复刷新仅清自注册旧实例 / release 只回收自注册 id 且幂等 / add 失败不记账不绑定；finally 接线在正常完成、RuntimeError、AbortError 三路径下各调用一次。
- 实机：flash 模式复现路径（1 次调用 + 2 次 HITL resume）验证修复后 0 次 `instance not found`，turbo 结束后父 agent 成功发送 3 个交付文件（对比修复前同路径连续 3 次失败）。

## 已知遗留

- `tool_ownership.py` 克隆卡片方案未落地（见"拒绝的方案"首条），继承卡片带父限定 id 的现状仍在；本缺陷的防线是刷新逻辑的"严禁 remove"约定与对应单测。
- 环境静态工具（非 send_file）不受本缺陷影响，但同样常驻全局注册表，是否需要统一生命周期管理待后续评估。
