# TOOL Definition Usage

Translate a toolDefinition into two things in the new skill:

1. **Call syntax** — an `invoke(functionName:"<toolName>", arguments:{...})` call.
2. **Tool-definitions block** — a `### Function: <toolName>` entry in the skill body's single **tool definitions** section.

Both are generated from the same source JSON below.

## Source & metadata

Read the toolDefinition from `<workspace>/resources/available-tools/<bundleName>__<toolName>.json`.

Declare every tool the skill actually calls in `SKILL.md` frontmatter (no empty placeholders; `bundleName` / `toolName` must match the source filename and JSON exactly):

```yaml
metadata:
  tools:
    - bundleName: "bundle_001"
      toolName: "weather_query"
```

## Example definition

This single definition drives both outputs below.

```json
{
  "schemaVersion": "1.3",
  "generatedAt": "2026-04-28T10:00:00Z",
  "bundleName": "bundle_001",
  "toolName": "weather_query",
  "toolType": "XiaoYiPlugin",
  "pluginType": "Cloud",
  "description": "查询指定城市的实时天气信息",
  "arguments": {
    "type": "object",
    "properties": {
      "city": {
        "type": "string",
        "description": "城市名称。支持中文（北京、上海）或拼音（beijing）或英文（Beijing）。"
      }
    },
    "required": ["city"]
  },
  "protocol": "REST"
}
```

## Output 1 — Call syntax

`functionName` is the toolDefinition `toolName`. `arguments` carries the tool arguments, plus `bundleName`:

- `arguments.bundleName` is always required and comes from `toolDefinition.bundleName`.
- Build the remaining arguments from the `arguments` JSON Schema; include every field in `arguments.required`.

From the example:

```text
invoke(functionName:"weather_query", arguments:{bundleName:"bundle_001", city:"北京"})
```

## Output 2 — Tool-definitions block

Mapping:

| JSON field | Markdown field |
|------------|----------------|
| `toolName` | `### Function: <toolName>` heading + `- **toolName**` |
| `description` | `- **description**` |
| `arguments` | `- **参数**: <arguments JSON 压缩平铺成一行>` |
| (n/a) | `- **约束**`：仅在工具有顺序、幂等性或前置条件时写 |
| (n/a) | `- **语义**`：仅在触发措辞会路由到不同工具时写（如"删除" vs "取消"） |

- `- **参数**` 直接把 `arguments` JSON 体压缩平铺成一行，不要改写成自定义参数说明格式。`arguments` 为空对象时写 `{}`。

From the example:

```markdown
### Function: weather_query
- **toolName**: weather_query
- **description**: 查询指定城市的实时天气信息
- **参数**: {"type":"object","properties":{"city":{"type":"string","description":"城市名称。支持中文（北京、上海）或拼音（beijing）或英文（Beijing）。"}},"required":["city"]}
```

No 约束 / 语义 line here — the definition implies no special preconditions or trigger-phrase routing.

## Field & safety reference

| Field | How to use it | Hard rule |
| --- | --- | --- |
| `bundleName` | Into metadata, source filename, and `invoke.arguments.bundleName`. | Copy exactly; never invent. Always present in `arguments`. |
| `toolName` | Into `invoke.functionName` and the block heading. | Copy exactly. Never write `toolName(...)` as a direct call. |
| `description` | Decide when the tool should be called. | — |
| `arguments` | JSON Schema for building `invoke.arguments` and the `- **参数**` line. | Include all `required` fields; preserve schema value types; pass as structured data, never command text. |
| `pluginType` | Backend category. | — |
| `deviceCommand` | Backend command template. | Never call directly; always go through `invoke`. |
| `schemaVersion`, `generatedAt`, `toolType`, `protocol` | Internal/transport metadata. | Do not include in generated usage instructions. |

Ask the user when a required value is missing and cannot be safely inferred.