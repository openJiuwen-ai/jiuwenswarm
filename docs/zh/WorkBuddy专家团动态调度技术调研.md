# WorkBuddy 专家团动态调度技术调研

## 结论摘要

当前方案需要把“专家图谱”和“执行图”拆成两层：

1. **专家图谱是静态能力图**：用于发现互补专家、形成可复用的专家团候选，表达“谁可能和谁合作”，不规定每次任务必须走哪条边。
2. **执行图是 Query 级临时子图**：每次收到用户自然语言后，由主理人从专家团成员中选择 0～N 位，生成本次任务独有的串行、并行或条件分支计划；未选成员明确标记为 skipped。
3. **模型负责语义判断，代码负责协议约束**：Router/Planner 用模型理解 Query 和选择成员；Executor 只执行结构化、已校验的计划，保证幂等、依赖、权限、产物和恢复语义。

WorkBuddy 的专家团包只声明主理人、成员和 Skill 集合，没有把一条固定 DAG 写进包协议。真实运行中，主理人会结合 Query 和中间结果动态选择成员、改变策略并跳过不必要成员。这种灵活性值得参考，但它也暴露了纯提示词编排的不稳定性：成员跳过可以与包内 SOP 矛盾，任务空间切换会出现状态重建，依赖缺失时可能发生多轮澄清。因此，小艺 Work 不应照搬“完全自由的主理人”，而应实现**结构化动态计划 + 确定性执行器 + 可审计重规划**。

---

## 一、研究范围与证据等级

### 1.1 真实产品观察

- 客户端：本机已登录的 WorkBuddy macOS 5.3.14。
- 观察日期：2026-09-11。
- 专家团：小红书创作专家团 `redfox-xiaohongshu-ops-team`。
- Query：`追踪最新美妆热门和低粉爆款笔记 TOP50 并分析趋势`。
- 会话 ID：`c4ed5a94-1951-4024-a8d4-ff2ca4df5084`。
- 本地会话证据：`~/.workbuddy/projects/Users-wujianyu-WorkBuddy-2026-09-11-09-56-11/c4ed5a94-1951-4024-a8d4-ff2ca4df5084.jsonl`。
- 专家团包证据：`~/.workbuddy/plugins/marketplaces/experts/plugins/redfox-xiaohongshu-ops-team/`。

本地证据分为三类：

| 等级 | 含义 | 本次材料 |
|---|---|---|
| A：直接运行证据 | 工具调用、成员激活、任务和产物事件 | 会话 JSONL、任务文件、产物索引 |
| B：产品包契约 | 专家团声明的成员、Skill、主理人规则 | `plugin.json`、各 Agent Markdown |
| C：界面观察 | 用户实际看到的成员和成品 | WorkBuddy 客户端截图与页面 |

### 1.2 外部资料

外部技术事实只使用产品官方文档或项目官方一手资料。没有公开证据支撑的 WorkBuddy 内部实现，均标记为推断。

---

## 二、WorkBuddy 的产品与包协议

### 2.1 官方定义

WorkBuddy 官方把专家团定义为“协作执行”机制：由多位专家分工协作，团长自动拆解、并行执行并整合交付；用户只需用自然语言描述任务。官方同时提醒专家团往往会产生单专家 3～5 倍的积分消耗，因此系统必须按任务复杂度控制成员数量。[1]

官方开放平台的专家团包结构包括：

- `plugin.json`：团队和市场信息；
- `agents/`：一位主理人和若干成员；
- `skills/`：团队共享 Skill；
- `teamInfo.leadAgent` 与 `teamInfo.memberAgents`：静态成员清单；
- 主理人 Agent Markdown：负责拆解、调度、信息中转与汇总。[2]

**关键观察：官方包 Schema 没有强制的执行 DAG 字段。** 它描述“团队里有哪些人”，而执行顺序主要由主理人的 Agent 指令、SOP 和运行时工具调用决定。这是动态调度能够成立的基础。

### 2.2 本地小红书专家团结构

本机包声明：

| 角色 | Agent ID | 能力 |
|---|---|---|
| 主理人 | `redfox-xhs-he` | 意图识别、拆解、调度、汇总 |
| 灵感猎手 | `redfox-xhs-sou` | 热门榜、低粉爆款、搜索、爬取、对标账号 |
| 内容创作者 | `redfox-xhs-bi` | 笔记、改写、标题、封面、违禁词 |
| 账号诊断师 | `redfox-xhs-zhen` | 七维账号诊断 |
| 素材下载员 | `redfox-xhs-cai` | 视频解析下载 |

