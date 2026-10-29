---
id: S_01_skillpack-workflow
title: SkillPack 沉淀与直达执行规约
type: S
author: 霆龙(吴建宇)
author_email: wujianyu@xiaohongshu.com
modules:
- jiuwenswarm/agents/harness/common
- jiuwenswarm/symphony
- jiuwenswarm/server/runtime/agent_adapter
files:
- jiuwenswarm/agents/harness/common/tools/skillpack_toolkit.py
- jiuwenswarm/agents/harness/common/rails/symphony/skillpack_route.py
- jiuwenswarm/symphony/skillpack_candidates.py
- jiuwenswarm/symphony/service.py
- jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
designs:
- symphony-design-docs:SDD-0013
design_note: 依据 symphony-design-docs 仓库 proposals/SDD-0013-jiuwenswarm-skill-pack-productization/design.md（v9）；设计仓库：https://gitcode.com/lin-xiaoyu/symphony-design-docs。
---

# SkillPack 沉淀与直达执行规约

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 关联模块 | `jiuwenswarm/agents/harness/common`、`jiuwenswarm/symphony`、`jiuwenswarm/server/runtime/agent_adapter/interface_deep.py` |
| 最近修订 | 2026-10-28 |
| 设计依据 | SDD-0013 v9 §4.2.1、§4.2.2 |
| 实施记录 | [沉淀与直达执行实现](../../../../../features/jiuwenswarm/agents/harness/common/F_20261026_2050_skillpack-workflow.md) |

## 范围与启用条件

本模块把 Core 已有的真实执行沉淀接入对话 Tool，并在单 Agent Symphony 编排前选择本地已安装 Pack。普通成员类型、根文档生成与线性链约束由 agent-core 的 `openjiuwen/symphony/flow` 和其 `S_01_skillpack-members` 规约维护；发布、成员下载和安装事务属于 SkillManager/Marketplace。

`SymphonyToolkit.get_tools()` 只有在 Symphony 已启用且 `SKILLPACK_PRODUCTIZATION_ENABLED=true` 时才注册 `skillpack_distill`。实际生成候选还要求 evolution flow 已启用。调用准备接口时检查 Core 是否提供 `SkillPackAdapter.validate_members`；旧 SDK 缺少此契约时返回 `core_skillpack_contract_missing`，不得继续用缺少类型检查的候选流程。注册 Tool 本身不等于检查通过。

直达执行由 `SKILLPACK_DIRECT_ROUTE_MODE` 或 `skillpack.direct_route_mode` 控制，值为 `off/shadow/confirm`；环境变量优先，缺省及非法值均为 `off`。`shadow` 仅执行候选判断，仍走原编排；`confirm` 才改变路由。仅在单 Agent 的 `capture_mode=agent` 下生效。

## 沉淀输入与作用域

`skillpack_distill` 接收用户意图、可选候选 ID/版本、`scope` 和 `source_refs`。模型不得提供执行图、成功率、成员类型、文件路径或确认标志。

- 会话身份来自 Host 绑定的 messaging route；`SessionMessagingRouteRail` 在 `skillpack_distill` 实际执行的任务内从 run context 恢复并在调用后清理绑定，适配 supervisor/并发工具调用。缺少真实 session 返回 `trusted_session_required`，不从模型参数推断身份。
- `scope` 缺省 `current_user`，也可指定 `current_session`。用户身份来自 Host route 或服务端固定会话元数据；无法确认用户时收窄到当前会话。
- `source_refs` 只限制已持久化真实 trace ID。不能借此导入用户外的执行记录，且候选全部来源证据都必须符合当前作用域及成员内容快照。
- 同用户跨会话证据可以聚合；其他用户的记录、归属发生变化的 trace、内容已改变的成员均不能成为候选依据。

作用域过滤发生在 Core 聚合和阈值判断之前。`ScopedCandidateStore` 只读原 Flow 的真实证据，按可信身份、`source_refs` 和当前成员快照构造证据视图；派生 recipe、评审和产物保存在 `flow/skillpack_candidates/<scope-key>/`，不改写原 evidence 或原 recipe。另一匿名会话新增同结构记录，不能使已有会话的合格候选消失，也不能借用其记录达到稳定阈值。

作用域 key 包含 Host 解析的用户/会话身份与规范化 `source_refs`。用户作用域仍保留当前会话维度，以隔离只允许本会话认领的历史匿名证据。每个视图复用原 Core 的配置、模型、评审器与 SkillPack adapter；宿主最多缓存 8 个 worker，候选准备、确认、安装串行使用该缓存，淘汰或服务关闭时关闭 worker。

`CandidateScopeStore` 在 Flow 数据目录保存 `skillpack_scopes.json`，用文件锁与原子替换保存 trace 归属、成员内容指纹和待确认记录。同一 trace 的归属和快照不可覆盖。它不接受模型上报的统计或类型。

