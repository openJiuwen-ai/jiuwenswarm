---
name: skill-creator
description: Unified entry point for all Skill creators. Routes skill creation and editing requests to the correct specialized creator. Use whenever the user wants to create a new Skill, edit an existing Skill from chat, or when the conversation scene is create_skill / edit_skill. Chooses among skill-creator-normal, swarmskill-creator, and skill-omni-creation, then ensures the final Skill package is delivered via send_file_to_user (never silently installed to workspace). Do not bypass this entry by calling specialized creators directly for create_skill / edit_skill scenes.
description_cn: 所有 Skill Creator 的统一入口。按场景与类型路由到 skill-creator-normal、swarmskill-creator 或 skill-omni-creation；创建/修改 Skill 时优先选用本 Skill，不要绕过本入口直接调用各专项 creator。
---

# Skill Creator（统一入口 / 路由）

你是**所有 Skill Creator 的统一入口**，同时负责路由：

- 用户创建或修改 Skill 时，应先进入本 Skill（`skill-creator`），再由你选择唯一下游 creator。
- 下游专项 creator 为：`skill-creator-normal`（单体）、`swarmskill-creator`（团队/Swarm）、`skill-omni-creation`（URL/网页/视频）。
- 在 `create_skill` / `edit_skill` 场景下，不要跳过本入口、直接按用户描述去调用某个专项 creator。

先判定场景（创建 vs 编辑），再选择唯一目标 creator，并按该 creator 的流程完成工作。

## 场景上下文

用户消息上下文可能包含：

| 字段 | 含义 |
|---|---|
| `scene=create_skill` | 聊天创建新 Skill |
| `scene=edit_skill` | 修改已有 Skill |
| `target_skill` | 待修改的 Skill 名称（`edit_skill` 时必有） |
| `target_skill_type` | 待修改 Skill 的类型：`skill` / `swarm_skill` / `multimodal_skill`（`edit_skill` 时优先使用） |
| `skills_to_use` | 通常固定包含本入口 Skill 名（`skill-creator`） |

创建与编辑的路由规则**不同**，必须先看 `scene`，不要混用。

## 创建场景（`scene=create_skill`）

按用户意图与输入形态只选一个目标：

| 目标 Skill creator | 何时选择 |
|---|---|
| `skill-creator-normal` | 普通文本描述；普通文档转**单体** Skill；无多角色/工作流编排诉求；非 URL/网页/视频主导 |
| `swarmskill-creator` | 明确多角色、团队协作、SwarmFlow、工作流编排 |
| `skill-omni-creation` | 以 **URL / 网页 / 视频** 为主生成新 Skill（与 omni 自身能力一致：链接→爬取/抽帧→写 Skill） |

说明：不要因为「纯图片 / 纯音频」附件就把创建路由到 `skill-omni-creation`；omni 流水线面向链接、网页与视频，不覆盖纯图/纯音频主导的新建。

## 歧义与无法判断时（创建与编辑均适用）

**不要**按固定优先级擅自选定 creator。出现以下情况时，先向用户说明可选 creator 及其适用场景，并询问用户选择哪一个，再继续：

- 用户意图同时符合多个 creator（例如既有链接又有多角色编排诉求）
- 输入形态或描述不足以唯一判定应走哪个 creator
- `edit_skill` 下缺少可靠的 `target_skill_type`，且无法从 `SKILL.md` frontmatter `kind` 明确判断类型
- 你对路由结果没有把握

询问时简要列出候选（如：单体 → `skill-creator-normal`；团队/Swarm → `swarmskill-creator`；URL/网页/视频 → `skill-omni-creation`），等用户明确后再路由；未得到答复前不要调用任何下游 creator。

## 编辑场景（`scene=edit_skill`）

与创建严格区分：**不以用户自然语言描述作为主路由依据**。

1. 读取 `target_skill`；若有 `target_skill_type` 则优先采用，否则根据 workspace 中该 Skill 判断类型（`SKILL.md` frontmatter `kind: swarm-skill` → `swarm_skill`；含多媒体资产 → `multimodal_skill`；否则 → `skill`）。
2. 先阅读 `target_skill` 对应 workspace 中的 `SKILL.md` 与相关文件，再进入对应 creator 做**增量修改**。
3. 按类型映射（默认锁定，不因用户随口描述改道）：

| `skill_type` / `target_skill_type` | 路由到 |
|---|---|
| `skill`（或空） | `skill-creator-normal` |
| `swarm_skill` | `swarmskill-creator` |
| `multimodal_skill` | `skill-omni-creation` |

4. **例外**：仅当用户**明确要求类型转换**（例如把单体 Skill 升级为团队 Skill）时，才可改走 `swarmskill-creator`；否则必须按上表路由。

## 选定后

选定后立即调用对应 Skill（`skill_tool` / 阅读其 `SKILL.md`），不要并行跑多个 creator。

## 强制交付规则（创建与修改均适用）

1. **不要**把成品直接安装进用户长期 workspace 技能目录作为最终步骤。
2. 在临时目录或工作草稿目录生成完整 Skill 目录（含合法 `SKILL.md`，YAML 含非空 `name` 与 `description`）。
3. 将目录打成 **`.zip` 或 `.skill`（zip 格式）** 包。包内**不得**包含根级 `.archive/`。
4. 包内 YAML 的 `version` **不要**当作产品版本写入；创建/修改输出包不应携带产品 `version` 身份。
5. 调用工具 `send_file_to_user`，传入成品包的**绝对路径**，把文件卡片发给用户。
6. 告知用户：在 Web 文件卡片上点击「保存」后才会安装到 workspace；你不会自动安装。

## 修改现有 Skill 的额外约束

- 不产生新的产品版本，也不更改 `current_version`。
- 覆盖语义由用户保存（`skills.import_local`）完成；你只产出可保存的包。

## 输出话术

完成后简要说明：经统一入口路由到了哪个 creator、包文件名，并提示用户点击保存。
