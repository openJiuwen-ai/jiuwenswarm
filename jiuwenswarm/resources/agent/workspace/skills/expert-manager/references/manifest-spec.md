# manifest.json 规范

jiuwenswarm 专家包以顶层 `manifest.json` 为入口，按是否含 `package_type` 键分两型。

权威源：`jiuwenswarm/server/runtime/expert/expert_store.py:validate_expert_package`、`agent_group.py:load_agent_group_package`、agent-core `harness/resources/extension_loader.py:load_agent_template_package`。

---

## 一、单专家（agent_template）

### 目录布局

```
<expert_id>/
├── manifest.json          # packageType: "agent_template"
├── agents/
│   └── <expert_id>.md     # identity 正文（注入 identity section, priority 10）
├── skills/<name>/SKILL.md # 可选，叶子形态（dir 直接含 SKILL.md）
└── avatars/<expert_id>.png  # 可选，metadata.avatar 引用
```

> **专家团目录布局**见下方"二、专家团"章节。团队成员的 persona 目录为 `persona`（非 `agents`）。

### 基础字段（必填）

| 字段 | 类型 | 说明 |
|---|---|---|
| `packageType` | string | 必须为 `"agent_template"` |
| `agentCard` | object | `{id, name, description}`；`id` 必须等于包目录名 |
| `persona` | object | `{dir}`；相对路径，目录内须有 `*.md`（多个按文件名排序拼接进 identity section）。**单专家固定为 `"agents"`**（对应 `agents/<expert_id>.md`） |

### 可选字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `skills` | list[{dir}] | `dir` 必须含 `SKILL.md`（叶子形态），禁止路径逃逸 |
| `tools` | list[{file, class?}] | `file` 必须存在 |
| `mcps` | list | MCP server 引用 |
| `metadata` | object | 自由对象，见下表 |

### metadata 子字段

| 字段 | 运行时 | 说明 |
|---|---|---|
| `avatar` | 读 + 校验文件存在 | 相对路径，单专家头像（如 `"avatars/expert.png"`） |
| `tags` | 透传 `experts.list` | 建议字符串列表，3 个 |
| `quickPrompts` | 产品层（前端展示） | 推荐提示词数组，固定 3 个字符串 |
| `profession` | 产品层 | 职业定位字符串（如 `"后端架构师"`） |
| `categoryId` | 产品层 | 分类 ID 字符串（如 `"02-Engineering"`） |

### 禁止字段

`rails`、`subagents`（本期不支持）。

### model 字段

根模板 `model` **不生效**（不被使用），校验会警告，建议移除。

### 完整模板：Agent 型

```json
{
  "packageType": "agent_template",
  "agentCard": {
    "id": "{kebab-case-id-等于目录名}",
    "name": "{展示名}",
    "description": "{一句话描述，突出核心能力}"
  },
  "persona": { "dir": "agents" },
  "skills": [{ "dir": "skills/{skill-name}", "mode": "all" }],
  "metadata": {
    "avatar": "avatars/expert.png",
    "tags": ["{领域 1}", "{领域 2}", "{领域 3}"],
    "quickPrompts": [
      "{推荐提示词 1}",
      "{推荐提示词 2}",
      "{推荐提示词 3}"
    ],
    "profession": "{职业定位}",
    "categoryId": "{分类 ID}"
  }
}
}
```

> 没有 skills/tools 时省略对应字段。

---

## 二、专家团（agent_group）

### 目录布局

```
<expert_id>/
├── manifest.json              # package_type: "agent_group"（snake_case）
├── agents/
│   ├── leader/                # 固定主理人
│   │   ├── manifest.json      # packageType: "agent_template"（camelCase）
│   │   ├── AGENT.md           # 团队级铁律（leader 必有，member 禁有）
│   │   └── persona/
│   │       └── leader-persona.md   # leader identity
│   └── <member>/
│       ├── manifest.json
│       └── persona/
│           └── <member>.md    # 成员 identity + 职责（禁 AGENT.md）
├── skills/<name>/SKILL.md     # 可选，顶层共享技能（注入所有成员）
└── avatars/<id>.png           # 可选，文件名 = 成员 id（leader.png / <member>.png）
```