## 沉淀确认与安装

```text
真实执行持久化 → 服务端记录归属和普通成员类型
→ Tool 检查作用域及内容快照 → Core 聚合、评审并准备候选
→ 返回成员、证据、执行说明和风险 → Host 问题确认
→ 重验当前成员 → 复用 SkillManager 安装 → 保存安装回执
```

候选准备可写 Flow 的候选和临时产物，不写已安装技能目录、启停状态或检索索引。Tool 仅请求确认，没有直接安装或发布方法。展示包括真实候选根名称、成员顺序、证据数量、执行说明和权限风险；供安装核对的摘要及成员内容指纹保存在服务端，不交由模型决定。

确认请求 ID 绑定 recipe、版本、session 和作用域 key。`install_candidate` 必须持有该 session 的服务端待确认记录，按保存的过滤范围重建同一证据视图，并重新检查可信身份与成员仍为可用的普通 Skill、内容未变化。身份或旧确认作用域不一致返回 `evidence_scope_changed_reconfirm`，不能改用全局候选。生成确认和安装所需的异步评审结束后都重验当前身份；安装还须持有共享库存锁再次校验，再同步执行恢复/安装，锁内不得等待模型或网络。成员变化返回 `members_changed_reconfirm`；原始证据不再存在或不再符合原过滤范围时返回 `evidence_changed_reconfirm`，保留 recipe 不得代替证据。客户端传入的 package 摘要不能覆盖服务端记录。确认与安装回执复用既有 Host 问答、Core 评审及 SkillManager 安全安装接口；服务重启后可从原范围恢复，继续执行同样的校验。

产品化开启时，旧的无作用域 `list_experience_candidates`、`request_experience_candidate` 和旧格式安装 request ID 返回 `scoped_skillpack_tool_required`；不得借旧经验 API 绕过新确认链路。关闭开关后仍保留原实验入口；新 Tool 的安装请求返回 `skillpack_disabled`。

安装只创建本地组合，不等于授权发布或成员的外部副作用。已经成功安装的同一确认请求返回保存的结果，不重复安装。

## 直达选择与权威状态

`SkillPackRouter` 先读取 SkillManager 的 `skillpack_inventory_snapshot()`。只有启用、全部成员可用、至少两个不重复普通成员的 Pack 才参与直达。类型、成员和健康状态以该实时快照为准，不以搜索排名或前端标签代替。

自动候选使用本地检索，结合根文档的 `When to use`、`Do not use`、`Required inputs`、`Side effects`、`Final output` 判断。缺少段落或无法确定完整覆盖时回原编排；单成员任务、部分覆盖、查看/比较/推荐请求和否定条件命中不直达。判定结果必须覆盖全部候选，未知、重复或遗漏 ID 均不产生选择。

自动命中先通过既有 `ask_user` 确认。多个完整匹配必须让用户选择；界面选项超过现有四项限制时显示全部候选的文本选择，不截取 Top 1。Picker 显式单选一个健康 Pack 视为确认；混合选择时仍需明确选择完整 Pack 或继续编排。检索或判定失败且尚未确认时走原链路，不自动下载 Hub 资源。

## 直达执行与恢复

确认后生成内存中的 `DirectRouteToken`，锁定 Pack ID、允许成员、库存修订及来源。确认时和执行工具前都重新读取快照；修订或成员状态变化必须重新选择，不静默替换 Pack。

1. 首次 `skill_tool` 只能读取选定 Pack 的根 `SKILL.md`；默认路径或精确的 `SKILL.md` 合法，其他文件不算根加载。
2. 只有工具明确返回 `success=True` 才标记根加载完成；随后 `skill_tool` 只允许声明成员。
3. 直达期间隐藏并拒绝图编排工具。成员已开始后失败不得自动重新编排，也不得用另一个 Pack 重复执行。
4. 原权限及副作用确认仍有效。路由确认只确定执行方案。
5. 活跃请求和其正常问答恢复共享内存 token。Session checkpoint 只保存“未完成直达请求”的拒绝标记，不能据此重建授权。进程或 rail 状态丢失后收到旧恢复输入，禁止执行及编排，要求重新选择；新 query 才开始新路由。

## 验证入口

- `tests/unit_tests/symphony/test_skillpack_tool.py`：真实 Core evidence、用户归属、确认/安装、旧入口阻断、注册和旧 Core 契约。
- `test_experience_flow.py`、`test_toolkit.py`、`test_startup_singleflight.py`：既有沉淀、Tool 和启动回归。
- `tests/unit_tests/agentserver/test_skillpack_direct_route.py`：确认、执行边界、实时状态及真实 DeepAgent checkpoint 恢复。
- `test_symphony_orchestration_needs_input.py` 等既有 rail 测试：原编排链路回归。
