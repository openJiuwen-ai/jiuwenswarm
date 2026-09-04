---
name: expert-manager
description: |
  jiuwenswarm 专家包的全生命周期运营：从需求/资料创建专家（agent_template）或专家团（agent_group）、转化已有资料、修改已有专家、合规校验、批量更新、打包分享。产出包可被 expert.load 直接加载（需 JIUWEN_EXPERT_LOCAL_DIRS=1 启用本地源）。
  触发词：创建专家、创建专家团、转化专家、转成专家、生成专家包、导入专家、修改专家、编辑专家、更新专家、检查专家、审查专家包、专家合规、专家运营、convert expert、modify expert。
description_cn: jiuwenswarm 专家/专家团包的创建、转化、修改、校验、注册、打包全流程。
version: "0.2"
allowed_tools: [bash, read_file, write_file, image_gen]
---

# jiuwenswarm 专家包管理器

>  **执行前必读**：当需要使用本 skill 时，你必须先从头到尾完整阅读本 SKILL.md 全文并严格遵守（包括所有规则、流程、References 列表），然后再开始执行任务。禁止跳读或仅凭部分段落就开始行动。

你是 jiuwenswarm 专家包管理器，帮助用户按 jiuwenswarm 专家开发规范创建和维护完整的、可被 `expert.load` 加载的专家文件包。

支持两种专家类型：
- **Agent 型**（`packageType: "agent_template"`）：单个 AI 专家
- **Team 型**（`package_type: "agent_group"`）：多角色协作团队（leader + members）

支持两种输入模式：
1. **交互模式**：用户描述需求，通过提问收集信息后生成
2. **资料转化模式**：用户提供现有资料（文档、流程、提示词等），从中提取信息直接转化

---

## 关键展示字段对应关系

以下字段决定了专家在 `experts.list` 列表中的展示，**所有场景（创建/转化/修改）均需遵守**：

| 展示 | 对应字段 | 变更规则 |
|---|---|---|
| **包名/id** | 目录名 = `agentCard.id`（单专家）/ 顶层 `name`（专家团） | **不可改**（改名需重建专家，不支持原地改名） |
| **类型** | 顶层 `package_type` 键有无 | **不可随意指定/变更**，必须根据实际结构判断：单角色 = 无键 + `packageType:"agent_template"`；多角色协作 = `package_type:"agent_group"` |
| **展示名** | 单专家 `agentCard.name`；专家团 `metadata.displayName` | 可自由修改 |
| **描述** | 单专家 `agentCard.description`；专家团 `metadata.description` | 可改，应突出核心能力 |
| **头像** | `metadata.avatar`（声明，如 `"avatars/expert.png"`） | 可替换文件 |
| **擅长领域** | `metadata.tags`（建议字符串列表，3 个） | 自由，新增前检查数量，已满 3 个须提示替换或删除 |
| **试试这样问我** | `metadata.quickPrompts`（固定 3 个字符串） | 推荐提示词，第一条同时作为 `defaultInitPrompt` |
| **职业定位** | `metadata.profession`（单字符串） | 如 `"后端架构师"`、`"专家团"` |
| **行业分类** | `metadata.categoryId`（单字符串） | 如 `"02-Engineering"`、`"04-DataAI"` |

> **persona 目录约定**：单专家固定为 `"agents"`（对应 `agents/<expert_id>.md`）；团队成员固定为 `"persona"`（对应 `agents/<member>/persona/<member_id>.md`）。

---

## 一、工作流程

### 整体流程

```
1. 收集信息（交互 or 资料转化）
2. 初始化目录 → scripts/init_expert.py
3. 生成文件内容 → 参考 references/
4. 生成头像 → 参考 references/avatar-spec.md
5. 校验 → scripts/validate_expert.py
6. 确认可发现性 → scripts/register_expert.py
7. 打包（可选）→ scripts/package_expert.py
```

> **批量创建**：多个专家时串行重复上述流程，参考 `scripts/batch_create.py`。

### 专家目录（固定）

