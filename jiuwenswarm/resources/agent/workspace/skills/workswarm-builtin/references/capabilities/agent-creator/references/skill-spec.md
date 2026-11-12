# skill 规范（skills/SKILL.md）

每个 skill 是 `<agent-name>/skills/<skill-name>/` 下的一个目录，核心文件是 `SKILL.md`。

## 设计原则

**重要**：必须参考 skill-creator 原则：

- name/description 要准确描述触发场景，不要使用"办公扩展""需求处理"这类泛名。
- SKILL.md 正文应精简、可操作，只包含 agent 完成该领域任务需要的工作流、规范、示例和验收标准。
- 如果需要 PPT 模板、品牌素材、字体、图片或样例文件，在 `skills/<skill_name>/assets/` 下放置。
- 如果需要较长的品牌规范、版式指南、API 文档或案例库，在 `skills/<skill_name>/references/` 下放置。
- 不要规划 README、安装指南、变更日志等与 skill 运行无关的文档。

## 目录结构

```
skills/<skill-name>/
├── SKILL.md           # 必须
├── scripts/           # 可选 — 可执行脚本
│   └── xxx.py
├── references/        # 可选 — 按需加载的长文参考
│   └── xxx.md
└── assets/            # 可选 — 模板、素材、样例文件
```



## Frontmatter

```yaml
---
name: {skill-name}          # 必填，必须等于目录名（kebab-case）
description: {一句话描述}    # 必填，触发机制：做什么 + 何时用
---
```


| 字段                            | 说明                    |
| ----------------------------- | --------------------- |
| `name`                        | 等于目录名；显式写，勿依赖文件名退化    |
| `description`                 | 准确描述触发场景，避免「办公扩展」这类泛名 |
| `version` / `author` / `tags` | 可选                    |
| `allowed_tools`               | 可选，工具白名单              |


不要在 frontmatter 里声明 `tools` 字段——权限由系统分配。

## 正文

精简、可操作：只写完成该领域任务需要的工作流、规范、示例和验收标准。大段查表/品牌规范放 `references/`，模板素材放 `assets/`。

```markdown
# {Skill 标题}

## 目标

说明这个 Skill 要完成什么，以及最重要的结果要求。

- {核心目标}
- {关键成功条件}

## 工作流

按实际执行顺序描述任务。

### 1. {步骤名称}

- {执行动作}
- {关键约束}
- {必要的判断条件}

如需要详细知识，读取：
`references/{xxx}.md`

如需要确定性处理，执行：
`scripts/{xxx}.py`

### 2. {步骤名称}

...

## 决策规则

仅记录会显著影响执行结果的分支判断。

- 当 {条件 A} 时，{动作 A}
- 当 {条件 B} 时，{动作 B}
- 优先 {方案 X}，除非 {例外条件}

## 输出要求

定义最终结果必须满足的结构或约束。

- {输出形式}
- {必须包含的信息}
- {质量要求}

如有固定产物，使用：
`assets/{template}`
```

若与 Tool 协作：正文写清何时调用哪个 tool、调用顺序与禁止事项；不要试图在 skill 里「假装执行」代码动作。

## 关键约束

1. 文件名必须全大写 `SKILL.md`
2. `name` = 目录名
3. `description` 写触发条件，不要只写空泛能力名
4. 正文别塞大段查表；细节 `@references/xxx.md`
5. `scripts/` 里的脚本须可 `python3 scripts/xxx.py` 直接调用；Python 代码质量见 `@references/code-quality.md`
6. 不要规划 README、安装指南、changelog 等与运行无关的文档