包内实际列出 13 个 Skill；成员能力通过 Agent frontmatter 中的 `skills` 分配。团队信息与成员展示在 `plugin.json`，工作流语义写在主理人 Markdown 中。

主理人预设三条 SOP：

- 爆款选题流：灵感猎手 → 内容创作者；
- 账号体检流：账号诊断师 → 内容创作者；
- 榜单监测日报：灵感猎手 → 内容创作者。

同时，主理人规则要求 TeamCreate、主理人中转和真实成员执行，并写了“禁止跳过前序阶段”。这说明 WorkBuddy 包作者可以声明推荐流程和强约束，但这些仍然是自然语言策略，不是运行时 Schema 的固定边。

---

## 三、真实案例：主理人如何选择、跳过和重规划

### 3.1 Query 级成员选择

对于 Query `追踪最新美妆热门和低粉爆款笔记 TOP50 并分析趋势`，真实工具链为：

1. 主理人创建 Team；
2. 主理人创建“榜单抓取与趋势分析”任务；
3. 仅激活 `redfox-xhs-sou`，并把用户目标、两项 Skill 和预期产出写入独立任务；
4. `redfox-xhs-bi`、`redfox-xhs-zhen`、`redfox-xhs-cai` 均未激活；
5. 成员把结果通过消息回传主理人；
6. 主理人汇编并交付单一 HTML 成品。

因此，这次运行不是“团队所有成员沿固定 DAG 逐个执行”，而是“主理人从静态成员池选择一个执行成员”。WorkBuddy 在会话中也明确显示：实际参与者是主理人 + 1 个执行成员，其他 3 位成员未调用。

### 3.2 动态跳过

主理人包中的标准 Workflow 3 原本是：

`灵感猎手 → 内容创作者 → 可视化日报`

真实运行却在灵感猎手完成后跳过内容创作者，由主理人直接汇编 HTML。主理人给出的理由是：

- 用户要的是趋势分析，而不是创作笔记；
- 上游产出已经足够完整；
- 再拉内容创作者只会增加一轮等待。

这证明运行时允许根据 Query 目标和阶段结果短路后续成员。它也暴露一个可靠性问题：该行为与包内“禁止跳过”“主理人不得代写专业产出”的文字规则存在张力。也就是说，**WorkBuddy 的灵活性主要来自 LLM 主理人的运行时判断，而不是一套完全可验证的显式路由协议。**

### 3.3 失败后的重规划

灵感猎手最初选择 `xiaohongshu-dailytop` 和 `xiaohongshu-lowtop`，但外部数据能力缺少所需凭据。运行时发生了：

1. 成员向主理人报告阻塞；
2. 主理人向用户澄清数据能力与模型 API 的区别；
3. 主理人向同一成员发送新任务指令，改为公开 WebSearch + 历史缓存；
4. 成员完成替代性趋势分析；
5. 主理人把数据口径写入最终 HTML。

这是一条真实的 `blocked → replan → resume` 路径，说明执行图不是一次生成后永不变化。

### 3.4 串行、并行与组合的产品逻辑

本次真实会话中的主理人说明了四类路由：

| Query 意图 | 选择成员 | 拓扑 |
|---|---|---|
| 找爆款选题并写笔记 | 灵感猎手、内容创作者 | 串行 |
| 诊断账号并生成改进笔记 | 账号诊断师、内容创作者 | 串行 |
| 追踪榜单、分析趋势 | 灵感猎手，必要时内容创作者 | 可短路串行 |
| 下载素材 | 素材下载员 | 单成员 |
| 同时找选题和诊断账号 | 灵感猎手、账号诊断师，随后按需创作 | 前段并行、后段汇合 |

其中“多意图组合时部分并行”是产品对能力的说明；本次直接运行只验证了单成员选择、动态跳过和失败重规划，没有实际跑通并行分支。因此，并行结论应视为产品声明，而不是本次 E2E 事实。

### 3.5 需要避免照搬的缺陷

真实会话还出现了以下问题：

