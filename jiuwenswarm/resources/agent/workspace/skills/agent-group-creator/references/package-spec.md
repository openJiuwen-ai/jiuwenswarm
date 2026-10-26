# 专家团包规范

## 目录

```text
research-team/
├── manifest.json
├── README.md
├── agents/
│   ├── leader/
│   │   ├── manifest.json
│   │   ├── AGENT.md
│   │   └── persona/leader.md
│   ├── researcher/
│   │   ├── manifest.json
│   │   ├── persona/researcher.md
│   │   └── skills/                 # 仅 researcher 使用
│   │       └── source-research/SKILL.md
│   └── reviewer/
│       ├── manifest.json
│       ├── persona/reviewer.md
│       └── skills/                 # 仅 reviewer 使用
│           └── review-evidence/SKILL.md
└── skills/                         # 可选，共享给所有角色
    └── evidence-check/SKILL.md
```

## 根 manifest.json

```json
{
  "name": "research-team",
  "package_type": "agent_group",
  "description": "围绕用户问题完成资料研究、独立复核和综合建议",
  "display_name": {"zh": "研究专家团", "en": "Research Team"},
  "display_description": {"zh": "资料研究与独立复核", "en": "Research with independent review"},
  "instruction": "leader 明确目标并分派任务；researcher 返回研究结论和来源；reviewer 检查证据、矛盾与遗漏；leader 消解分歧后向用户提交完整结果。",
  "agents": ["leader", "researcher", "reviewer"],
  "skills": ["evidence-check"],
  "tags": [{"zh": "研究", "en": "Research"}],
  "quick_inputs": [{"zh": "帮我研究这个问题并复核结论", "en": "Research this question and review the findings"}]
}
```

- `agents` 是角色目录名列表，必须非空、唯一并包含 `leader`，不能写对象、路径或独立专家 ID 来代替包内文件。
- 角色 ID 会成为团队成员名（消息路由键与目录名），首字符必须是小写字母 `a-z`，其后仅允许小写字母、数字、下划线和连字符（推荐 kebab-case，如 `backend-dev-1`）；大写、中文、空格等会被加载器拒绝。
- `instruction` 是共享给所有角色的字符串；写协作规则，不把 Leader 专属操作要求施加给每个成员。
- `skills` 是共享技能目录名列表，列出的技能必须存在。根 `skills/` 下的有效技能目录也会自动加载，因此不要放入无关技能。
- `member_templates` 可选：仅复用真实独立专家模板时填写角色 ID 到模板 ID 的映射，不编造来源。成员仍须完整复制到 `agents/`。
- 展示字段使用 `zh/en`；空的可选字段可以省略。不要把单专家的根 manifest 直接当成专家团 manifest。

## 角色 manifest.json

每个角色均为 `agent_template`，以下是带成员专用技能的 `agents/researcher/manifest.json` 示例：

```json
{
  "package_type": "agent_template",
  "name": "researcher",
  "display_name": {"zh": "资料研究专家", "en": "Researcher"},
  "display_description": {"zh": "检索资料并核验来源", "en": "Research information and verify sources"},
  "description": "负责资料检索、来源核验与研究结论",
  "persona": {"dir": "./persona"},
  "skills": [{"dir": "skills/source-research", "mode": "all"}]
}
```

新建成员的 `name` 与角色目录一致，展示名称放在 `display_name`；运行时角色 ID 取自 `agents` 列表和目录名。persona 必须为包内相对目录且包含 Markdown 文件。Leader 也可使用 `./persona`，加载器会额外挂载该角色根目录的 `AGENT.md`，不必扩大到 `.` 读取所有 Markdown。

