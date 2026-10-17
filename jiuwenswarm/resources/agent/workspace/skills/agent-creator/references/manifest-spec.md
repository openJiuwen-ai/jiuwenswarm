# manifest.json 字段规范

manifest 是整个 agent 模板包的总清单，声明包的元信息、组件路径和展示字段。加载器读它来定位各组件。


## 顶层字段

| 字段               | 类型 | 必填 | 说明 |
|------------------|------|----|------|
| `version`        | string | 是  | 版本号。`create` 固定 `"1.0.0"` |
| `package_type`    | string | 是  | 固定 `"agent_template"` |
| `name`            | string | 是  | 唯一标识，必须等于包目录名（agent-name，kebab-case） |
| `description`     | string | 是  | 专家包的一句话描述 |
| `persona`        | `{dir}` | 是  | persona 目录相对路径，固定 `{"dir": "./persona"}` |
| `skills`         | array | 否  | 每项 `{"dir": "./skills/<name>", "mode": "all"}`。无则**整段省略** |
| `tools`          | array | 否  | 每项见下方「tools / rails 条目」。无则**整段省略** |
| `rails`          | array | 否  | 每项见下方「tools / rails 条目」。无则**整段省略** |
| `display_name`    | `{en, zh}` | 是  | 专家列表/详情页标题 |
| `display_description` | `{en, zh}` | 是  | 能力介绍；中文**建议** 40-50 字（非强制，偏离时 validate 仅 warning，不阻塞 register） |
| `category`       | string | 是  | 专家分类；用于列表页筛选，见下方「category 取值」 |
| `avatar`         | string | 是  | 头像路径；本 skill **固定写空字符串** `""`，不生成 `avatars/` 目录 |
| `source`         | string | 是  | 来源标识，**固定** `"local"` |
| `default_init_input` | `{en, zh}` | 是  | 首次对话引导语，须与 `quick_inputs` 第一条一致 |
| `tags`           | `[{en,zh}]` | 是  | 擅长领域标签，固定 3 个 |
| `quick_inputs`    | `[{en,zh}]` | 是  | 推荐提示词，固定 3 个 |

> `persona` **必填**。`skills` / `tools` / `rails` 没生成对应文件就**整段省略**，不要留空数组 `[]`，也不要声明指向不存在的路径。

> **本 skill 禁止生成或声明**：`agent_card`、`model`、`mcps`、`subagents`。

## tools / rails 条目

声明了 `tools[]` / `rails[]` 时，**每一项**除加载字段外必须带前端展示字段（详情页能力卡片用）。加载器只消费 `file`/`class`，展示字段不影响热加载。

| 字段 | 类型 | 必填 | 说明 |
|------|------|----|------|
| `file` | string | 是 | 包内相对路径，如 `tools/<name>_tool.py` / `rails/<name>_rail.py` |
| `class` | string | 是 | PascalCase，与 `.py` 中类名完全一致 |
| `display_name` | `{en, zh}` | 是 | 卡片标题；优先与 `ToolCard.name` / rail 逻辑名一致（如 `content_analyzer`） |
| `display_description` | `{en, zh}` | 是 | 卡片简介，中英各一句，说明用途 |

```json
{
  "file": "tools/content_analyzer_tool.py",
  "class": "ContentAnalyzer",
  "display_name": { "en": "content_analyzer", "zh": "content_analyzer" },
  "display_description": {
    "en": "Analyzes copy metrics: word count, reading time, keyword density, and title scoring.",
    "zh": "内容分析工具：统计字数与阅读时长、评估关键词密度与标题评分。"
  }
}
```

> **skills 不要写 `display_name` / `display_description`**：技能卡片名与简介来自各目录 `SKILL.md` frontmatter 的 `name` / `description`。

## category 取值

`category` 为**单语字符串**，按专家领域择一，常用值：

| 值 | 适用场景 |
|----|----------|
| `Design` | 内容创作、视觉/文案、策划类 |
| `Engineering` | 代码、架构、工程效率类 |
| `Life` | 健康、生活、个人成长类 |
| `IndustryConsultant` | 垂直行业顾问（如职场教练、法务、财务） |

无合适枚举时直接使用 `Others`。

## 最小模板

`init_template.py` 生成展示字段 + persona 骨架（带 `[TODO]`）。`skills` / `tools` / `rails` 在填充阶段按需追加。

```json
{
  "version": "1.0.0",
  "package_type": "agent_template",
  "name": "{agent-name}",
  "description": "{一句话描述}",
  "persona": {
    "dir": "./persona"
  },
  "display_name": {
    "en": "{EN display name}",
    "zh": "{中文展示名}"
  },
  "display_description": {
    "en": "{EN description}",
    "zh": "{中文描述，建议 40-50 字}"
  },
  "category": "{category}",
  "avatar": "",
  "source": "local",
  "default_init_input": {
    "zh": "{中文引导语}",
    "en": "{English prompt}"
  },
  "tags": [
    { "en": "{Tag1 EN}", "zh": "{标签1}" },
    { "en": "{Tag2 EN}", "zh": "{标签2}" },
    { "en": "{Tag3 EN}", "zh": "{标签3}" }
  ],
  "quick_inputs": [
    { "en": "{Prompt1 EN}", "zh": "{提示词1}" },
    { "en": "{Prompt2 EN}", "zh": "{提示词2}" },
    { "en": "{Prompt3 EN}", "zh": "{提示词3}" }
  ]
}
```


## 关键约束

1. **`name` = 包目录名**
2. **路径诚实**：`skills` / `tools` / `rails` 只声明已落盘路径；无则整段省略，禁止 `[]`
3. **`default_init_input` 与 `quick_inputs[0]` 中英逐字一致**
4. **填充后不得残留 `[TODO]`**