- TeamCreate 后任务列表空间切换，主理人发现原计划任务为空并重新创建；
- 缺失外部凭据时经历了多轮沟通，端到端耗时较长；
- 主理人跳过成员与包内 SOP 冲突；
- 主理人把大量成员结果原文通过消息中转，容易产生上下文膨胀；
- 图中展示的“主理人调度决策逻辑”是对话中临时生成的 SVG，不是 WorkBuddy 内置的实时运行图。原生 UI 的直接证据是参与成员状态胶囊和任务/产物区域。

这些问题说明：动态调度要保留，但不能只靠提示词自觉。

---

## 四、与业界主流方案的交叉验证

### 4.1 OpenAI Agents SDK：经理式编排与 Handoff

OpenAI 官方把多智能体编排分为两类：

- **Agents as tools**：经理保留对用户会话的控制权，按需调用一个或多个专家，统一汇总最终答案；
- **Handoffs**：路由 Agent 把当前会话控制权交给专家，由专家继续处理。

官方还明确区分 LLM 编排和代码编排：LLM 适合开放任务的动态选择；代码编排更可预测，可负责结构化分类、链式执行、并行执行和评估循环；二者可以混合。[3]

小艺 Work 专家团更适合经理式模式：主理人始终是最终对话 Owner，成员是有独立上下文的可调用执行单元，不应把用户会话控制权永久交给成员。

### 4.2 Anthropic：Orchestrator-Worker 与按复杂度扩缩

Anthropic 的多智能体 Research 使用 lead agent 根据 Query 制定策略并动态创建多个并行子 Agent。官方工程复盘指出：开放研究任务无法硬编码固定路径；需要根据中间发现持续调整。与此同时，多智能体通常比普通聊天使用约 15 倍 Token，只适合高价值、可并行任务。[4]

其对小艺 Work 最有价值的实践包括：

- 简单查询只用 1 个 Agent；对比类任务用 2～4 个；复杂研究才扩展到更多成员；
- 调度任务必须包含目标、输出格式、工具/数据源和任务边界；
- 并行适合相互独立的方向；高依赖任务不应强行并行；
- 成员将大产物写入持久化 Artifact Store，只把引用和摘要回传主理人，减少“传话损失”和 Token 膨胀；
- 运行时需要观测、测试和快速迭代，而不是只优化静态 Prompt。

### 4.3 Microsoft AutoGen：基于上下文选下一位成员

AutoGen 的 `SelectorGroupChat` 使用模型根据共享上下文动态选择下一位发言者，并支持：角色描述、候选集过滤、自定义选择函数、禁止连续选择同一成员等控制。[5]

对小艺 Work 的启示是：先用图谱和规则把候选成员缩到 Top-N，再让模型做最终选择，避免把几百个专家同时暴露给主理人。

### 4.4 LangGraph Supervisor：中央调度与层级团队

LangGraph Supervisor 由中央 supervisor 根据上下文和任务要求决定调用哪个专家，并通过工具化 handoff 调度成员；还支持团队嵌套和层级 supervisor。[6]

对小艺 Work 的启示是：专家图谱挖出的可以是“能力团队”或“子团队”，主理人不需要把每个叶子专家都直接纳入一个超大路由表。第一阶段可先做单层团队，协议中预留 `teamRef`，以后再支持团队作为成员。

---

## 五、当前固定 DAG 方案的 Gap

当前实现的优点是确定性强、可恢复、产物边界严格；问题是把“候选团队拓扑”误当成“每次 Query 的必跑拓扑”。代码层面的硬约束包括：

1. 物化时要求 `workflow` 完整覆盖 `memberIds`，且每个成员恰好一次；
2. 要求图连通、只有一个 sink，只有该 sink 可以声明 `finalOutput`；
3. 主理人必须一次性创建完整任务 DAG；
4. scheduler 只负责沿固定 `depends_on` 放行；
5. 成员 Stage Prompt 被固化为“非终点只能交接、终点只能生成唯一主产物”；
6. 前端展示的是候选中的固定 Workflow，不是某次 Query 的实际选择结果。

由此导致：

- 团队成员越多，任何小 Query 都会触发越多无关成员；
- 无法表达“本次只用素材下载员”；
- 无法在上游已足够时跳过下游；
- 无法表达条件分支；
- 无法根据依赖缺失换成员或换工具；
- 单一 sink 约束把“产物生成者”和“最终对话汇总者”混为一谈；
- 专家团挖掘会偏向很短的链，而不是覆盖一类用户需求的能力组合。

