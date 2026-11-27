---
id: B_20261126_1625_enterprise-cron-permission-rail-a4p-decouple
title: 企业版 Cron 权限护栏与 A4P 解耦，ask 无人值守 fail-closed
type: B
author: zhangxiangyu52
author_email: zhangxiangyu52@huawei.com
designs: []
design_note: 无独立 D：依据企业版安全护栏与定时任务无人值守契约的既有产品要求及 PR #10967 检视意见落地；用户文档 docs/zh/A4P预授权.md、docs/en/A4P-Preauthorization.md 同步了面向使用者的表述，本 B 记录实现取舍与演进。
modules:
  - jiuwenswarm/agents/harness/common/rails
  - jiuwenswarm/agents/swarm
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/agents/harness/common/rails/interrupt/interrupt_helpers.py
  - jiuwenswarm/agents/swarm/providers/code_rails.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - docs/zh/A4P预授权.md
  - docs/en/A4P-Preauthorization.md
  - tests/unit_tests/agentserver/test_a4p_cron_permission.py
  - tests/unit_tests/agentserver/test_a4p_cron_rail_registration.py
  - tests/unit_tests/agentserver/test_cron_skip_permission_interrupt_rail.py
---

# 企业版 Cron 权限护栏与 A4P 解耦

## 现象与影响

企业版定时任务（cron）在「工具权限已开启、但 A4P 预授权关闭」时，原先不挂
`PermissionInterruptRail`：调度会话按 `_skip_cron_permission_rail` /
Code 路径把护栏整段跳过，工具规则（含默认 ask）无法在无人值守路径上生效，
相当于 **企业版 cron 无护栏裸奔**。

叠加影响：

1. allow/deny 规则在 cron 上形同虚设（rail 未挂载）；
2. 若后续仅挂手动 rail 却仍落到交互式 `"interrupt"`，无人 resume → 任务挂起到
   turn 超时，失败延迟暴露；
3. 产品文档承诺「定时任务不等待交互审批」与实现不一致时难排查——用户文档
   （A4P 预授权）无法替代本仓 bugfix 对 **挂载策略 / fail-closed** 的决策记录。

## 关联设计与当前契约

无独立 D。当前有效约束：

- **企业版**：权限护栏与 A4P 配置独立；cron 只要 permissions 启用即挂手动
  PermissionInterruptRail（与聊天会话同构的挂载条件，不要求 A4P on）。
- **个人版**：保留实验门控——cron 默认跳过护栏，仅在 A4P 预授权激活时接入
  （避免个人版无人值守路径被 ask 规则意外卡住）。
- **Cron ask**：无人值守；无有效 A4P cron intent token 时 **fail-closed 直接拒绝**，
  不落 HITL `"interrupt"`。智能审批（`permissions.mode=auto`）亦不例外——
  cron 不会安装 `AutoPermissionInterruptRail`（见
  `interface_deep._auto_permission_profile_supported` 含
  `not self._is_cron_execution`），按配置放行 interrupt 只会挂死。
- **A4P token 路径优先**：confirm 回调里先走 A4P 校验；通过则放行；失败 / 无 token
  再进入 cron fail-closed。二者顺序固定，避免「有 token 仍被 deny」或「无 token
  却去等人」。

## 根因与修复决策

### 根因

1. **挂载与 A4P 耦合**：`_skip_cron_permission_rail`（Deep）与
   `code_rails.build_permission_interrupt` 曾用
   `is_cron and not is_a4p_preauthorization_active(...)` 跳过护栏，把「要不要挂
   安全轨」绑在「A4P 是否开启」上。企业版关闭 A4P 时 cron 无轨。
2. **ask 交互语义不适合 cron**：rail 挂上后，ask 默认会 `"interrupt"` 等人审批；
   cron 无操作员，必须在 confirm 层 fail-closed。
3. **曾误开 auto 例外（已回滚）**：中间一度按 `is_auto_permission_mode(配置)`
   对 cron ask 放行 `"interrupt"`，注释假定存在「rail LLM 路径」。cron 实际从不
   安装 Smart Approval LLM rail，例外会变成 HITL 挂起回归；后改为 **cron 一律
   fail-closed**（不再看 mode）。

### 方案取舍

