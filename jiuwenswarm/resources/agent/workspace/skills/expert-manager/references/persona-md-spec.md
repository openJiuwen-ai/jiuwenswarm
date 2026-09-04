# persona 正文规范

persona 目录下的 `*.md` 是专家的 **identity 正文**，会被 agent-core
`_build_persona_prompt_sections` 全部读取、按相对路径排序拼接，作为 `identity`
prompt section（priority 10）注入系统提示词。

> jiuwenswarm 的 persona.md **没有 frontmatter**（与某些体系的 agent-md 不同）。
> persona 是纯正文，整段作为人设指令注入。工具/skills 由 manifest 声明，不在 persona 里。

## 加载行为（权威：extension_loader.py:_build_persona_prompt_sections）

1. 取 `persona.dir`（包内相对目录）
2. `rglob("*.md")` 收集所有 markdown 文件
3. 按相对路径字典序排序
4. `"\n\n".join(...)` 拼接全部内容
5. 包成 `PromptSectionSpec(name="identity", content={cn, en}, priority=10)`

> 多个 .md 会被合并进**同一个** identity section；不要靠分文件做"多段身份"，
> 它们会按文件名排序后平铺拼接。

---

## 一、单专家 persona

- 位置：`<expert_id>/persona/<name>.md`
- 内容：角色人设、核心能力、工作流程、输出规范、注意事项
- 模板：`templates/agent-persona.md`

### 正文结构：普通 Agent

```markdown
# {角色名称} - {人名/花名}

{角色描述，说明这是谁，擅长什么}

## 核心能力
1. **{能力1}**：{描述}
2. **{能力2}**：{描述}
3. **{能力3}**：{描述}

## 工作流程
1. {步骤1}
2. {步骤2}
3. {步骤3}

## 输出规范
- {规范1}
- {规范2}
- {规范3}

## 注意事项
- {约束或边界条件}
```

---

## 二、专家团 persona

### leader persona（主理人身份）

- 位置：`agents/leader/persona/leader-persona.md`
- 内容：主理人身份 + 团队成员表 + SOP 阶段 + 协作机制
- **额外**：leader 同级还必须有 `AGENT.md`（团队级铁律，注入
  `agent_group_leader_rules` section, priority 15）
- 两者关系：**persona = "我是谁、负责什么编排"；AGENT.md = "团队怎么协作"**

### leader 正文模板

```markdown
# {团队名称} - 主理人 {花名}

{主理人角色描述，负责协调团队完成什么任务}

## 团队成员

### {分组名称}
| 成员 | 名字 | 职责 |
|------|------|------|
| {member-id} | {name} | {职责描述} |

## 标准工作流程（SOP）

### Phase 1: {阶段名}
{调用哪些成员、输入输出说明}

### Phase 2: {阶段名}
{...}

### Phase N: 最终报告
综合所有成员产出，生成最终报告返回用户。

## 协作机制
1. 按阶段将成员拉入协作、下发独立任务
2. 成员产出回传后，由主理人汇总、转交下一阶段
3. 跨成员信息流必须经主理人中转
4. 专业产出以成员结论为准，主理人只做编排与汇编
```

> 协作的"硬铁律 + 严禁行为"写进 `AGENT.md`（见 `team-spec.md`），不在 persona 里重复。

### member persona（成员身份 + 职责）

- 位置：`agents/<member>/persona/<member>.md`
- 内容：成员身份 + 核心能力 + 输出规范 + 回传机制
- **禁止** `AGENT.md`（校验硬规则：非 leader 成员含 AGENT.md 直接报错）

### member 正文模板

```markdown
# {角色名称} - {人名/花名}

{角色描述，说明这是谁，擅长什么}

## 核心能力
1. **{能力1}**：{描述}
2. **{能力2}**：{描述}

## 工作流程
1. {步骤1}
2. {步骤2}

## 输出规范
- {规范1}
- {规范2}

## 回传机制
分析完成后，将结构化结果回传给主理人，由主理人汇总转交下一阶段。
```

### 成员 Prompt 必备结构

每个成员 persona 须包含：

1. **角色定义**：一句话说清"你是谁"
2. **擅长领域**：3-5 个具体能力点
3. **分析框架**：内嵌的分析能力，分步骤流程
4. **数据获取方式**：具体的查询命令或工具调用
5. **结构化输出模板**：表格/分段格式
6. **回传要求**：明确产出回传给主理人

---

## 三、主理人额外要求

### 成员能力清单

主理人 persona 中必须列出每个成员的：
- 成员 id（= `agents/` 下目录名，调度时用此 id）
- 擅长领域（3-5 个具体能力点）
- 典型问法（什么问题该调它）

### 预设 Workflow

针对高频综合性问题设计预设 Workflow，每个 Workflow 写明：
- **触发条件**：什么类型的问法匹配
- **Phase 编排**：分 Phase 的串并行调度
- **输入输出依赖**：每个 Phase 的输入来自哪里、输出传给谁

Workflow 数量根据用户实际高频场景设计，不是越多越好。

### 单 agent 直调路由表

| 问法类型 | 直接调谁 |
|---------|---------|
| 单一维度问题 | 对应成员 |
| 综合性问题 | 走预设 Workflow |

---

## 四、能独立成 agent 的标准

判断标准：**有没有用户会直接问它的问题？**
- 有 → 独立成 agent（成员）
- 没有 → 归入其他 agent

每个 agent 覆盖一个完整的"分析域"，域内多个能力归并进来，但不跨域。跨域协作由主理人通过 Workflow 编排。

---

## 五、书写要点

1. **正文即提示词**：persona 内容会被原样塞进系统提示词，按"写给 LLM 的人设指令"
   组织，不要写 README 式说明（如"这个文件用来…"）
2. **id 一致**：persona 中提到的角色 id 须与 manifest `agentCard.id` / 成员目录名一致
3. **不要声明 tools**：persona 无 frontmatter，不支持 `tools` 字段（工具由 manifest `tools` 声明）
4. **大段参考资料**：放 `skills/<name>/references/`，不要塞进 persona 正文
5. **调度形态而非实现**：persona 里的协作描述应写"期望的调度形态"（建团→调度→中转→汇编），
   不写"我来 spawn 某成员"的实现细节——实际多角色调度由 swarm runtime 接管（见 `team-spec.md`）
