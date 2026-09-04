# 专家团协作规范

jiuwenswarm 专家团（agent_group）的实际调度由 swarm 装配层完成，不是靠 persona 里的"模拟发言"。本规范说明哪些是**包规范强制的硬约束**，哪些是**persona / AGENT.md 里建议的协作约定**。

---

## 一、包规范硬约束（校验器强制，违反则 expert.load 失败）

1. 顶层 `agents` 必须含 `"leader"`（固定主理人位）
2. leader 子包必须有 `AGENT.md`；非 leader 成员**禁止** `AGENT.md`
3. 成员 `agentCard.id` 须等于成员目录名
4. 成员级禁止 `rails` / `subagents`
5. `persona.dir` 指向的目录必须有 `*.md`
6. 所有路径包内相对、禁止逃逸

---

## 二、装配层语义（agent_group.py:load_agent_group_package）

- 顶层 `instruction` → 注入每个成员的 `agent_group_instruction` section（priority 20）
- 顶层 `skills` 共享技能 → 合并进每个成员的 skills（去重保序）
- leader 的 `AGENT.md` → 注入 `agent_group_leader_rules` section（priority 15）
- 成员模板经 agent-core loader 二次解析（双保险）

---

## 三、成员命名规范

Team 型专家团中，每个成员有三个外露字段：`agentCard.name`（展示名）、`agentCard.description`（职业定位）、目录名（id）。三者不应重复。

### name：谐音花名风格

| 规则 | 说明 |
|------|------|
| 建议三个字 | 姓+名，建议三个字，两个字也可 |
| 先是正常人名 | 姓+名结构，不解释也能当名字用 |
| 暗含职能巧思 | 谐音、拆字、典故均可，但不刻意 |
| 不与 description 重复 | name 是"谁"，description 是"做什么" |

**示例（软件开发团队）：**

| description | name | 巧思 |
|-----------|----------|------|
| 交付总监 | 齐活林 | 齐活了(交付完成) |
| 产品经理 | 许清楚 | 需求要说清楚 |
| 架构师 | 高见远 | 高见(架构视野) |
| 工程师 | 寇豆码 | code(代码) |
| QA工程师 | 严过关 | 严格过关 |

**禁止：**
- 叠字谐音（领码码、需求求）
- 一个字的 name 或纯职能词
- 和 description 重复
- 无意义随机名（张三、John Doe）
- 目录名（id）用中文名

### 主理人 description 规范

主理人的 `description` 不能用通用 title（团长、主理人、Team Lead），应体现该团队的调度风格和业务定位。

**示例：**

| 专家团 | 主理人 name | description |
|--------|-----------|---------------|
| 软件开发团队 | 齐活林 | 交付总监 |
| 交易分析团队 | 何执舟 | 首席策略官 |
| 营销战役团队 | 营销总监 | 增长操盘手 |
| 深度研究团队 | 顾全之 | 研究主编 |

---

## 四、协作铁律（写入 leader AGENT.md）

### 4 条正则

1. **建立团队**：任务开始时由主理人建立协作边界，明确各成员职责。
2. **调度成员**：按 SOP 阶段将成员拉入协作、下发独立任务；成员作为独立协作方输出专业产出，不得由主理人代写。
3. **消息中转**：成员产出回传给主理人，由主理人汇总、转交下一阶段；所有跨成员信息流必须经主理人中转，不得互相直连。
4. **成员结论为准**：任何专业产出必须由对应成员输出后再采信，主理人只做编排与汇编。

### 5 条红线

-  禁止跳过建团，直接自己模拟成员发言或并行写出多角色内容
-  禁止自己代写任何团队成员的专业产出
-  禁止未完成前序阶段就跳到后续阶段
-  禁止让成员互相直连通信，所有跨成员信息流必须经主理人中转
-  禁止主理人自己调度自己（编排、汇总、决策由主理人亲自完成）

### 协作规则

1. 所有成员调度必须经过"建立团队 → 调度成员 → 成员回传"流程
2. 每阶段结束后，将完整产出原文传递给下一阶段成员
3. 调度成员时使用成员的 **id**（`agents/` 下的目录名），禁止使用中文名或自创名称
4. 裁决型角色（如研究主管、风险主管）必须给出明确结论，不得回避决策
5. 每完成一个阶段向用户简要通报进度
6. 所有输出使用与用户原始需求相同的语言

> 注：jiuwenswarm 的实际多角色调度由 swarm runtime（`assembly.py` 七步装配 + `TEAM_MEMBER_IDENTITY` rail）完成，不是靠 leader "自己模拟成员发言"。AGENT.md 里的铁律描述**期望的调度形态**，swarm 会遵循——不需要在 persona 里写"我来 spawn 某成员"的实现细节。

---

## 五、SOP 工作流编排

### 并行 vs 串行

- **并行 Phase**：同一条消息中调度多个成员，适用于成员间无数据依赖
- **串行 Phase**：等前一 Phase 全部回传后，将结论传入下一 Phase 的成员 prompt

### 示例 SOP

```
Phase 1（并行）：
  stock-researcher → 基本面+财务
  valuation-pricer → 估值判断
  money-tracker   → 资金态度

Phase 2（串行，Phase 1 结论传入）：
  risk-doctor → 综合风险诊断

主理人汇编 → 输出
```

### 预设 Workflow 设计原则

每个 Workflow 写明：
- **触发条件**：什么类型的问法匹配此 Workflow
- **Phase 编排**：分 Phase 的串并行调度
- **输入输出依赖**：每个 Phase 的输入来自哪里、输出传给谁

Workflow 数量根据用户实际高频场景设计，不是越多越好。

---

## 六、与 swarm 装配的关系

> 专家团被 `expert.load` 加载后，由 `agents/swarm/assembly.py` 的专家团组装七步
> 装配为 TeamAgentSpec：基础 spec enrich → 团覆写（leader/teammate + 共享 skills）→
> 身份文本渲染。persona 的协作约定会进入 leader/teammate 的系统提示词，但**实际
> 的多进程/多通道调度由 swarm runtime 完成**，不是靠 leader "自己模拟成员发言"。

因此 persona / AGENT.md 里的协作铁律应描述**期望的调度形态**，而非"我来 spawn 某成员"的实现细节（实现由 swarm 接管）。