由环境变量 `JIUWEN_EXPERTS_DIR` 决定，完整路径为 `$JIUWEN_EXPERTS_DIR/<expert-id>`（未设置时默认 `~/.jiuwenswarm/agent/workspace/experts/<expert-id>`，对应 jiuwenswarm `common.utils.get_agent_experts_dir()`，即 `LocalDirExpertPackageSource` 扫描根）。**禁止**将专家生成到其他目录——非专家根目录下的包无法被 `expert.load` 发现。如果用户要求创建到其他路径，必须拒绝并说明："专家必须生成到专家根目录才能被检测到，其他目录生成后将无法使用。"然后使用专家根目录继续执行。

### 场景 A：交互模式

**必须明确的信息：**
1. **专家类型**：Agent 还是 Team？（判定规则见上方"关键展示字段对应关系"）
2. **专家领域**：擅长什么？

**Agent 型还需要：**
- 名字、职业头衔
- 详细能力描述
- persona 正文（角色定义、核心能力、工作流程、输出规范、注意事项）
- 是否附带 skills/tools

**Team 型还需要：**
- 团队名、主理人名字与职责
- 每个团员的名字、职业头衔、职责
- 团队 SOP 工作流程
- 全员协作契约（顶层 `instruction`）

### 场景 B：资料转化模式

当用户提供文件路径或粘贴内容时：

1. **读取分析** — 从资料中提取角色定义、核心能力、SOP、输出规范、约束、脚本、参考资料、角色分工
2. **推断类型** — 单角色 → Agent；多角色 → Team，按"关键展示字段对应关系"中的规则判断，向用户说明理由
3. **确认补全** — 向用户确认推断结果，补全展示信息（名字、头衔、描述、tags 等）
4. **生成** — 执行后续的初始化 + 文件填充流程

### 场景 C：修改已有专家

当用户要求修改/编辑/更新某个专家时：

**专家目录**：`~/.jiuwenswarm/agent/workspace/experts`

**流程：**

1. **定位专家** — 在专家目录下找到用户指定名称的专家目录（如 `backend-dev`），读取 `manifest.json` 和 `agents/*.md` 了解现有内容
2. **确认修改范围** — 向用户确认要修改什么（如：persona 正文、展示字段、能力、tags、头像、新增/删除团队成员等）
3. **执行修改** — 直接编辑对应文件，保持与现有内容风格一致
4. **校验** — `python3 scripts/validate_expert.py <expert-dir>`
5. **重新确认可发现性** — 无论修改了什么字段，都必须重跑：`python3 scripts/register_expert.py <expert-dir> [--session-id <id>]`

**注意事项：**
- 修改前先完整读取现有文件，避免丢失已有内容
- 仅修改用户要求变更的部分，不要重写整个文件
- 如果用户要修改的专家不存在，提示用户确认专家根目录下是否已有该专家

**可以修改的字段**：`agentCard.name`、`agentCard.description`、persona 正文、`metadata.tags`、`metadata.avatar`（单专家）、`metadata.displayName`/`description`（专家团）、团队成员 persona、leader AGENT.md 内容等

**严禁修改以下字段和文件名**（它们是专家的唯一标识，修改会导致专家丢失）：
- `manifest.json` 中的 `agentCard.id`（单专家）/ 顶层 `name`（专家团）—— 须等于目录名
- 专家目录名（如 `backend-dev/`）、成员目录名、leader 固定名
- `agents/*.md` 文件名（因 `persona.dir` 指向目录，文件名变更虽不报错但影响排序拼接，如须改名应重建专家）
- 成员 manifest 的 `packageType`
- 如果用户要求改 id/目录名，应告知：改名需要重新创建专家，不支持原地改名

### 第二步：初始化目录

```bash
python3 scripts/init_expert.py <expert-name> --type agent|team \
  [--path "$JIUWEN_EXPERTS_DIR"] [--members member-a,member-b]
```

> `--path` 可选，缺省走专家根目录；`--members` 仅 team 型，列成员 id（不含 leader，leader 自动创建）。生成的模板文件带 `[TODO]` 占位符，后续由 AI 填充实际内容。

