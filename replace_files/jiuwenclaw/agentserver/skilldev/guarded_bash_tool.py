from __future__ import annotations

import logging
from typing import Any

from openjiuwen.harness.tools.bash import BashTool

from jiuwenclaw.agentserver.skilldev.utils.archive_security import (
    guard_archive_command_before_exec,
)

logger = logging.getLogger(__name__)


class GuardedBashTool(BashTool):
    """Bash tool wrapper with pre-execution archive validation."""

    def __init__(self, sys_operation=None, language: str = "cn", agent_id: str | None = None):
        super().__init__(sys_operation, language=language, agent_id=agent_id)

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> Any:
        command = self._extract_command(inputs)
        if command:
            guard_archive_command_before_exec(
                command,
                cwd=self._extract_cwd(inputs),
            )
        return await super().invoke(inputs, **kwargs)

    @staticmethod
    def _extract_command(inputs: dict[str, Any]) -> str:
        for key in ("command", "cmd", "script", "shell_command"):
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    @staticmethod
    def _extract_cwd(inputs: dict[str, Any]) -> str | None:
        for key in ("cwd", "workdir", "working_dir", "working_directory"):
            value = inputs.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            return value.strip()
        return None
