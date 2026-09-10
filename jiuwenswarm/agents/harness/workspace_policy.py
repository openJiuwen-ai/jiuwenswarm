# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retire agent-core's default file memory for Swarm workspaces.

Apply before workspace construction, including workspaces built inside
agent-core for team members and subagents. Filtering an individual workspace
is insufficient: the SDK supplements missing nodes from its default schema.
No existing files are modified.
"""

from openjiuwen.harness.prompts.workspace_content import workspace_header
from openjiuwen.harness.workspace import workspace


def disable_file_memory() -> None:
    """Remove legacy file-memory defaults and their prompt instructions."""
    for schema in (workspace.DEFAULT_WORKSPACE_SCHEMA, workspace.DEFAULT_WORKSPACE_SCHEMA_EN):
        schema[:] = [node for node in schema if node["name"] not in {"USER.md", "memory"}]
    workspace_header.CONTEXT_FILES[:] = [
        name for name in workspace_header.CONTEXT_FILES if name not in {"USER.md", "MEMORY.md"}
    ]
    for language, content in workspace_header.IMPORTANT_FILES.items():
        workspace_header.IMPORTANT_FILES[language] = "\n".join(
            line for line in content.splitlines()
            if not any(path in line for path in ("`USER.md`", "`memory/"))
        ) + "\n"
