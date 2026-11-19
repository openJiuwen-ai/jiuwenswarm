# manifest.json 字段规范（plugin）

manifest 是整个 plugin 插件包的总清单，声明包的元信息、组件路径和展示字段。加载器读它来定位各组件。

## 顶层字段


| 字段                   | 类型          | 必填  | 说明                                                                                |
| -------------------- | ----------- | --- | --------------------------------------------------------------------------------- |
| `version`            | string      | 是   | 版本号。`create` 固定 `"1.0.0"`；`update` 不手改，由 `register_plugin.py --bump` 自动递增 patch 位 |
| `package_type`        | string      | 是   | 固定 `"plugin"`                                                                     |
| `id`                 | string      | 是   | 唯一标识，**必须等于包目录名**（plugin-name，kebab-case）                                         |
| `name`               | string      | 是   | 插件包名                                                                              |
| `description`        | string      | 是   | 插件包的一句话描述                                                                         |
| `display_name`        | `{en, zh}`  | 是   | 插件包展示名                                                                            |
| `display_description` | `{en, zh}`  | 是   | 能力介绍，中文约 40-50 字                                                                  |
| `category`           | string      | 是   | 插件分类；用于列表页筛选，见下方「category 取值」                                                     |
| `source`             | string      | 是   | 来源标识，**固定** `"local"`                                                             |
| `avatar`             | string      | 否   | 头像包内相对路径，与专家包同一字段。有图时写 `"avatars/avatar.png"`；没有则写 `""` 或整段省略 |
| `default_init_input`   | `{en, zh}`  | 是   | 首次对话引导语，须与 `quick_inputs` 第一条一致                                                    |
| `tags`               | `[{en,zh}]` | 是   | 能力标签，固定 3 个                                                                       |
| `quick_inputs`        | `[{en,zh}]` | 是   | 推荐提示词，固定 3 个；写**能力触发场景**                                                          |
| `skills`             | array       | 否   | 每项 `{"dir": "./skills/<name>", "mode": "all"}`。无则**整段省略**                         |
| `tools`              | array       | 否   | 每项见下方「tools / rails 条目」。无则**整段省略**                                                |
| `rails`              | array       | 否   | 每项见下方「tools / rails 条目」。无则**整段省略**                                                |


> **至少声明一类能力**：`skills` / `tools` / `rails` 中至少有一类非空且路径真实存在。

> 没生成对应文件就**整段省略**，不要留空数组 `[]`，也不要声明指向不存在的路径。



## tools / rails 条目

声明了 `tools[]` / `rails[]` 时，**每一项**除加载字段外必须带前端展示字段（详情页能力卡片用）。


| 字段                   | 类型         | 必填  | 说明                                             |
| -------------------- | ---------- | --- | ---------------------------------------------- |
| `file`               | string     | 是   | 包内相对路径，如 `tools/<name>_tool.py` / `rails/<name>_rail.py` |
| `class`              | string     | 是   | PascalCase，与 `.py` 中类名完全一致                     |
| `display_name`        | `{en, zh}` | 是   | 卡片标题；优先与 `ToolCard.name` / rail 逻辑名一致          |
| `display_description` | `{en, zh}` | 是   | 卡片简介，中英各一句                                     |


> **skills 不要写** `display_name` **/** `display_description`：来自各目录 `SKILL.md` frontmatter 的 `name` / `description`。



## category 取值


| 值                    | 适用场景           |
| -------------------- | -------------- |
| `Design`             | 内容创作、视觉/文案、策划类 |
| `Engineering`        | 代码、架构、工程效率类    |
| `Life`               | 健康、生活、个人成长类    |
| `IndustryConsultant` | 垂直行业顾问         |


无合适枚举时直接使用 `Others`。

## 最小模板

`init_plugin.py` 生成展示字段骨架（带 `[TODO]`）。能力组件在填充阶段按需追加。

```json
{
  "version": "1.0.0",
  "package_type": "plugin",
  "id": "{plugin-name}",
  "name": "{中文插件名}",
  "description": "{一句话描述}",
  "display_name": {
    "en": "{EN display name}",
    "zh": "{中文展示名}"
  },
  "display_description": {
    "en": "{EN description}",
    "zh": "{中文描述，建议 40-50 字}"
  },
  "category": "{category}",
  "source": "local",
  "avatar": "",
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

1. `id` **= 包目录名**
2. `default_init_input` **与** `quick_inputs[0]` **中英逐字一致**
3. **路径诚实**：只声明已落盘路径；无则整段省略，禁止 `[]`
4. **填充后不得残留** `[TODO]`

