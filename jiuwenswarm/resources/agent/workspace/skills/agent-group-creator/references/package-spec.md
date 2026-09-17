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
│   │   └── persona/researcher.md
│   └── reviewer/
│       ├── manifest.json
│       └── persona/reviewer.md
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
  "skills": [],
  "tags": [{"zh": "研究", "en": "Research"}],
  "quick_inputs": [{"zh": "帮我研究这个问题并复核结论", "en": "Research this question and review the findings"}]
}
```

- `agents` 是角色目录名列表，必须非空、唯一并包含 `leader`，不能写对象、路径或独立专家 ID 来代替包内文件。
- `instruction` 是共享给所有角色的字符串；写协作规则，不把 Leader 专属操作要求施加给每个成员。
- `skills` 是共享技能目录名列表，列出的技能必须存在。根 `skills/` 下的有效技能目录也会自动加载，因此不要放入无关技能。
- `member_templates` 可选：仅复用真实独立专家模板时填写角色 ID 到模板 ID 的映射，不编造来源。成员仍须完整复制到 `agents/`。
- 展示字段使用 `zh/en`；空的可选字段可以省略。不要把单专家的根 manifest 直接当成专家团 manifest。

## 角色 manifest.json

每个角色均为 `agent_template`，最小示例：

```json
{
  "package_type": "agent_template",
  "name": "资料研究专家",
  "description": "负责资料检索、来源核验与研究结论",
  "persona": {"dir": "./persona"}
}
```

这里 `name` 可以是展示名称；运行时角色 ID 取自 `agents` 列表和目录名。persona 必须为包内相对目录且包含 Markdown 文件。Leader 也可使用 `./persona`，加载器会额外挂载该角色根目录的 `AGENT.md`，不必扩大到 `.` 读取所有 Markdown。

persona 应写清角色定位、专业范围、输入要求、工作方法、交付格式及不确定性处理。不同成员应有实质不同的职责，不能只替换名字。输出中区分证据、推断和未完成事项。

Leader 的 `AGENT.md` 写目标澄清、任务拆解、分派时的上下文与验收标准、成员结果复核、分歧处理和最终汇总。要求 Leader 将需用户回答的问题直接放在可见正文中，并在等待补充信息时明确说明；不编造工具名称或任务完成状态。

普通成员只用 persona，不放 `AGENT.md`。成员专用 skills/tools/rails 如需扩展，应遵循当前运行时 AgentTemplate 格式并通过真实加载校验；不要复制单专家创建器中的占位配置。

README.md 面向使用者介绍用途、成员分工、适用场景、输入与输出、限制及示例提问。不要包含密钥、绝对机器路径或临时文件引用。
