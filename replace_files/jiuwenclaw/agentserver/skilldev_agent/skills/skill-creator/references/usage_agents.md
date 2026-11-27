# AGENT Definition Usage

Translate an agentDefinition into two things in the new skill:

1. **Call syntax** — an `invoke(functionName:"agent_as_a_tool", arguments:{...})` call.
2. **Tool-definitions block** — a `### Function: agent_as_a_tool` entry in the skill body's single **tool definitions** section.

Both are generated from the same source JSON below.

## Source & metadata

Read agent definitions from `<workspace>/resources/agents/available_agents.json`.

Declare every agent the skill actually calls in `SKILL.md` frontmatter (no empty placeholders; `agentId` must match the source definition exactly):

```yaml
metadata:
  agents:
    - agentId: "aaabbbccc"
```

## Example definition

This single definition drives both outputs below.

```json
{
  "agentId": "aaabbbccc",
  "name": "travelAgent",
  "description": "查询出行相关资讯与方案",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "用户查询问题内容"
      },
      "filesInfo": {
        "type": "Array<Object>",
        "description": "附带的文件资料信息"
      }
    },
    "required": ["query"]
  }
}
```

## Output 1 — Call syntax

`functionName` is always the fixed string `agent_as_a_tool` (never the agent `name` or `agentId`). `arguments` carries the call inputs:

- `arguments.agentId` (**always required**) comes from `agentDefinition.agentId`.
- `arguments.query` is the task query to send to the agent — make it self-contained with the user's task, constraints, and expected output.
- Build the remaining arguments from the `parameters` JSON Schema; include every field in `parameters.required`.
- Optional fields, including `filesInfo`, are omitted unless the task needs them. When forwarding files, build `filesInfo` according to `parameters.properties.filesInfo` — do not assume a fixed shape beyond the schema.

From the example:

```text
invoke(functionName:"agent_as_a_tool", arguments:{agentId:"aaabbbccc", query:"查询北京三日游玩攻略"})
```

With files (only when required or task-relevant):

```text
invoke(functionName:"agent_as_a_tool", arguments:{agentId:"aaabbbccc", query:"分析这份行程文件", filesInfo:<value matching parameters.properties.filesInfo>})
```

Example (based on the example definition above):

The parameter description table is a **required** section in the generated SKILL.md. Follow the rules strictly:

1. **`agentId` is a fixed row** — it MUST always be present as the **first row** of the table, regardless of the agent's `parameters` JSON Schema. It is not derived from `parameters.properties`; it is an intrinsic routing parameter.
2. **All other rows** are derived from `parameters.properties`. Fields listed in `parameters.required` are marked as "是" (required); all others are marked as "否" (optional).
3. The table MUST NOT contain any row that is neither `agentId` nor present in `parameters.properties`.
```text
参数说明：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| agentId | string | 是 | 目标 Agent 的 ID，取自 `metadata.agents` 中的注册值，是运行时路由参数 |
| query | string | 是 | 用户查询问题内容 |
| filesInfo | Array\<Object\> | 否 | 附带的文件资料信息 |
```

## Output 2 — Tool-definitions block

Mapping:

| JSON field | Markdown field |
|------------|----------------|
| (固定) | `### Function: agent_as_a_tool` 标题（恒为此字符串，不用 `name`/`agentId`） |
| (固定) | `- **toolName**: agent_as_a_tool`（恒为此字符串，不用 `name`/`agentId`） |
| `description` | `- **description**` |
| `parameters` | `- **参数**: <parameters JSON 压缩平铺成一行>` |
| (n/a) | `- **约束**`：仅在有顺序或前置条件时写 |
| (n/a) | `- **语义**`：仅在触发措辞会路由到不同 agent 时写 |

- `- **参数**` 直接把 `parameters` JSON 体压缩平铺成一行，不要改写成自定义参数说明格式。`parameters` 为空对象时写 `{}`。

From the example:

```markdown
### Function: agent_as_a_tool
- **toolName**: agent_as_a_tool
- **description**: 查询出行相关资讯与方案
- **参数**: {"type":"object","properties":{"query":{"type":"string","description":"用户查询问题内容"},"filesInfo":{"type":"Array<Object>","description":"附带的文件资料信息"}},"required":["query"]}
```

## Field & safety reference

| Field | How to use it | Hard rule |
| --- | --- | --- |
| `agentId` | Into `invoke.arguments.agentId` only; also the fixed first row of the parameter description table. | Copy exactly; never use as the toolName identifier. Always include as the first row in the parameter description table, even though it is not part of `parameters.properties`. |
| `name` | Human-readable agent name only. | Never use as the call identifier or toolName. |
| `description` | Decide when delegation is appropriate. | — |
| `parameters` | JSON Schema for building `query`, `filesInfo`, and the `- **参数**` line. | Include all `required` fields. |
| `parameters.properties.query` | User task content → `invoke.arguments.query`. | Make it self-contained. |
| `parameters.properties.filesInfo` | File metadata → `invoke.arguments.filesInfo`. | Include only when required or task-relevant; follow this schema, don't assume a fixed shape. |

`functionName` is always `agent_as_a_tool` — never replace it with the agent `name` or `agentId`. Ask the user when a required value is missing and cannot be safely inferred.