因此，固定 DAG 不应删除，而应降级为：

- `playbook`：主理人可参考的典型流程；
- `hardConstraints`：必须满足的依赖、权限和产物契约；
- `softEdges`：协作推荐，不等于必跑边。

---

## 六、推荐协议：Team Router / Planner / Executor

### 6.1 静态 Team Definition

专家团包保存“成员池 + 能力契约 + 推荐 Playbook”，不保存唯一必跑 DAG。

```json
{
  "schemaVersion": "xiaoyi.expert-team.v2",
  "teamId": "xiaohongshu-ops-team",
  "leadAgent": "team-leader",
  "members": [
    {
      "expertId": "content-scout",
      "capabilities": ["trend_search", "topic_discovery"],
      "accepts": ["user_query", "account_profile"],
      "produces": ["content_plan"],
      "requiredDependencies": ["web_search"],
      "estimatedCost": "medium",
      "riskLevel": "low"
    }
  ],
  "playbooks": [
    {
      "id": "discover_then_write",
      "triggerHints": ["找选题并写笔记"],
      "recommendedStages": ["content-scout", "copywriter"]
    }
  ]
}
```

图谱边至少区分：

- `can_handoff`：输出/输入契约可衔接；
- `complements`：能力互补；
- `parallelizable_with`：适合并行；
- `hard_prerequisite`：必须先满足的硬依赖；
- `conflicts_with`：资源或输出冲突；
- `requires_dependency`：Connector、Skill、权限或凭据依赖。

### 6.2 Router

Router 负责低成本缩小候选集，不创建任务。

输入：

- 用户 Query、附件和会话上下文；
- 团队成员能力卡；
- 当前 Connector/Skill 可用状态；
- 预算、截止时间和权限策略。

输出：

```json
{
  "intent": ["trend_analysis"],
  "requestedArtifacts": ["html_report"],
  "complexity": "medium",
  "candidateMembers": [
    {"expertId": "content-scout", "score": 0.94, "reason": "具备榜单与趋势能力"},
    {"expertId": "copywriter", "score": 0.43, "reason": "只有需要成稿时才使用"}
  ],
  "maxMembers": 3
}
```

候选召回建议使用“图谱过滤 + 语义排序”两级机制：先按 capability、I/O 契约和依赖状态过滤，再用模型在 Top-N 中选择。不要让主理人面对整个专家库。

### 6.3 Planner

Planner 把候选成员转为本次 Query 的结构化执行子图：

```json
{
  "schemaVersion": "xiaoyi.expert-team.plan.v1",
  "runId": "run-...",
  "revision": 1,
  "selection": {
    "selected": [
      {"expertId": "content-scout", "reason": "需要实时趋势检索", "confidence": 0.94}
    ],
    "skipped": [
      {"expertId": "copywriter", "reasonCode": "not_needed", "reason": "用户未要求创作笔记"},
      {"expertId": "account-doctor", "reasonCode": "not_needed"}
    ]
  },
  "tasks": [
    {
      "taskId": "run-...-t01",
      "expertId": "content-scout",
      "objective": "检索并分析当前趋势",
      "dependsOn": [],
      "inputs": [{"kind": "user_query"}],
      "produces": [{"kind": "trend_report", "mediaType": "application/json"}]
    }
  ],
  "finalizer": {
    "owner": "team-leader",
    "deliverables": [{"kind": "html_report"}]
  },
  "budgets": {"maxMembers": 3, "maxParallel": 2, "deadlineSeconds": 600}
}
```

稳定 reason code：

- `not_needed`
- `redundant_capability`
- `missing_required_input`
- `dependency_unavailable`
- `conditional_not_met`
- `cost_budget`
- `risk_policy`
- `replaced_after_replan`

Planner 校验条件：

- 只能选择团队成员；
- 只为 selected 成员创建任务；
- DAG 无环，依赖只引用本 Plan 中任务；
- 无依赖任务可并行；
- 条件节点必须声明可判定的 predicate；
- 所有输入必须来自用户、已有 Artifact 或上游输出；
- “最终回复 Owner”只能有一个，但最终 Artifact 可以有多个；
- 专业工作任务默认至少选择一位成员，介绍/澄清类 Query 允许 leader-only；
- 高风险工具继续走现有权限审批，不因专家团放宽。

