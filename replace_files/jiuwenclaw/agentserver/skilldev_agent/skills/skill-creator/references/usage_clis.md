# CLI Definition Usage

Translate a cliDefinition into two things in the new skill:

1. **Call syntax** — an `exec(command: "...")` instruction wrapping the CLI command, with inputs appended as flags inside the command string.
2. **Tool-definitions block** — a `### CLI: <name>` entry in the skill body's single **tool definitions** section.

Both are generated from the same source JSON below.

## Source & metadata

Read CLI definitions from `<workspace>/resources/clis/available_clis.json`. Only `name`, `description`, and `inputSchema` drive the translation.

Declare every CLI the skill actually calls in `SKILL.md` frontmatter (no empty placeholders):

```yaml
metadata:
  clis:
    - name: "ohos-bm install"
```

## Example definition

This single definition drives both outputs below.

```json
{
  "name": "ohos-bm install",
  "description": "安装应用包",
  "inputSchema": {
    "properties": {
      "module": { "type": "string" },
      "force":  { "type": "boolean", "default": false }
    },
    "required": ["module"]
  }
}
```

## Output 1 — Call syntax

Wrap the full CLI command in `exec(command: "...")`. The cliDefinition `name` is the fixed command prefix inside that string; append inputs as flags only when the CLI has input parameters:

- String / number / array params → `--<paramName> <placeholder>`.
- Boolean params → include `--<paramName>` only when the workflow needs the true value.
- Never use single-dash flags (`-a`, `-m`); all flags must use double-dash prefix (`--a`, `--m` or the full `--<paramName>` form).   
- Include every field in `inputSchema.required`. If `inputSchema` (or its `properties`) is missing or empty, the command is just the prefix.
- Include optional fields only when the workflow needs them or the user requested them. Preserve parameter names exactly as defined in `inputSchema.properties`.

From the example:

```text
exec(command: "ohos-bm install --module entry")
```

Add `--force` only when overwriting an existing install:

```text
exec(command: "ohos-bm install --module entry --force")
```

## Output 2 — Tool-definitions block

Mapping:

| JSON field | Markdown field |
|------------|----------------|
| `name` | `### CLI: <name>` 标题 + `- **toolName**: <name>`（用完整 `name`，如 `ohos-bm install`） |
| `description` | `- **description**` |
| `inputSchema` | 调用表（见下，替代 `- **参数**`） |
| (n/a) | `- **约束**`：子命令不可逆时（force-stop / delete / uninstall）**必填**，并写明需先确认哪步 |

调用表格式 —— 一行一条有意义的参数组合，命令统一包成 `exec(command: "...")`：调用列用 `<placeholder>` 模板，样例列用完整替换后的命令；CLI 无入参时命令里只有前缀：

```markdown
| 调用 | 说明 | 样例 |
|------|------|------|
| exec(command: "<command-name> --<param> <placeholder>") | <one-line purpose> | exec(command: "<concrete example>") |
```

From the example:

```markdown
### CLI: ohos-bm install
- **toolName**: ohos-bm install
- **description**: 安装应用包

| 调用 | 说明 | 样例 |
|------|------|------|
| `exec(command: "ohos-bm install --module <module>")` | 安装指定模块 | `exec(command: "ohos-bm install --module entry")` |
| `exec(command: "ohos-bm install --module <module> --force")` | 强制覆盖已存在的安装 | `exec(command: "ohos-bm install --module entry --force")` |

- **约束**: `--force` 会覆盖已存在的安装，**必须**先确认用户意图
```

## Field & safety reference

| Field | How to use it | Hard rule |
| --- | --- | --- |
| `name` | Fixed command prefix inside `exec(command: "...")` + block heading / `- **toolName**`. | Never replace the prefix with user input. |
| `description` | Decide when the CLI should be called. | — |
| `inputSchema` | Build flags and the 调用表. | Include all `required` inputs; preserve param names exactly; flags must use `--` double-dash prefix (e.g. `--a`, `--module`) — single-dash flags (`-x`) are forbidden. |

- The assembled command is always the `command` string of `exec(command: "...")`; never split it into separate structured fields.
- User values belong only in flag values — never in the command prefix or flag names.
- Quote concrete string values that contain spaces.
- Reject string values with newlines, backticks, `$(`, `;`, or `|` unless the CLI explicitly needs them.
- Any irreversible subcommand (force-stop / delete / uninstall) **requires** a `- **约束**` line stating what to confirm first.
- Ask the user when a required input is missing and cannot be safely inferred.