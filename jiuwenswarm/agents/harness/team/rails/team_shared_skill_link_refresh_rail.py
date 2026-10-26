# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Refresh team shared skill links after skill-root file writes."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.core.sys_operation.cwd import get_cwd
from openjiuwen.harness.rails.base import DeepAgentRail


class TeamSharedSkillLinkRefreshRail(DeepAgentRail):
    """Refresh team shared skill links when tools write into the global skills root."""

    WRITE_TOOLS = frozenset({"write_file", "edit_file"})

    def __init__(
        self,
        *,
        global_skills_dir: Path,
        refresh_links: Callable[[], None],
    ) -> None:
        super().__init__()
        self._global_skills_dir = global_skills_dir
        self._refresh_links = refresh_links

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Refresh shared links after write-like tools touch the global skills root."""
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs):
            return

        tool_name = str(inputs.tool_name or "").strip()
        if tool_name not in self.WRITE_TOOLS:
            return
        file_path = self._extract_file_path(inputs)
        if not file_path or not self._is_under_global_skills_dir(file_path):
            return
        self._refresh_links()

    @staticmethod
    def _extract_file_path(inputs: ToolCallInputs) -> str:
        args = inputs.tool_args
        if args is None:
            args = {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (TypeError, ValueError):
                return ""
        if not isinstance(args, dict):
            return ""
        return str(args.get("file_path", "") or args.get("path", "")).strip()

    def _is_under_global_skills_dir(self, file_path: str) -> bool:
        try:
            candidate = Path(os.path.expanduser(file_path))
            if not candidate.is_absolute():
                candidate = Path(get_cwd()).expanduser().resolve() / candidate
            resolved_candidate = candidate.resolve()
            resolved_root = self._global_skills_dir.resolve()
        except (OSError, ValueError):
            return False
        return resolved_candidate == resolved_root or resolved_root in resolved_candidate.parents