### 6.4 Executor

Executor 不再理解业务语义，只执行校验后的 Plan：

1. 原子持久化 Plan revision；
2. 创建 selected tasks；
3. 所有 ready tasks 并行派发；
4. 成员把大产物写入共享 Artifact Store，只回传 `artifactRef + summary + status`；
5. 条件满足后创建或放行下游；
6. 阻塞、失败或结果超预期时触发 Replanner；
7. 主理人读取 Artifact 引用，生成最终答复和用户可见成品。

建议事件协议：

```text
run.planned
member.selected / member.skipped
task.ready / task.started
artifact.produced
task.blocked / task.failed
run.replan_requested / run.replanned
task.skipped / task.completed
run.synthesizing / run.completed
```

每个事件带 `runId`、`planRevision`、`taskId`、`expertId`、`causationId` 和时间戳。幂等键可使用 `runId + revision + taskKey`，恢复后不得重复创建成员或重复产生外部副作用。

### 6.5 Replanner

触发条件：

- 外部依赖不可用；
- 成员返回 `insufficient_evidence`；
- 产物质量门禁失败；
- 中间结果已经满足下游目标，可以短路；
- 发现新的必要子任务；
- 用户中途修改范围。

重规划不篡改旧 Plan，而是生成 `revision + 1`；旧任务保持可追溯，未执行任务转 `skipped/cancelled` 并记录原因。这样既保留 WorkBuddy 的灵活性，也避免提示词“悄悄改流程”。

---

## 七、专家图谱与专家团挖掘应如何变化

### 7.1 图谱用于发现能力组合，不用于锁死执行顺序

专家团候选的质量标准建议从“是否存在完整单 sink DAG”改为：

- 是否覆盖 2～4 个高频用户 Job；
- 成员能力是否互补而非高度重复；
- 是否存在至少一条可验证的 I/O handoff；
- 是否存在可并行的独立能力；
- 主理人是否能用简单 Query 区分各成员；
- 依赖是否可用或有明确降级方案；
- 典型任务的实际激活成员数是否明显小于团队总人数。

建议专家团规模：主理人 + 3～7 位成员。成员很多时按二级能力簇检索，不全部暴露给 Planner。

### 7.2 Graph Mining 输出

候选团队应输出：

- `memberPool`：可选成员；
- `capabilityCoverage`：覆盖的高频意图；
- `softEdges`：互补、可交接、可并行关系；
- `hardConstraints`：真正不可违反的前置依赖；
- `playbooks`：2～5 条常见 Query 的推荐流程；
- `routingExamples`：简单 Query → 预期成员子集；
- `dependencyReadiness`：Skill/Connector 当前可用情况。

这样可以先放宽专家挖掘门禁、扩大专家库，同时把严格校验集中到每次运行生成的 Plan，而不是限制候选团队必须是完美固定链。

---

## 八、前端：Symphony 能力图 + 动态执行子图

### 8.1 两种视图必须分开

**能力图（构建态）**

- 使用与 Symphony 类似的力导向/关系网络布局；
- 支持缩放、拖拽、搜索、关系强度过滤、聚类和自动调整视图；
- 节点是专家，候选专家团用轮廓/Hull 或选区高亮，不要把专家团再混成普通专家节点；
- 点击专家后在右侧显示简介、原始 Skill、输入输出、依赖、可组团状态；
- 大图默认做 LOD：弱边隐藏、同类聚类、只展开选中节点邻域，避免几十个节点全部连线。

关系视觉建议：

| 关系 | 样式 |
|---|---|
| 已验证 handoff | 蓝色实线 |
| 能力互补 | 灰色虚线 |
| 可并行 | 双向细线或并行标记 |
| 硬依赖 | 带箭头深色线 |
| 依赖不可用 | 橙色警告线 |

**执行图（运行态）**

- 收到 Query 后再生成；
- 未选成员淡化，并可查看 skipped reason；
- selected 节点高亮，实际任务边加粗；
- 同一层无依赖任务横向排列，清晰表达并行；
- 条件边使用虚线并显示 predicate；
- 状态颜色：planned 蓝、running 橙、completed 绿、blocked 红、skipped 灰；
- Replan 时保留旧 revision 的淡色轨迹，突出新增、替换和跳过节点；
- 主理人始终固定在顶部或中心，最终 Artifact 汇聚到“交付”节点。