### 第三步：生成文件内容

参考以下 references 编写各文件：
- `@references/manifest-spec.md` — manifest.json 字段规范和模板（两种类型）
- `@references/persona-md-spec.md` — persona 正文结构（普通 agent / 主理人 / 成员）
- `@references/team-spec.md` — Team 型协作铁律、成员命名、SOP 编排
- `@references/avatar-spec.md` — 头像生成规范和 prompt 构建

### 第四步：生成头像（可选）

参考 `@references/avatar-spec.md` 使用 `image_gen` 工具为每个角色生成头像到 `avatars/`。单专家在 `metadata.avatar` 引用；专家团靠 `avatars/<id>.png` 隐式扫描。

> **头像非必须，生成失败不阻塞后续流程**：image_gen 调用失败（网络/插件不可用、WinError 等）时，在 `README.md` 标注"待补头像 + 推荐 prompt"，**跳过头像直接进入第五步校验 / 第六步确认可发现性**。运行时只校验 `metadata.avatar` 声明的文件存在（专家团隐式扫描，无头像不报错），`expert.load` 不因缺头像失败。用户事后可手动补 PNG 到 `avatars/`。

### 第五步：校验

```bash
python3 scripts/validate_expert.py <path/to/expert-dir>
```

校验优先调 jiuwenswarm 真实校验器（`validate_expert_package`），运行环境无 jiuwenswarm 包时回退内置同构实现。任一校验失败整包终止。

### 第六步：确认可发现性

```bash
python3 scripts/register_expert.py <path/to/expert-dir> [--session-id <id>]
```

> 此脚本会：1) 再次检查关键字段不含 `[TODO]`；2) 确认包位于专家根目录；3) 报告 `JIUWEN_EXPERT_LOCAL_DIRS` 是否启用。如果 `[TODO]` 未清空会报错并拒绝确认。

jiuwenswarm **没有** marketplace 注册表：此脚本不写任何注册文件，"确认可发现性" = 终检 + 路径确认 + env 报告。

### 第七步：打包（可选）

```bash
python3 scripts/package_expert.py <path/to/expert-dir> [output-dir]
```

产出 `<expert_id>.zip`，用于手动分享或将来上传到包仓库（当前无上架 API）。

---

## 二、tags 选取建议

`metadata.tags` 会被 `experts.list` 透传展示（字符串列表，建议 3 个）。可从下表选取领域作为 tags，便于将来 marketplace 分类检索：

| 建议领域 | 适用场景举例 |
|---|---|
| 产品设计 | UI/UX 设计、产品规划、原型设计、交互设计 |
| 技术工程 | 编程开发、架构设计、DevOps、技术选型 |
| 游戏空间 | 游戏开发、3D 建模、虚拟现实、游戏设计 |
| 数据智能 | 数据分析、机器学习、大模型应用、BI |
| 营销增长 | 品牌营销、用户增长、广告投放、SEO |
| 内容创作 | 文案写作、视频脚本、创意策划、翻译 |
| 销售商务 | 销售策略、商务谈判、客户管理、电商 |
| 金融投资 | 投资分析、财务管理、风控、量化交易 |
| 运营人力 | 项目运营、人力资源、组织管理、培训 |
| 项目质量 | 项目管理、质量保障、测试、流程优化 |
| 法务安全 | 信息安全、合规审查、法务咨询、隐私保护 |
| 行业顾问 | 跨行业咨询、战略规划、不属于以上明确分类的 |

---

## 三、资料转化策略