### 顶层 manifest（snake_case）

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `package_type` | Y | string | 必须为 `"agent_group"` |
| `name` | Y | string | 必须等于包目录名 |
| `agents` | Y | list[string] | 非空无重复，**必须含 `"leader"`** |
| `instruction` | N | string | 注入 `agent_group_instruction` section（priority 20），全员共享 |
| `skills` | N | list[string] | 共享技能**名**列表（非路径），对应 `skills/<name>/SKILL.md` |
| `metadata` | N | object | 见下方 metadata 子字段表 |

### 专家团 metadata 子字段

| 字段 | 运行时 | 说明 |
|---|---|---|
| `displayName` | 透传 `experts.list` | 团队展示名（单字符串） |
| `description` | 透传 `experts.list` | 团队描述（单字符串） |
| `avatar` | 读 + 校验文件存在 | 相对路径，团队头像（如 `"avatars/expert.png"`） |
| `tags` | 透传 `experts.list` | 建议字符串列表，3 个 |
| `quickPrompts` | 产品层 | 推荐提示词数组，固定 3 个字符串 |
| `profession` | 产品层 | 职业定位字符串（如 `"专家团"`） |
| `categoryId` | 产品层 | 分类 ID 字符串（如 `"04-DataAI"`） |

### 成员子包 manifest（camelCase，core schema）

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `packageType` | Y | string | `"agent_template"` |
| `agentCard` | N | object | `{id, name, description}`；`id` 须等于成员目录名（也接受平铺 `name`/`description`，id 由目录名派生） |
| `persona` | Y | object | `{dir}`；相对成员目录，须有 `*.md`。**成员固定为 `"persona"`**（对应 `persona/<member_id>.md`） |
| `tools` / `mcps` / `skills` | N | — | 同单专家 |

### 成员级禁止字段

`rails`、`subagents`。

### AGENT.md 规则

仅 leader 可有 `AGENT.md`（团队级铁律，注入 `agent_group_leader_rules` section, priority 15）；非 leader 成员**禁止** `AGENT.md`（职责写进 persona）。

### 完整模板：Team 型（顶层）

```json
{
  "package_type": "agent_group",
  "name": "{kebab-case-id-等于目录名}",
  "agents": ["leader", "{member-a}", "{member-b}"],
  "instruction": "{全员共享的协作契约，字符串}",
  "skills": ["{shared-skill-name}"],
  "metadata": {
    "displayName": "{团队展示名}",
    "description": "{团队描述}",
    "tags": ["{领域1}", "{领域2}", "{领域3}"]
  }
}
```

### 完整模板：成员子包（leader / member 通用）

```json
{
  "packageType": "agent_template",
  "agentCard": {
    "id": "{member-id-等于成员目录名}",
    "name": "{成员展示名}",
    "description": "{成员一句话描述}"
  },
  "persona": { "dir": "persona" }
}
```

> leader 同级再加 `AGENT.md`；member 不加。

---

## 三、可改 / 严禁改字段清单

### 可以修改

`agentCard.name`、`agentCard.description`、persona 正文、`metadata.tags`、`metadata.avatar`（单专家）、`metadata.displayName`/`description`（专家团）、团队成员 persona、leader `AGENT.md` 内容、顶层 `instruction`。

### 严禁修改（专家唯一标识，改了会丢专家）

- `agentCard.id`（单专家）/ 顶层 `name`（专家团）—— 须等于目录名
- 专家目录名、成员目录名、leader 固定名
- 成员 manifest 的 `packageType`
- `package_type` / `packageType` 的取值

如须改 id/目录名，应告知用户：改名需要重新创建专家，不支持原地改名。

---

## 四、加载与可发现性

- 包目录须位于专家根：`~/.jiuwenswarm/agent/workspace/experts/<expert_id>/`（`get_agent_experts_dir()`；可用 `JIUWEN_EXPERTS_DIR` 覆盖路径）
- 本地源由 `LocalDirExpertPackageSource` 扫描，需 `JIUWEN_EXPERT_LOCAL_DIRS=1` 启用
- `expert.load` → `fetch` → `validate_expert_package` → `load_agent_template_package`（单专家）/ `load_agent_group_package`（专家团）
- 任一校验失败整体终止（失败全终止原则）

---

## 五、路径安全铁律

所有 manifest 路径字段（`persona.dir`、`tools[].file`、`skills[].dir`、`metadata.avatar`）必须是**包内相对路径**：

- 禁止绝对路径
- 禁止 `..` 逃逸（zip slip 同规）
- 解析后须落在包根或成员子包目录内