### 8.2 右侧解释面板

运行态右侧建议展示：

- 原始 Query 和识别出的目标；
- 为什么选这些成员；
- 为什么没选其他成员；
- 当前串/并行结构；
- 任务、Artifact、阻塞与重规划时间线；
- 预算：计划成员数、实际成员数、耗时和 Token；
- 最终交付文件。

这比只展示“固定调用链”更能让用户理解专家团的价值，也能帮助研发定位误路由。

---

## 九、验证用 Case

### Case 1：单成员路由

Query：`帮我下载这个小红书视频：<链接>`

预期：只选择素材下载员；其他成员 skipped；无多余内容创作任务。

### Case 2：两成员串行

Query：`帮我找3个适合新手露营灯的小红书选题，再写成1篇笔记。`

预期：灵感猎手 → 内容创作者；第二个任务依赖第一份 `content_plan`。

### Case 3：两成员并行

Query：`分析这个露营账号的问题，同时找3个同赛道最近的爆款选题，最后给我改进建议。`

预期：账号诊断师和灵感猎手并行；主理人汇总；未要求成稿时跳过内容创作者。

### Case 4：条件分支

Query：`先诊断这个账号；如果适合做露营内容，再帮我写一篇露营灯笔记。`

预期：先运行账号诊断师；只有 predicate `fit_for_camping=true` 才选择/放行内容创作者，否则标记 conditional_not_met。

### Case 5：混合并串行

Query：`找3个露营灯爆款选题并写成笔记，同时把这个参考视频下载下来。`

预期：灵感猎手与素材下载员并行；内容创作者依赖灵感猎手；最终主理人汇总文案和下载产物。

### Case 6：依赖阻塞与重规划

Query：`追踪最新美妆热门和低粉爆款 TOP50，做成趋势网页。`

前置：榜单 Connector 不可用。

预期：成员报告 `dependency_unavailable`；主理人选择询问用户或显式降级到公开搜索；Plan revision 增加；最终成品标明数据口径，不能伪造 TOP50。

### Case 7：Leader-only

Query：`你们团队分别能做什么？`

预期：主理人直接介绍，不激活成员、不创建执行任务。

### Case 8：模糊需求先澄清

Query：`帮我优化一下。`

预期：Router 置信度低；主理人先问对象与目标，澄清前不创建成员任务。

### 核心指标

| 指标 | 定义 |
|---|---|
| 成员选择准确率 | selected set 与预期 set 的一致性 |
| 无关成员激活率 | 不必要成员实际运行数 / 可选成员数 |
| 依赖正确率 | 串并行、条件边和输入产物是否符合 Plan |
| 动态恢复率 | blocked 后是否能重规划并完成或诚实终止 |
| Plan/执行一致率 | 实际事件图是否可由 Plan revision 完整解释 |
| 成品契约通过率 | 用户要求的类型、数量和质量是否满足 |
| 成本收益 | 相比全员固定 DAG 的 Token、延迟和成功率变化 |
| 恢复幂等性 | 重启/重试后是否无重复任务与重复副作用 |

验收门槛建议先轻后严：首版重点确保 Query 易懂、路由合理、成品吸睛和无明显错误；但成员选择、权限和不可伪造的数据依赖必须严格。

---

## 十、分阶段落地建议

### Phase A：先解开固定 DAG

- 保留现有 `scheduled` Executor；
- 把物化时“覆盖所有成员”改为运行时 Plan 校验“只覆盖 selected 成员”；
- 把固定 workflow 改成 `playbooks + hardConstraints`；
- 主理人首次只创建本次 selected tasks；
- 支持 `skipped`、`conditional` 和 Plan revision。

### Phase B：接入图谱召回

- Graph 根据 Query 返回 Top-N 候选成员及关系证据；
- Planner 只在 Top-N 中选成员；
- 记录选中/跳过理由，形成可评估数据；
- 用简单高频 Case 调整路由，不按单一 benchmark Query 写特判。

### Phase C：动态执行图前端

- 迁移/复用 develop 的专家团原有 UI；
- 能力图改为 Symphony 风格网络；
- 新增 Query 级 execution overlay、并行层、跳过原因和 replan 时间线；
- 用户可从图谱候选直接生成团队，再从团队详情发起真实 Query。

### Phase D：规模化挖掘与 Eval