| 候选 | 结论 | why / why-not |
|---|---|---|
| 企业版 cron 独立挂载，与 A4P 解耦 | **采用** | 安全护栏是企业基线；A4P 是可选预授权增强，不应关掉 A4P 就拆护栏 |
| 个人版仍「无 A4P 则 skip cron rail」 | **采用** | 个人版保留原实验门控，降低无人值守被 ask 卡住的回归面 |
| ask → fail-closed（立刻拒绝） | **采用** | 符合无人值守；比挂起超时更快、更可观测 |
| ask → interrupt 等人 | **拒绝** | cron 无人 resume，占用会话至超时 |
| auto 模式仍 interrupt 交给 LLM | **拒绝** | cron 不装 AutoPermissionInterruptRail；按配置判断能力不存在 |
| 用 `enable_auto_permission` 构建期标志放行 interrupt | **无需** | 当前 cron 该标志恒 False；直接 `if is_cron: deny` 更简单且与能力一致 |
| reload 后企业版 A4P off 仍卸轨 | **拒绝** | 解耦后 `_skip_cron_permission_rail` 企业版恒 False；`_update_permission_rail` / `_unregister_permission_rail_if_disabled` 不再因「A4P off」卸掉企业版 cron 护栏，rail 随 permissions 启停而非 A4P |

### 修复要点

1. `interface_deep._skip_cron_permission_rail`：企业版 cron → 永不 skip；个人版 cron →
   仅 A4P active 时不 skip。
2. `code_rails.build_permission_interrupt`：同步
   `is_cron and not is_enterprise() and not is_a4p_preauthorization_active(...)`。
3. `interrupt_helpers` confirm：A4P 路径之后，`if is_cron: return _deny_cron_scope(...)`，
   不区分 manual/auto。
4. 用户文档（zh/en A4P）补充「企业版 cron 挂轨与 A4P 独立、ask fail-closed」表述。

## 回归证据与适用范围

行为矩阵（实现提交与 UT 对照）：

| 场景 | 期望 |
|---|---|
| 企业版 × permissions on × A4P off × cron | 挂手动 PermissionInterruptRail |
| 企业版 × permissions on × A4P on × cron | 挂轨；有匹配 token 可放行，否则 ask fail-closed |
| 企业版 × permissions off × cron | 不挂 / 卸轨（随 permissions 显式关闭） |
| 个人版 × A4P off × cron | 跳过护栏（实验门控） |
| 个人版 × A4P on × cron | 挂轨并走 A4P / fail-closed 语义 |
| cron × mode=auto × ask 无 token | **拒绝**（`A4P_CRON_SCOPE_DENIED`），非 interrupt |
| web × mode=auto × ask | 仍可 interrupt（Smart Approval 路径，非本 B 重点） |
| Code / Team 构建路径 | 与 Deep 企业/个人分流一致 |

验证入口（本分支）：

- `tests/unit_tests/agentserver/test_cron_skip_permission_interrupt_rail.py`
- `tests/unit_tests/agentserver/test_a4p_cron_rail_registration.py`
- `tests/unit_tests/agentserver/test_a4p_cron_permission.py`
  （含 `test_smart_approval_does_not_consume_a4p_tokens`：cron 分支断言 fail-closed）

重验条件：改动 `_skip_cron_permission_rail`、`build_permission_interrupt`、
confirm 内 cron 分支，或 `_auto_permission_profile_supported` 放开 cron 时，须重跑上表矩阵。

## 防错模式与资料同步

- **能力标志优于配置猜测**：无人值守路径是否可 `"interrupt"`，应以「是否安装了能决策的
  rail」（如 AutoReviewer）为准；不可仅凭 `permissions.mode=auto` 推断存在 LLM 审批。
- **产品分流写进 skip 谓词**：企业/个人差异集中在 `_skip_cron_permission_rail` /
  Code 对称条件，避免散落在 confirm 与文档各写一套。
- 已同步：`docs/zh/A4P预授权.md`、`docs/en/A4P-Preauthorization.md`（使用者视角）；
  本 B 为决策与回归视角。未另增 AGENTS 模式条目（属定点契约修正）。

## 遗留与后续

- 若产品明确要求 **cron 也跑智能审批 LLM**，需先放开
  `_auto_permission_profile_supported` 的 cron 排除，并为 cron 安装
  `AutoPermissionInterruptRail`；彼时再评估是否允许 confirm 对 cron 返回
  `"interrupt"`。超出本修复范围。
- Team/Code 路径若新增其它构建入口，须复用同一企业/个人 skip 谓词，避免再次耦合 A4P。
