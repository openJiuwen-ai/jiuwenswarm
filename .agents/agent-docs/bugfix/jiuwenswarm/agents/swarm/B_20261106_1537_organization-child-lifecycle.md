---
id: B_20261106_1537_organization-child-lifecycle
title: 群聊组织子任务未领取、遗漏验收与失败重试脱节修复
type: B
author: “ZhangRan-Zora
author_email: “hidadamao@gmail.com”
modules:
  - jiuwenswarm/agents/swarm
  - jiuwenswarm/agents/harness/team/expert_org
files:
  - jiuwenswarm/agents/swarm/group_org_lifecycle.py
  - jiuwenswarm/agents/swarm/group_org_bootstrap.py
  - jiuwenswarm/agents/swarm/registry.py
  - jiuwenswarm/agents/harness/team/expert_org/launcher.py
designs: []
design_note: 根据用户提供的 D1 日志修复既有组织生命周期衔接，保留代理拉起实例，无独立设计。
---

# 群聊组织子任务生命周期修复

## 现象与证据

用户提供的 D1 日志快照中，财务 Owner 在 11:29:56 创建组织子任务后，于 11:30:08 直接派发团内任务，没有领取并开始组织子任务。组织任务在约五分钟后请求描述修订，再过约三分钟因 `description_update_timeout` 失败。后续 v2/v3 未建立修复关系，仍跳过领取。运营和品牌已提交组织结果但未被验收；另一个要求商业策划能力的任务在本次三团组织中无人能领取。日志还出现对团内 ID 调用 `org_review_task`。

这些数据库状态和工具调用已在日志中确认。日志包未携带代码提交号，因此不将运行机器归因为某个确定提交；当前锁定 Core 的创建事件确实跳过创建者团队，与原 Owner 提示词“匹配团队会被唤醒”的笼统描述不一致。

## 决策与范围

本次是启动后的子任务生命周期问题，独立于 [B_20261106_1132_organization-round-cleanup.md](B_20261106_1132_organization-round-cleanup.md) 的启动/取消收尾修复。原有代理 `run_team` 启动实例、追加问题路由和组织卡片规则继续有效。

当前契约维护在 `docs/zh/SwarmChat群聊专家团组织.md` 和对应英文文档第 5.1 节；Swarm 侧此前无该模块独立 S 文档，使用既有链路文档及锁定 Core 的 Rail、OrgTask 与工具接口作为依据。

1. Owner 提示词明确自己的子任务也要领取、开始、提交；组织验收必须使用组织子任务 ID。
2. 新增组织专用 Rail，只读取 Core 任务池并拦截不满足前置条件的工具调用，不维护第二份任务状态，不自动领取/验收，也不延长无人领取超时。
3. 模型继续工作前得到最新组织子任务状态及待验收提醒，避免忙碌时只依赖后台排队唤醒；不增加独立模型请求。
4. 子任务能力限定为当前计划实际成员；同级未修复失败项需要显式 `repairs_task_id`。资格、去重、状态事务仍由 Core 工具决定。已验收后的正常新工作不被一概禁止。
5. Launcher 通过可选 `leader_rails` 把声明写入组织实例 Leader spec；provider 使用构建上下文中的 backend，实例冷恢复能重建检查。复用旧实例时补齐声明并给当前 native 安装一次。普通启动默认不传该选项，Summary Team 和团内成员不安装此 Rail。
6. 组织已结束、无计划、非本轮成员、绑定其他组织或已解除绑定时不干预工具。临时状态读取失败不屏蔽查询、领取、开始、提交等恢复工具。

不采用仅改提示词的方案，因为日志暴露的错误会真实写入任务池；不直接修改 Core 通用领取/验收规则，避免扩展到普通 Team。历史失败任务不自动改成完成，日志目录和运行数据未修改。

## 回归证据

- 候选：`0.2.9`、`45e270a0c` 基线上的本次工作区差异。保留原有 macOS Worker 未提交修复。
- 环境：Python 3.11.3；实际安装与 `uv.lock` 一致的 openJiuwen `f3f8f576164cc405c046e43079c8ad00ee17f0c4`，不是旁边的 editable Core 工作区。
- 新用例使用临时真实 TeamDatabase 和 Core 的创建、领取、开始、完成、验收工具，检查最终记录；无真实模型调用。
- 已验证跳过领取/开始时团内派单无副作用、领取后不会走无人领取过期、真实过期后必须关联修复、修复验收后可完成根任务、错误任务 ID 验收被拒绝、无人可领的任务不落库、重复活动修复仍由 Core 拒绝。
- 已验证完成后的单团追问与无组织路径不受限制，普通实例不安装规则，序列化 RailSpec 通过实际 registry/provider 重建后继续检查当前 backend。
- 定向结果：新生命周期及专家团 Launcher 测试 79 项通过。
- 最终相关回归：下列命令共 591 项通过，包含单专家、单专家团、混合 @、追加问题、组织卡片、团队启动与组织模型边界。

