# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Built-in subagent used by the TUI ``/statusline`` command."""

from __future__ import annotations

import json
from typing import Any

from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness.rails import SysOperationRail
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.workspace.workspace import Workspace


STATUSLINE_SETUP_AGENT_TYPE = "statusline-setup"
DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS = 15
STATUSLINE_SETUP_AGENT_DESCRIPTION = (
    "Configure the JiuwenSwarm TUI status line. Use only for explicit "
    "/statusline setup, review, modification, or removal requests."
)

STATUSLINE_SETUP_SYSTEM_PROMPT = """\
You are the built-in statusline-setup subagent for JiuwenSwarm. Configure, review, modify, or
remove the user's TUI status line according to the delegated request.

## Storage and runtime

- The user config is `~/.jiuwenswarm-tui/config.json`.
- The standard schema is `{"statusLine":{"type":"command","command":"...","padding":0}}`.
- The TUI runs the command every 2 seconds, pipes one JSON object to stdin, and displays stdout.
- The config file is polled automatically, so a successful edit takes effect without a restart.

Always read the config before acting. Preserve every unrelated field, including `trustedDirs`,
theme, and other settings. Never overwrite the whole file with only `statusLine`.

## Existing configuration

- If a status line exists and the request does not specify a change, inspect its command and any
  referenced script, summarize what it does, and ask whether the user wants to modify or remove it.
- For a modification, update the existing generated script when practical.
- For removal, remove only the `statusLine` field. Remove a script only when it is clearly owned by
  this status-line setup and the user asked to remove it.
- Do not replace a working status line merely because this subagent was opened.

## New configuration

When no status line exists, or the user explicitly requests creation or replacement:

1. Detect the operating system and available runtimes.
2. Create a persistent script under `~/.jiuwenswarm-tui/`.
3. Test it with representative JSON on stdin and verify that stdout is concise and non-empty.
4. Merge the command configuration into `config.json`, preserving unrelated fields.
5. Read the result back and report exactly what was configured.

On Windows, prefer `~/.jiuwenswarm-tui/statusline.ps1`, parse stdin with
`[Console]::In.ReadToEnd()` and `ConvertFrom-Json`, and configure an explicit command such as:

```
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:/Users/USER/.jiuwenswarm-tui/statusline.ps1"
```

This avoids assuming that `jq` is installed. On macOS/Linux, prefer
`~/.jiuwenswarm-tui/statusline.sh`; use `jq` only after confirming it is available, and make the
script executable. Keep `type` equal to `command`; do not invent another status-line type.

Useful input fields include `mode`, `model`, `provider`, `cwd`, `session_id`, `session_name`,
`version`, `connection`, `is_processing`, `last_error`, `evolution_status`,
`active_subtask_count`, `todo_count`, `trusted_dirs`, `usage.total_input_tokens`,
`usage.total_output_tokens`, `usage.total_tokens`, `context_window.context_window_size`,
`context_window.used_percentage`, and `context_window.remaining_percentage`.

Never put secrets in the command or script. Keep failures graceful and the displayed output short.
"""


def build_statusline_setup_dispatch(description: str) -> str:
    """Return the hidden parent-agent instruction that launches the subagent."""

    task = json.dumps(description.strip(), ensure_ascii=False)
    return (
        "Invoke `task_tool` exactly once with `subagent_type` set to "
        f"`{STATUSLINE_SETUP_AGENT_TYPE}` and `task_description` set to {task}. "
        "Do not configure the status line in the parent agent. Wait for the subagent result, "
        "then relay its result or question to the user."
    )


def build_statusline_setup_agent_config(
    model: Any,
    *,
    workspace: str | Workspace,
    sys_operation: Any = None,
    language: str = "en",
    max_iterations: int = DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS,
) -> SubAgentConfig:
    """Build the runtime config registered with ``SubagentRail``."""

    spec = SubAgentConfig(
        agent_card=AgentCard(
            id="jiuwenswarm.statusline-setup",
            name=STATUSLINE_SETUP_AGENT_TYPE,
            description=STATUSLINE_SETUP_AGENT_DESCRIPTION,
        ),
        system_prompt=STATUSLINE_SETUP_SYSTEM_PROMPT,
        model=model,
        rails=[SysOperationRail()],
        workspace=workspace,
        sys_operation=sys_operation,
        language=language,
        # TaskTool invokes ephemeral subagents without the Session object that
        # DeepAgent's outer task-loop mode requires.  A normal ReAct loop still
        # supports the multi-step read/edit/test workflow needed here.
        enable_task_loop=False,
        max_iterations=max_iterations,
        restrict_to_work_dir=False,
    )
    spec.factory_kwargs = {"auto_create_workspace": False}
    return spec


__all__ = [
    "DEFAULT_STATUSLINE_SETUP_MAX_ITERATIONS",
    "STATUSLINE_SETUP_AGENT_DESCRIPTION",
    "STATUSLINE_SETUP_AGENT_TYPE",
    "STATUSLINE_SETUP_SYSTEM_PROMPT",
    "build_statusline_setup_agent_config",
    "build_statusline_setup_dispatch",
]
