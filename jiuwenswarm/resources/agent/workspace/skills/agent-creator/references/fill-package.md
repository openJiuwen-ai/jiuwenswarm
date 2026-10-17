# 第三步：填充/更新包内容

包根目录为 `plugins/agent_templates/local/<agent-name>/`。用户只确认核心能力（create）或修改范围（update）；选型与落盘内部完成，不要追问「要不要加 tool/rail」。

本文件由 SKILL 第三步调用，**不区分平行流程**：

| SKILL mode | 本文件怎么用 |
|------------|-------------|
| `create` | init 完成后按下方完整顺序执行 §1→§4 |
| `update` | **跳过 §1**；只按用户确认范围局部改文件；能力增减走 §2–§4；新增 Tool 时还必须回写 persona「工作流程」；不要重写整包 |

## 执行顺序

```
1. 初始化必要文件（清 [TODO]）          ← create only；update 跳过
2. 能力选型
3. 组件生成（按选型落盘）→ 局部验证
4. 回写 manifest（只声明已生成路径）
```

---

## 1. 初始化必要文件

> **update mode 跳过本节。** 已有包不要整文件重填；只改用户要求的字段/正文。


| 文件                  | 参考                             | 要点                                                                                                            |
| ------------------- | ------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| `manifest.json`     | `@references/manifest-spec.md` | 展示字段填实；`display_name` / `category` 必填；`avatar` 固定 `""`；`name` = 目录名；禁止写 `agent_card` / `model` / `mcps` / `subagents` |
| `README.md`         | —                              | 与核心能力一致                                                                                                       |
| `persona/<name>.md` | `@references/persona-spec.md`  | 四段式；工作流程写清能力如何落地                                                                                              |


---

## 2. 能力选型

收集上下文，对每条核心能力判定组件类型。原则：**用最轻量的组件解决问题**；persona 对话即可时选 `none`。

包可含 Rail、Tool、Skill；一条能力可组合多种。

### Rail — 行为拦截与流程控制

继承 `DeepAgentRail`，通过生命周期钩子介入。实现细节见 `@references/rail-spec.md`。

**适合：** 安全/审计/合规拦截；周期性自省纠偏；动态 prompt 增强；工具调用前后置处理

**不适合：** 提供新的可调用动作（用 Tool）；纯知识/流程指导（用 Skill）

### Tool — 可调用的外部动作

继承 `Tool`，通过 `ToolCard` 描述能力，agent 自主决定何时调用。

**能力：** `invoke` / `stream` 执行动作并返回结果；靠 description 告知何时使用  

**适合：** API/CLI/DB/持久化等外部集成；需主动决策「何时调用」；有明确 I/O 的离散操作  

**不适合：** 被动拦截行为（用 Rail）；纯知识传递（用 Skill）；Skill 独立即可时勿强行加 Tool

### Skill — 知识注入与流程指导

目录 `skills/<name>/SKILL.md`（+ 可选辅助文件）。

**能力：** frontmatter + 正文注入领域知识与流程；不需 Python 执行  

**适合：** 领域知识、规范、工作流指南、决策框架  

**不适合：** 需要运行时代码（用 Tool）；需要拦截行为（用 Rail）

### 选型决策树

```
需求是否需要运行时代码执行？
├── 是 → 需要拦截/修改 agent 行为流程？
│   ├── 是 → Rail
│   └── 否 → 需要 agent 主动调用的离散动作？
│       ├── 是 → Tool
│       └── 否 → Rail（被动触发的后台逻辑）
└── 否 → Skill（纯知识/流程指导）
```

### 必须遵守

- 生成文件、调用外部库、封装 API/CLI 或执行明确动作 → 至少含 **Tool**（简单 API/CLI 可仅 Tool，勿强行加 Skill）
- 领域规范、模板原则、生成流程、示例或验收标准 → 含 **Skill**（可单独，也可与 Tool 叠加）
- 生命周期拦截、审计、周期触发、累计状态或动态注入 → 才含 **Rail**

组合补充：

- 复杂任务（规划 + 执行、文件生成）常为 **Tool + Skill**
- 周期提醒/审计且需可查询动作常为 **Rail + Tool**
- 按需选择，不要为完整性强行加 Rail

---



## 3. 组件生成

按 §2 选型结果落盘；`<name>` 用 kebab-case（与能力英文名对应）。实现规范必须参考对应 spec。

涉及文件读写的 Tool/Rail 必须遵守对应 spec 的「运行时路径规则」；禁止组件自己发明产物根或状态根。

| 类型    | 落点                | 规范                          |
| ----- | ----------------- | --------------------------- |
| Skill | `skills/<name>/`  | `@references/skill-spec.md` |
| Tool  | `tools/<name>_tool.py` | `@references/tool-spec.md`  |
| Rail  | `rails/<name>_rail.py` | `@references/rail-spec.md`  |
| none  | —                 | 跳过                          |




### 局部验证

对每个新生成的 `.py`（`tools/`、`rails/`）做语法检查，失败则先修再继续：

```bash
python -c "import ast; ast.parse(open(r'<file>', encoding='utf-8').read())"
```

---



## 4. 回写 manifest

只声明步骤 3 实际存在的路径（create 为新生成；update 为增删后的最终集合）。`version` 字段除外——create 固定写 `1.0.0`；**update 禁止手改 `version`**，由第五步 `register_template.py --bump` 自动递增。

- `skills[]`：`{"dir": "./skills/<name>", "mode": "all"}`
- `tools[]` / `rails[]`：除 `file` / `class` 外，**每项必填**展示字段：

```json
{
  "file": "tools/<name>_tool.py",
  "class": "<PascalCase>",
  "display_name": { "en": "<tool_or_rail_id>", "zh": "<tool_or_rail_id>" },
  "display_description": {
    "en": "<one-line description>",
    "zh": "<一句用途说明>"
  }
}
```

没有的字段整段省略，不要空数组 `[]`。形状见 `@references/manifest-spec.md`。
