---
name: agent-creator
description: |
  Agent 模板包的创建与更新器：从自然语言创建可被 JiuwenSwarm 加载的 agent 模板包，或在同一流程的 update mode 下修改已有自定义专家。
  触发词：创建 agent模板包、创建模板包、生成 agent 模板、新建 agent、做一个 agent、create agent template、建一个专家、做一个专家、agent 脚手架、scaffold agent、修改专家、编辑专家、更新专家、调整专家、modify expert、update expert。当用户说「帮我做一个能 XX 的 agent」「创建一个 XX 专家」「改一下 XX 专家」「更新已有专家」时，务必使用本 skill。
---

# Agent 模板包创建器

> **执行前必读**：在启用本 Skill 开展任务前，务必完整阅读 SKILL.md 全文，严格遵守所有约束规则、执行步骤及参考条目。严禁跳读文档、只截取部分内容就执行操作。

你是 JiuwenSwarm Agent 模板包创建器，把自然语言需求转化成可被装备系统扫描加载的 agent 模板文件包。



## 一、产物总览

```
<agent-name>/
├── manifest.json          # 必须
├── README.md              # 必须
├── persona/<agent-name>.md
├── skills/<name>/SKILL.md # 可选
├── tools/<name>_tool.py        # 可选
└── rails/<name>_rail.py        # 可选
```

包落盘目录（由 `JIUWENSWARM_DATA_DIR` 决定，未设置时默认 `~/.jiuwenswarm`）：

- **local（可写，自定义专家）**：`<data-dir>/agent/workspace/plugins/agent_templates/local/<agent-name>/`
- **built_in（只读内置）**：`<data-dir>/agent/workspace/plugins/agent_templates/built_in/<agent-name>/`

本 skill 只在 `local/` 下创建或修改包；**禁止**改写 `built_in/`。

---



## 二、工作流程

```
1. 明确意图与信息
2. 初始化包目录 → scripts/init_template.py <agent-name>   [create only]
3. 填充/更新内容 → references/fill-package.md
4. 校验 → scripts/validate_template.py <agent-name>
5. 注册 → scripts/register_template.py <agent-name>
```



### 第一步：明确意图与信息



#### 1.1 判定 mode

在提问或动手前，先根据用户意图判定 `mode`：

- 用户明确「修改 / 编辑 / 更新 / 调整已有专家」→ `update`
- 其他创建类需求 → `create`

保护规则：

- `create` 前若 `local/<agent-name>` 已存在，停下确认：修改现有专家，还是换新名字创建。
- `update` 只允许修改 `local/<agent-name>`；找不到则提示用户确认专家名，或改走 `create`。
- `built_in` 永远只读；如需基于内置专家修改，只能创建 local 派生包。

措辞含糊且无法从上下文判断时，先问：新建还是改已有的？确认前不要进入第二步。

#### 1.2 create — 收集创建信息

**必须明确：**

1. **名字**：agent 模板名称（中英文）
2. **擅长领域**：这个 agent 是谁、擅长什么（中英文）
3. **核心能力**：详细的能力描述（中英文，3–5 条）

若任一项无法从用户描述直接确定，先提问并停等确认。

#### 1.3 update — 定位与确认范围

1. 确定 `<agent-name>`（kebab-case）
2. 读取 `local/<agent-name>/` 下现有 `manifest.json`、`persona/`、已有 `skills/` / `tools/` / `rails/`
3. 向用户确认要改什么（展示字段、persona 正文、增删 skill/tool/rail 等）
4. 包不存在 → 提示在智能体中心确认「我的专家」，或改走 `create`

唯一标识、展示字段和 manifest 细则见「关键规则」与 `references/manifest-spec.md`。用户要求改名 → 告知需换新名字走 `create`，不支持原地改名。

### 第二步：初始化包目录

**仅** `mode=create` **执行。**`mode=update` **跳过本步**，直接进入第三步。

- `<agent-name>`: kebab-case 包 id（等于目录名和 manifest 顶层 `name`）

```bash
python3 <skill_dir>/scripts/init_template.py <agent-name>
```

脚本自行解析落盘路径并在 stdout 输出；生成的模板文件带 `[TODO]` 占位符，实际内容后续填充。勿手建目录或改落盘路径。

### 第三步：填充/更新内容

读取并遵守 `@references/fill-package.md`。

`create` 完整填充；`update` 只做用户确认范围内的局部修改。涉及 skill/tool/rail/persona 增删时，必须同步回写 `manifest.json`；`update` 新增 Tool 时，还必须同步回写 persona 的「工作流程」，写清调用时机与调用顺序。

### 第四步：校验

`create` 与 `update` 均必须执行：

```bash
python3 <skill_dir>/scripts/validate_template.py <agent-name>
```

脚本依次跑 L0 规范质量、L1 静态结构、L2 真实热加载。必须用 JiuwenSwarm 运行环境的 python 执行，跑到 `RESULT: PASS` 才算通过；L2 失败或未执行均不能 register。输出中的 **warning（`!`）不阻塞**——有 `RESULT: PASS` 即可 register，不必为 warning 重跑 validate。

### 第五步：注册

`create` 与 `update` 均必须执行（register 对 marketplace 做 upsert；已安装专家会保留 `installed=true`）。命令按 mode 分派：

```bash
# create
python3 <skill_dir>/scripts/register_template.py <agent-name>

# update
python3 <skill_dir>/scripts/register_template.py <agent-name> --bump
```

注册成功后才算全流程完成。告知用户：参考「三、输出规范」。

---



## 三、输出规范

注册成功后，用下面结构告知用户：

1. **模板概览**：模板包名、分类、核心能力（或本次变更摘要）、标签
2. **产物位置**：包路径（`create` 用 init 脚本输出；`update` 用已定位的 `local/<agent-name>/`）
3. **推荐提问**：给出 2–3 条贴合该专家能力的示例问句
4. **如何安装**：
  - `create`：打开智能体中心 → 找到该专家 → 安装 → 新开对话并装备后使用
  - `update`：若原本已安装，保持可对话，建议按本次改动重试；若未安装，同 `create` 先安装

---



## 四、关键规则

1. **update 只做局部修改**：禁止 init、禁止重写整包；只修改 `local/`，`built_in/` 只读。
2. **不改唯一标识**：包目录名、manifest 顶层 `name`、persona 文件名、`source`。
3. **禁止自动生成 MCP**：不得创建 MCP 配置、连接器或在 manifest 声明 `mcps`；只生成 Skill、Tool、Rail 与 persona。
4. **manifest 以 spec 为准**：组件声明、禁用字段、展示字段规则见 `references/manifest-spec.md`；不要复制本 skill 的 `references/`。
5. **顺序不可跳**：必须 `validate_template.py` 跑到 `RESULT: PASS` 后再 `register_template.py`；`create/update` 都一样。

---



## References

- `references/fill-package.md` — 填充/更新包内容规范
- `references/manifest-spec.md` —  manifest文件结构模板
- `references/persona-spec.md` — persona角色模板
- `references/skill-spec.md` — 创建skill模板
- `references/tool-spec.md` — Tool结构模板
- `references/rail-spec.md` — Rail结构模板
- `references/code-quality.md` — tool/rail/scripts Python 代码质量