- 扩大专家库后再挖团队；
- 用 Query suite 覆盖单成员、串行、并行、条件、降级、Leader-only；
- 比较 `固定全员 DAG`、`纯 LLM 主理人`、`Hybrid Router/Planner/Executor` 三组；
- 关注真实成品质量，同时统计路由成本和无关成员激活率。

---

## 十一、确定事实、推断与待验证项

### 已验证事实

- WorkBuddy 5.3.14 的专家团包声明主理人和成员池，不包含强制执行 DAG Schema。
- 本地小红书专家团有 1 位主理人和 4 位成员，包内列出 13 个 Skill。
- 真实 Query 只激活了灵感猎手 1 位成员；其他成员未执行。
- 原 SOP 包含内容创作者，但真实运行跳过了该成员。
- 外部依赖失败后，主理人重规划了同一成员的任务。
- 成员结果回传主理人，由主理人交付 HTML。

### 有证据支持的架构推断

- WorkBuddy 的运行时执行拓扑主要由主理人 LLM 根据 Prompt、Query 和中间状态生成；TeamCreate/Agent/SendMessage 提供执行原语。
- 调度策略的强制程度有限，部分规则由 Prompt 软约束，而非 Schema/Executor 硬约束。
- WorkBuddy 的截图式调度图不是内置运行图，而是对话内生成的可视化说明。

### 仍待验证

- WorkBuddy 多成员并行任务的真实调度时序和失败传播；
- 多轮会话中是否会复用已激活成员；
- 成员最大并发、超时、自动重试和持久化恢复的具体实现；
- 官方市场不同专家团是否采用同一套主理人提示词规范；
- WorkBuddy 内部是否还有未公开的路由评分、预算或安全策略。

---

## Sources

### 本地证据索引

| 证据 | 位置 | 说明 |
|---|---|---|
| WorkBuddy 版本 | `/Applications/WorkBuddy.app/Contents/Info.plist` | `CFBundleShortVersionString=5.3.14` |
| 专家团静态包 | `~/.workbuddy/plugins/marketplaces/experts/plugins/redfox-xiaohongshu-ops-team/.codebuddy-plugin/plugin.json` | 第 9～35 行：Agent 与团队成员；第 16～29 行：13 个 Skill；第 66～71 行：市场成员卡 |
| 主理人 SOP | `~/.workbuddy/plugins/marketplaces/experts/plugins/redfox-xiaohongshu-ops-team/agents/redfox-xhs-he.md` | 第 17～38 行：成员与三条 Workflow；第 40～61 行：调度规则 |
| 真实会话 | `~/.workbuddy/projects/Users-wujianyu-WorkBuddy-2026-09-11-09-56-11/c4ed5a94-1951-4024-a8d4-ff2ca4df5084.jsonl` | 第 38 行：只激活灵感猎手；第 46、49、63、70 行：阻塞和成员回传；第 60 行：replan；第 90、106 行：HTML 写入与交付；第 121、132 行：成员选择解释 |
| 当前固定 DAG | `jiuwenswarm_xiaoyi_expert_graph/jiuwenswarm/server/runtime/expert/team_materializer.py` | 第 216～234 行：一次性完整 DAG；第 294～367 行：全成员、连通、单 sink；第 488～541 行：固定阶段产物边界 |

### 外部一手资料

1. WorkBuddy，《[专家](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Expert-Center)》，官方产品文档，访问于 2026-09-11。
2. WorkBuddy 开放平台，《[专家团](https://open.workbuddy.cn/docs/expert-team)》，官方开发文档，访问于 2026-09-11。
3. OpenAI，《[Agent orchestration — OpenAI Agents SDK](https://openai.github.io/openai-agents-python/multi_agent/)》，官方 SDK 文档，访问于 2026-09-11。
4. Anthropic，《[How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)》，2025-06-13。
5. Microsoft AutoGen，《[Selector Group Chat](https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/selector-group-chat.html)》，官方文档，访问于 2026-09-11。
6. LangChain，《[@langchain/langgraph-supervisor](https://langchain-ai.github.io/langgraphjs/reference/modules/langgraph-supervisor.html)》，官方 API 文档，访问于 2026-09-11。
7. Tencent Cloud，《[WorkBuddy 产品页](https://cloud.tencent.com/product/workbuddy)》，官方产品页，访问于 2026-09-11。
