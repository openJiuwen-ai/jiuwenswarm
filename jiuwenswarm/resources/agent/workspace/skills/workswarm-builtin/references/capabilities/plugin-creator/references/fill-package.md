# 第三步：填充/更新包内容

包根目录为 `plugins/plugin_packages/local/<plugin-name>/`。用户只确认核心能力（create）或修改范围（update）；选型与落盘内部完成，不要追问「要不要加 tool/rail」。

本文件由 SKILL 第三步调用，**不区分平行流程**：


| SKILL mode | 本文件怎么用                                     |
| ---------- | ------------------------------------------ |
| `create`   | init 完成后按下方完整顺序执行 §1→§4                    |
| `update`   | **跳过 §1**；只按用户确认范围局部改文件；能力增减走 §2–§4；不要重写整包 |


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


| 文件              | 参考                             | 要点                                                                                                                                   |
| --------------- | ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `manifest.json` | `@references/manifest-spec.md` | 展示字段填实；`id` = 目录名；`name` / `description` 非空；`default_init_input` ≡ `quick_inputs[0]`；`avatar` 可选（`""` 或 `avatars/avatar.png`）；禁止 `persona` / `agent_card` / `model` / `subagents` / `mcps` |
| `README.md`     | —                              | 描述插件**能力**与使用方式（配合专家装备），不写人设                                                                                                         |


### quick_inputs 写法（插件特有）

- 第一条：插件核心入口（常与 `default_init_input` 相同），如建档 + 生成方案
- 后两条：覆盖其它 skill/tool 的典型场景
- **禁止**写「你好，我是 XX 专家」类人设句；写可直接发送的任务指令

---

## 2. 能力选型

收集上下文，对每条核心能力判定组件类型。原则：**用最轻量的组件解决问题**。

包可含 Skill、Tool、Rail；一条能力可组合多种。**至少选一种组件**，纯 manifest 空包无效。

### Skill — 知识注入与流程指导

目录 `skills/<name>/SKILL.md`（+ 可选辅助文件）。

**适合：** 领域知识、规范、工作流指南、决策框架  
**不适合：** 需要运行时代码（用 Tool）；需要拦截行为（用 Rail）

### Tool — 可调用的外部动作

继承 `Tool`，agent 自主决定何时调用。

**适合：** API/CLI/DB/持久化；有明确 I/O 的离散操作  
**不适合：** 被动拦截（用 Rail）；纯知识（用 Skill）

### Rail — 行为拦截与流程控制

继承 `DeepAgentRail`，通过生命周期钩子介入。

**适合：** 安全/审计/合规拦截；周期性自省纠偏；动态 prompt 增强；工具调用前后置处理
**不适合：** 提供新的可调用动作（用 Tool）；纯知识（用 Skill）

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

- 生成文件、封装 API/CLI 或执行明确动作 → 至少含 **Tool**（简单 API/CLI 可仅 Tool）
- 领域规范、模板原则、工作流指南 → 含 **Skill**（可单独，也可与 Tool 叠加）
- 生命周期拦截、审计、安全边界 → 才含 **Rail**
- 按需选择，不要为完整性强行加 Rail

组合补充：

- 复杂任务（规划 + 执行）常为 **Tool + Skill**
- 安全边界 + 工具调用常为 **Rail + Tool**

---

## 3. 组件生成

按 §2 选型结果落盘；`<name>` 用 kebab-case。实现规范必须参考对应 spec。


| 类型    | 落点                     | 规范                          |
| ----- | ---------------------- | --------------------------- |
| Skill | `skills/<name>/`       | `@references/skill-spec.md` |
| Tool  | `tools/<name>_tool.py`      | `@references/tool-spec.md`  |
| Rail  | `rails/<name>_rail.py`      | `@references/rail-spec.md`  |
| none  | —                      | 跳过                          |


涉及文件读写的 Tool/Rail 必须遵守对应 spec 的「运行时路径规则」。

### 局部验证

对每个新生成的 `.py` 做语法检查：

```bash
python -c "import ast; ast.parse(open(r'<file>', encoding='utf-8').read())"
```

---

## 4. 回写 manifest

只声明步骤 3 实际存在的路径。`version`：create 固定 `1.0.0`；**update 禁止手改**，由 `register_plugin.py --bump` 递增。

- `skills[]`：`{"dir": "./skills/<name>", "mode": "all"}`
- `tools[]` / `rails[]`：每项必填 `file` / `class` / `display_name` / `display_description`

没有的字段整段省略，不要空数组 `[]`。形状见 `@references/manifest-spec.md`。
