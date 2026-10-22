# Process CLI: model connections for noninteractive commands

[中文](../zh/Process_CLI_Model.md) · [English](Process_CLI_Model.md)

Run `jiuwenswarm-process "task"` in the Python environment where JiuwenSwarm
is installed. The entrypoint ships with the JiuwenSwarm wheel and owns one
local Runtime lifecycle. No separate SDK installation or running Gateway,
AgentServer, Web, or TUI service is required.

## 1. Options

| Option | Value | Behavior |
| --- | --- | --- |
| `-m`, `--model` | Configured model name, unique alias, or `name#global_index` | Reuses the selected connection. Duplicate names prefer the configured default, then the first entry; ambiguous aliases require an exact index. |
| `--api-base` | HTTP(S) service base URL | Requires both `--model` and `--api-key-env`. Userinfo, query parameters, fragments, and whitespace are rejected. |
| `--api-key-env` | Environment variable **name**, not a key value | Requires `--model`. Reads a nonempty key from the current process environment before dotenv loading. Does not fall back to dotenv for this explicitly named variable. |
| `--model-provider` | `OpenAI` or `Anthropic`, case sensitive | Requires `--api-base`. Defaults to the selected provider for an existing model and `OpenAI` for a new model. |

The global index is zero based, as in the Runtime model catalog. These flags
require a task prompt and cannot be combined with machine protocol entrypoints.
REPL model selection continues to use `/model`.

Noninteractive commands with a task prompt support only normal single-Agent
modes, such as `--mode agent.code.normal` and `--mode agent.work.normal`.
Selecting Plan or Team with `--mode`, including historical aliases, exits with
code `2` before Runtime starts. This also applies to `--session` continuation
and every `--output` format. Interactive mode switching and `/plan` remain
available. Machine JSON/JSONL entrypoints have their own mode and interaction
protocol, reject `--mode`, and are unaffected by this restriction.

Select the environment with a complete `--mode`: `agent.code.normal` for code
and `agent.work.normal` for general work. The separate `--work-mode` CLI option
has been removed.

## 2. Windows CMD examples

Select a configured connection:

```bat
jiuwenswarm-process "Reply MODEL_OK" --model "configured-alias" --mode agent.code.normal --timeout 120
jiuwenswarm-process "Reply MODEL_OK" -m "configured-model#0" --mode agent.work.normal --timeout 120
```

Provide a complete connection, including a model absent from local config:

```bat
set "JIUWEN_MODEL_KEY=replace-with-your-real-key"
jiuwenswarm-process "Reply CLI_OK" --model "service-model-name" --api-base "https://your-service.example/v1" --api-key-env JIUWEN_MODEL_KEY --model-provider OpenAI --mode agent.code.normal --timeout 120
```

Rotate only the key of a configured model:

```bat
jiuwenswarm-process "Reply KEY_OK" --model "configured-alias" --api-key-env JIUWEN_MODEL_KEY --timeout 120
```

Verify actual tools and output:

```bat
jiuwenswarm-process "Use read_file to read notes.txt and return its complete content. Do not change files." --model "configured-alias" --cwd "D:\cli-test" --mode agent.code.normal --show-tools --timeout 120
jiuwenswarm-process "Compute 17 plus 25" --model "configured-alias" --output json --timeout 120
```

Continue an existing Process CLI session; repeat ephemeral connection flags
on each new invocation:

```bat
jiuwenswarm-process "Continue the task" --session "existing-process-cli-session-id" --model "service-model-name" --api-base "https://your-service.example/v1" --api-key-env JIUWEN_MODEL_KEY --timeout 120
```

Help is available with `-h`, `--help`, and `-help`. After a command exits, run
`echo %ERRORLEVEL%` separately. Success is `0`, invalid configuration is `2`,
execution/authentication failure is `1`, and timeout is `124`. Verify actual
task content as well as exit status.

## 3. Precedence and isolation

Omitting the new flags preserves existing defaults. `--model` resolves through
the Runtime catalog without saving a selection. An explicitly named key
environment variable overrides the configured credential and is captured
before dotenv can overwrite it. A complete connection becomes the scoped
Runtime default, inherited by child Agent tasks. Configured default groups and
AgentOS fallbacks cannot reroute this connection.

Changing the API destination constructs a fresh client configuration, with TLS
verification enabled. Old credentials, headers, and endpoint dialect settings
are not forwarded. Key-only overrides retain existing connection settings but
remove authentication headers that could override the new key.

The overlay is held in an async context and resets on exit. It never calls a
configuration writer or adds API credentials to an Agent request. Other
processes and unrelated Web/TUI contexts retain their configuration. A
resumed session does not restore ephemeral credentials: resend the options
and retain the key environment variable in the invoking CMD.

## 4. File access approval

With permissions enabled, standalone prompt commands directly deny operations
that require approval, without displaying an approval question. A one-shot
machine request that cannot receive interaction answers follows the same policy.
Writes already allowed by the trusted-path policy continue to work. Interactive
CLI workers and duplex machine requests retain their approval response flow.

Standalone prompt commands do not expose `ask_user` or read interactive choices,
even in a real terminal. An unexpected interaction event ends the command with
exit code `4` without waiting for user input.

With `permissions.file_guard.enabled` also enabled, shell commands whose file
accesses cannot be determined require approval, including variable-based command
chains, redirections, interpreters, and unrecognized programs. Non-interactive
runs deny these commands. Interactive runs require explicit approval each time;
Smart Approval cannot approve them automatically, and approvals are not remembered
for the session or persisted. Recognized literal paths retain existing path policy.

`--cwd` selects a working directory and `--trusted-dir` adds trusted paths;
neither provides an operating-system sandbox. Disabling permissions skips these
checks. Disabling only file protection leaves tool-level permission rules in
effect. This policy is exclusive to `process_cli`; other channels retain their
existing behavior.

## 5. Codex comparison

Codex `exec` supports `-m/--model` and `-c/--config key=value`. Provider config
uses `model_providers.<id>.base_url` and `env_key`. This implementation follows
the same per-invocation selection and environment-key pattern; JiuwenSwarm
and agent-core continue to define their own flags and model wire protocols.

Official references: [CLI commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli),
[provider configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[authentication](https://learn.chatgpt.com/docs/auth).

---

[Documentation index](../README_EN.md) · [Project home](../../README.md)