```bash
source .venv/bin/activate
export PYTHONPATH=.
pytest -q --no-cov \
  tests/agents/swarm/test_group_org_lifecycle.py \
  tests/agents/swarm/test_group_org_bootstrap.py \
  tests/agents/swarm/test_group_org_routing.py \
  tests/agents/swarm/test_group_turn.py \
  tests/agents/swarm/test_group_chat.py \
  tests/unit_tests/server/test_group_execution.py \
  tests/unit_tests/agentserver/test_team_helpers.py \
  tests/unit_tests/server/extensions/test_expert_team_launcher.py \
  tests/unit_tests/server/extensions/test_organization_team_models.py
```

按 `jw-review` 对以上候选完成自检：检查实际 provider 注册、声明序列化/重建、模型可见提示、工具拦截副作用、原 Core 状态权威和非组织路径隔离。在该范围未发现待处理的确定问题；该结论不是独立人工评审或真实模型端到端验收。

未运行完整仓库测试、格式化或静态检查；没有使用真实模型重跑原商业任务，因此不声称任意模型一定能收敛或任务时长已经改善。后续变更 Core 依赖、Rail 装配、任务工具或组织路由时需重验上述行为。

## 2026-11-06 复查修订：下级任务权限与工作流收尾

用户要求复查后，使用当前锁定 Core 和临时任务库确认两处遗漏：直属任务已完成、但本团另有已领取并开始的下级任务时，原 Rail 仍拒绝团内派单；组织子任务失败或状态查询异常时，`swarmflow` 的暂停、停止也被当成启动工作拦截。此前 591 项测试及自检未覆盖这两个边界，上述“未发现待处理确定问题”的历史结论不适用于它们。

本次根据用户确认修正同一 Rail，代理启动、卡片和非组织路由不变：

1. 直属子任务仍用于 Owner 的专家拆分完整性、来源验收及修复判断。只有执行权限检查在直属任务不满足条件时，沿 Core 的 `list_child_tasks` 查询下级任务；要求同一根任务、对应专家能力、当前团队实际领取且为 `IN_PROGRESS`。使用无条数截断的子节点查询并记录已访问节点，不新增任务状态或修改 Core。直属任务已满足条件时不增加遍历查询。下级查询异常同样返回可恢复的重试说明。
2. 与 Core `SwarmflowTool.invoke` 的参数语义保持一致：非空 `resume_id` 加 `pause`/`stop` 在读取组织状态前放行，包括参数首尾空白。控制成功与否仍由工作流工具及控制器决定。`resume`、仅带 `resume_id` 的重新启动和普通启动继续经过原执行权限检查。

验证仍对应 `0.2.9`、`45e270a0c` 基线及上节锁定依赖，无真实模型调用：

- 新增 19 项测试，使用真实组织创建、领取、开始及失败工具，核对数据库里的根任务、领取团队和状态。覆盖不同深度的下级任务、未开始任务、跨团队/根任务隔离，以及下级任务不能替代 Owner 必须创建的直属专家任务。
- 暂停/停止通过真实 `SwarmflowTool` 进入可控控制器替身，验证指定运行句柄的控制调用和活动集合变化；不将替身控制器视为真实后台工作流或进程取消的端到端证据。
- 修复前新增边界测试 11 失败、7 通过（当时尚未加入直属拆分反向用例）；失败点对应下级派单、暂停/停止及下级读取异常处理。修复后重复上节完整相关回归命令，610 项通过，日志位于本地 `/private/tmp/org-lifecycle-boundaries-regression.log`。
- 随后进一步收紧不同深度用例：中间节点分配给财务、仅叶子节点分配给运营，确保必须查到叶子才能放行。仅修改测试后重跑受影响的 `pytest -q --no-cov tests/agents/swarm/test_group_org_lifecycle.py -k started_descendant`，6 项通过；生产代码未再变化。
- 未重启服务、未提交，未修改真实任务库、原始日志或 macOS Worker 本地修复。既有真实模型端到端验证限制继续有效。