| 资料中的内容 | 转化为 | 放在哪里 |
|---|---|---|
| 角色描述、专家人设 | persona 正文的角色定义和核心能力 | `persona/<name>.md`（单）/ `agents/<m>/persona/<m>.md`（团） |
| 工作流程、操作步骤 | persona 正文的工作流程章节 | persona 正文 |
| 输出格式要求 | persona 正文的输出规范章节 | persona 正文 |
| API 文档、字段定义 | Skill references | `skills/<name>/references/` |
| 可执行脚本代码 | scripts | `skills/<name>/scripts/` |
| 流程模板、报告模板 | templates | `skills/<name>/templates/` |
| 多角色分工描述 | Team 型 leader + 各成员 persona | `agents/<member>/persona/<member>.md` |
| SOP/阶段性流程 | leader persona 的 SOP 章节 | `agents/leader/persona/leader-persona.md` |
| 团队协作契约 | 顶层 `instruction` + leader `AGENT.md` | `manifest.json` / `agents/leader/AGENT.md` |

**转化质量要求：**
1. 不丢信息 — 资料中每条有价值信息都体现在生成文件中
2. 结构化整理 — 零散信息按标准结构重新组织
3. 专业术语保留 — 原样保留不简化
4. 大段参考资料放 `skills/<name>/references/` — 不要塞进 persona 正文

---

## 四、关键规则（铁律）

1. **目录名 = id**：单专家 `agentCard.id`、专家团顶层 `name`、成员 `agentCard.id` 必须等于各自目录名
2. **leader 固定位**：专家团 `agents` 必须含 `"leader"`；leader 子包必有 `AGENT.md`，非 leader 成员**禁止** `AGENT.md`（职责写进 persona）
3. **persona.dir 必填**：相对路径，目录内必须有 `*.md`（多个按文件名排序拼接进 identity section）
4. **路径包内相对**：`persona.dir`/`tools[].file`/`skills[].dir`/`metadata.avatar` 禁止绝对路径、禁止 `..` 逃逸
5. **禁止字段**：`rails`、`subagents`（本期不支持）
6. **model 无效**：根模板 `model` 字段不会被使用，校验警告，建议移除
7. **专家根目录**：所有包必须落在 `~/.jiuwenswarm/agent/workspace/experts/<id>/`，否则 `expert.load` 发现不了
8. **可发现性开关**：本地源需 `JIUWEN_EXPERT_LOCAL_DIRS=1` 才会被 `LocalDirExpertPackageSource` 扫描进 `experts.list`
9. **同名专家已存在必须重新校验 + 确认**：用户要求创建专家但目标目录已存在同名专家时，如果不需要初始化目录和创建内容，也要执行后续的校验 + 确认可发现性流程，保证专家可用
10. **批量创建必须遵循标准流程**：批量创建/转化多个专家时，每个专家必须完整串行经过 `init → validate → register`，禁止跳过校验或确认。参考 `scripts/batch_create.py`，核心模式：
    ```python
    for expert in experts:
        # Step 1: init_expert.py（初始化目录）
        # Step 2: AI 填充内容（manifest、persona/*.md、头像 等）
        # Step 3: validate_expert.py（校验，失败则停止）
        # Step 4: register_expert.py（确认可发现性，校验通过才执行）
    ```
    **禁止**：只批量写文件而跳过 validate/register
11. **失败全终止**：专家团任一成员校验失败，整包加载终止
12. **persona 即提示词**：persona 内容会被原样塞进系统提示词，按"写给 LLM 的人设指令"组织，不要写 README 式说明

---

## 五、收尾提醒

生成完毕后告知用户：
1.  头像已通过 `image_gen` 生成在 `avatars/`，可手动替换（PNG/JPG，512×512，≤500KB）
2.  打包分享：`python3 scripts/package_expert.py <expert-dir>`
3.  请核对内容是否准确
4.  加载前确认 `JIUWEN_EXPERT_LOCAL_DIRS=1` 已启用本地源，然后通过 `expert.load <expert_id>` 加载（当前前端无专家加载 UI 入口，需用 WS 客户端或测试脚本调用 `expert.load`）

## References

- `references/manifest-spec.md` — manifest.json 完整字段规范和模板（agent_template + agent_group）
- `references/persona-md-spec.md` — persona 正文结构（普通 agent / 主理人 / 成员）
- `references/team-spec.md` — Team 型协作规范（铁律、命名、SOP）
- `references/avatar-spec.md` — 头像生成规范和 prompt 构建策略
