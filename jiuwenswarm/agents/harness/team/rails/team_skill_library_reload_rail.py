# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reload member Skill views after writes into the global Skill library.

Backward-incompatible rename: this module used to export
``TeamSharedSkillLinkRefreshRail``, which materialized a per-team symlink view
of the Skill library. Teams no longer own a mirrored ``skills/`` directory, so
the rail is now ``TeamSkillLibraryReloadRail`` and its constructor takes
``reload_skill_views`` instead of ``refresh_links``.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.core.sys_operation.cwd import get_cwd
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.rails.base import DeepAgentRail

if TYPE_CHECKING:
    from openjiuwen.harness.deep_agent import DeepAgent

logger = logging.getLogger(__name__)


async def reload_agent_skill_views(agent: "DeepAgent") -> int:
    """Reload every ``SkillUseRail`` mounted on an agent.

    Skills live in exactly one physical library, and an agent's view of it is
    decided by its ``skills-visibility.json`` document rather than by a
    materialized per-agent directory. Making a library change visible is
    therefore only a matter of asking each Skill rail to re-read the library
    and the metadata.

    Args:
        agent: Agent whose Skill rails should be reloaded.

    Returns:
        Number of Skill rails that reloaded successfully.
    """
    reloaded = 0
    try:
        skill_rails = agent.find_rails_by_type((SkillUseRail,))
    except Exception as exc:
        logger.warning("[TeamSkillLibraryReload] cannot enumerate skill rails: %s", exc)
        return reloaded

    for rail in skill_rails:
        try:
            await rail.reload_skills()
            reloaded += 1
        except Exception as exc:
            logger.warning("[TeamSkillLibraryReload] skill view reload failed: %s", exc)
    return reloaded


class TeamSkillLibraryReloadRail(DeepAgentRail):
    """Reload the member's Skill view after a write into the global Skill library.

    Team members share one physical Skill library and differ only by the
    visibility metadata that selects what each of them may see. Nothing has to
    be copied or linked when a Skill source changes; the member only has to
    re-read the library, which is what this rail triggers as soon as a
    write-like tool touches the library root.
    """

    WRITE_TOOLS = frozenset({"write_file", "edit_file"})

    def __init__(
        self,
        *,
        global_skills_dir: Path,
        reload_skill_views: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        """Bind the rail to the single physical Skill library.

        Args:
            global_skills_dir: Root of the one Skill library shared by every
                agent.
            reload_skill_views: Optional hook run instead of the built-in
                reload. Defaults to reloading every ``SkillUseRail`` mounted on
                the agent this rail belongs to.
        """
        super().__init__()
        self._global_skills_dir = global_skills_dir
        self._reload_skill_views = reload_skill_views

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Reload Skill views after write-like tools touch the global library."""
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs):
            return

        tool_name = str(inputs.tool_name or "").strip()
        if tool_name not in self.WRITE_TOOLS:
            return
        file_path = self._extract_file_path(inputs)
        if not file_path or not self._is_under_global_skills_dir(file_path):
            return
        await self._reload(ctx.agent)

    async def _reload(self, agent: "DeepAgent") -> None:
        """Run the injected hook, or reload the agent's own Skill rails."""
        if self._reload_skill_views is None:
            await reload_agent_skill_views(agent)
            return
        try:
            result = self._reload_skill_views()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("[TeamSkillLibraryReload] reload hook failed: %s", exc)

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


__all__ = ["TeamSkillLibraryReloadRail", "reload_agent_skill_views"]