每个成员按 [下方成员内容规范](#成员内容规范) 完成能力选型、persona、Skill 与可选 Tool/Rail 内容及自检。不要套用独立专家的安装目录或注册流程。

persona 应写清角色定位、专业范围、输入要求、工作方法、交付格式及不确定性处理。不同成员应有实质不同的职责，不能只替换名字。输出中区分证据、推断和未完成事项。

Leader 的 `AGENT.md` 写目标澄清、任务拆解、分派时的上下文与验收标准、成员结果复核、分歧处理和最终汇总。要求 Leader 将需用户回答的问题直接放在可见正文中，并在等待补充信息时明确说明；不编造工具名称或任务完成状态。

普通成员只用 persona，不放 `AGENT.md`。成员专用 tools/rails 如需扩展，应遵循当前运行时 AgentTemplate 格式并通过真实加载校验；不要复制单专家创建器中的占位配置。

## 技能归属与路径

- **成员专用技能**：文件在 `agents/<角色 ID>/skills/<skill-id>/SKILL.md`；只在该成员的 manifest 中声明 `{"dir": "skills/<skill-id>", "mode": "all"}`。`dir` 相对于该成员目录，不是专家团根目录；不要写 `agents/<角色 ID>/skills/...`、绝对路径或 `../../skills/...`。Leader 专用技能也遵循同一规则。
- **团队共享技能**：文件在根 `skills/<skill-id>/SKILL.md`；根 manifest 的 `skills` 填目录名，例如 `["evidence-check"]`，不是成员 manifest 使用的对象数组。加载器会将根目录共享技能合并到所有角色，因此不能用根 `skills/` 存放仅供某个成员使用的技能。
- 成员没有专用技能时省略该成员的 `skills` 或写 `[]`，不要创建空目录。只在多人都需要该能力时创建团队共享技能，不因“属于同一个专家团”就把成员技能放在根目录。
- 上述示例中 researcher 使用自己的 `source-research` 和共享的 `evidence-check`，reviewer 使用自己的 `review-evidence` 和共享的 `evidence-check`；两个成员不应获得对方的专用技能。每个声明的目录都必须有实际的 `SKILL.md`。


README.md 面向使用者介绍用途、成员分工、适用场景、输入与输出、限制及示例提问。不要包含密钥、绝对机器路径或临时文件引用。

## 成员内容规范

每个 `agents/<角色 ID>/` 都是一个完整的 `agent_template` 包。复用单专家的内容质量要求，路径、身份和生命周期遵循本技能的 [package-spec.md](package-spec.md)。不要调用单专家的初始化、校验或注册脚本，不把成员复制到独立专家安装目录；成员随整个专家团一次校验和注册。

### 1. 按职责选择能力

对每个成员逐项说明：接收的输入、要完成的动作、交付物，以及能力由什么组件提供。已有运行时工具足够时直接复用，不重复生成包装工具。

| 需求 | 选择 |
| --- | --- |
| 角色定位、沟通方式、简单判断 | persona |
| 领域知识、流程、模板、示例、验收标准 | Skill |
| 现有工具不能完成的 API、CLI、数据库等离散动作 | Tool |
| 需要生命周期拦截、审计或动态注入 | Rail |

只有实际需求需要时才组合组件，不要求每个成员都具备 Skill、Tool 和 Rail。不要用纯文本假装执行外部动作，也不要仅为完整性生成空组件。

### 2. 成员身份与展示字段

- `name` 使用与角色目录一致的角色 ID，例如 `researcher`；中文名称放在 `display_name.zh`，不要混用唯一标识与展示名称。
- 提供非空 `description`，并为成员填写双语 `display_name` 和 `display_description`，便于团队界面识别职责。
- `persona.dir` 指向该成员的 `persona/`，正文文件推荐 `persona/<角色 ID>.md`。
- 团内成员不套用独立专家的 `source: local`、独立安装路径、单独版本递增、固定数量的标签或推荐问题要求。这些是独立资产的约定，不是成员加载条件。
- 不生成 `agent_card`、独立 `model`、`mcps` 或 `subagents` 配置；复用已有模板时不扩充用户未授权的连接器或权限。

### 3. persona 与协作

参考单专家的 [persona 规范](../../agent-creator/references/persona-spec.md)，将其中的包名和文件名替换为角色 ID。正文不写 YAML frontmatter，包含角色定位、核心能力、工作流程、输出规范和边界条件。

在成员工作流程中写清：

- 收到什么任务或输入时进入各步骤，资料不足时如何向 Leader 反馈。
- 何时读取哪个 Skill、调用哪个已声明或运行时已提供的 Tool，以及必要的调用顺序。
- 如何检查产物并向 Leader 返回结果、证据、不确定性、错误和未完成事项。

新增或删除组件时，同步调整 persona 的工作流程与 manifest。不要引用不存在的工具、技能或文件。普通成员不写 Leader 专属的团队创建、调度和验收职责；Leader 的额外规则写入自己的 `AGENT.md`。

### 4. Skill 内容

成员专用技能与团队共享技能均遵循单专家的 [Skill 规范](../../agent-creator/references/skill-spec.md)，归属与路径按 [package-spec.md](package-spec.md) 的“技能归属与路径”执行。

- 每个 `SKILL.md` 都有 YAML frontmatter：`name` 等于技能目录名，`description` 说明做什么及何时使用；不声明 `tools` 权限字段。
- 正文包含可操作的步骤、必要决策、输入输出和验收标准，不只重复 persona 的能力描述。
- 长参考放入该技能的 `references/` 并从正文按需链接；模板与素材放入 `assets/`；需要确定性处理时才提供 `scripts/`，写清调用方式。
- 所有引用的辅助文件都随技能复制，路径相对该技能目录，不指向开发机绝对路径或原模板的安装目录。

### 5. Tool、Rail 与脚本

仅在生成对应组件时，读取单专家的 [Tool 规范](../../agent-creator/references/tool-spec.md)、[Rail 规范](../../agent-creator/references/rail-spec.md) 和 [Python 代码质量](../../agent-creator/references/code-quality.md)。复用接口、生命周期、错误处理和运行时路径规则，不复制其中的占位实现。检查工具服从当前宿主的执行约束。

- 文件分别放在 `agents/<角色 ID>/tools/` 和 `agents/<角色 ID>/rails/`；manifest 的 `file` 相对该成员目录，`class` 必须与实际类名一致。
- Tool/Rail 条目提供双语 `display_name`、`display_description`；Skill 的展示信息来自自身 frontmatter。
- 通过现有运行时接口解析产物与状态路径，不向模板包目录写运行数据，不跨会话读取或覆盖其他成员数据。
- 缺依赖、外部服务失败、产物生成失败要返回明确失败，不伪造成功；宣告文件生成成功前检查实际文件与格式。
- 生成的 Python 文件按 UTF-8 读写，检查语法；Tool/Rail 要验证真实组件加载，脚本要运行最小自检。有外部副作用的动作使用临时目录或测试替身，不借校验执行用户业务。

### 6. 注册前逐成员检查

逐一检查 Leader 与所有成员，不以抽查一位成员替代全团检查：

1. manifest 与组件清单一致，双语展示字段已填，无 `[TODO]` 或模板占位内容。
2. persona、专用技能、Tool/Rail 和它们引用的辅助资源都真实存在，路径相对各自所属包。
3. 每个 Skill 的 frontmatter、正文和资源引用完整；根 `skills/` 只含需要共享给全体角色的技能。
4. persona 的调用流程与实际能力一致，输出有可核验的交付物和验收标准；新增代码的自检通过。
5. 使用本技能 `register_group.py --validate-only` 跑到 `RESULT: PASS`，再注册整个专家团。

该脚本自动检查 `[TODO]` 残留和技能 frontmatter，再调用包加载器；这些检查不等同于单专家脚本的全部 L0 质量检查、L1 静态规则和 L2 完整 harness 热加载。上述内容检查和组件自检仍须完成；无法验证的依赖、工具或外部服务必须在交付时说明，不能把包加载通过表述为业务执行通过。
