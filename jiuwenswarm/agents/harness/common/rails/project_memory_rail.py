# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ProjectMemoryRail -- jiuwenswarm product-side rail.

On every ``before_model_call``, rebuild the ``project_memory`` section from
cached discovery results. Cache invalidation happens explicitly on write-like
tool calls, mode/workspace switches, and also falls back to a lightweight
filesystem snapshot check for correctness.

This rail lives in jiuwenswarm (not agent-core) so that:

* agent-core stays untouched
* jiuwenswarm can evolve memory-loading semantics without upstream PRs
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.project_memory import (
    SECTION_NAME,
    build_project_memory_section,
    clear_project_memory_cache,
    discover_and_load_memory_files,
    merge_memory_content,
)
from jiuwenswarm.common.utils import logger

if TYPE_CHECKING:
    from openjiuwen.harness.deep_agent import DeepAgent


class ProjectMemoryRail(DeepAgentRail):
    """Auto-load project memory files and inject them into the system prompt.

    Loaded sources (all read-only; only ``JIUWENSWARM.md`` and
    ``JIUWENSWARM.local.md`` are written by ``/init``):

    * **Project root**: ``JIUWENSWARM.md``, ``JIUWENSWARM.local.md``,
      ``.jiuwen/JIUWENSWARM.md``, ``.jiuwen/rules/*.md``
    * **User level**: ``~/.jiuwen/JIUWENSWARM.md``, ``~/.jiuwen/rules/*.md``
    * **Managed**: ``/etc/jiuwen/JIUWENSWARM.md``, ``/etc/jiuwen/rules/*.md``
    * **Additional dirs**: explicit project-memory directories passed to the rail

    Priority (low -> high): ``managed < user < project (root -> cwd) < local``.
    """

    WRITE_LIKE_TOOLS = frozenset({
        "write_file",
        "edit_file",
        "write_text_file",
        "write",
        "delete_file",
        "delete",
        "move_file",
        "rename_file",
    })

    # Higher than MEMORY(85) / TOOLS(~100); lower than RUNTIME (see
    # agent-core ``prompts/sections/__init__.py`` SectionName for the
    # conventional range).
    SECTION_PRIORITY = 120

    def __init__(
        self,
        workspace: str,
        *,
        language: str = "cn",
        max_chars: int = 60_000,
        additional_directories: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__()
        # NOTE: 父类 DeepAgentRail.set_workspace() 会把 self.workspace 替换成
        # Workspace 对象（DeepAgent.register_rail 内部强制注入）。这里改用
        # 私有属性 _workspace_path 保存构造期传入的字符串路径，避免被覆盖。
        self._workspace_path: str = workspace
        self._language: str = language
        self._max_chars: int = max_chars
        self._additional_directories: tuple[str, ...] = tuple(additional_directories or ())
        self._system_prompt_builder = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(self, agent: "DeepAgent") -> None:
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        if self._system_prompt_builder is None:
            logger.warning(
                "[ProjectMemoryRail] agent has no system_prompt_builder; disabled"
            )
            return
        logger.info(
            "[ProjectMemoryRail] initialized for workspace=%s language=%s",
            self.resolve_workspace_path(),
            self._language,
        )

    def uninit(self, agent: "DeepAgent") -> None:
        """Clear the injected section to avoid stale content on rail swap."""
        clear_project_memory_cache(self.resolve_workspace_path())
        if self._system_prompt_builder is not None:
            self._system_prompt_builder.remove_section(SECTION_NAME)

    # ------------------------------------------------------------------
    # Public knobs (per-request hot updates, parallel to RuntimePromptRail)
    # ------------------------------------------------------------------

    def set_language(self, language: str) -> None:
        """Per-request language switch (cn/en). No-op if value unchanged."""
        if language and language != self._language:
            self._language = language

    def get_language(self) -> str:
        """Get current language setting."""
        return self._language

    def set_additional_directories(self, dirs: tuple[str, ...] | list[str] | None) -> None:
        """Per-request hot update of additional scan directories.

        Called by the adapter when ``trusted_dirs`` arrives from the client,
        ensuring the rail always searches the directory where /init wrote
        JIUWENSWARM.md (typically the CLI process's cwd, which differs from
        the AgentServer process cwd).
        """
        extra = tuple(dirs or ())
        # Merge with constructor-level dirs, dedup by realpath
        base_resolved = {os.path.realpath(d) for d in self._additional_directories}
        merged = list(self._additional_directories)
        for d in extra:
            if os.path.realpath(d) not in base_resolved:
                merged.append(d)
                base_resolved.add(os.path.realpath(d))
        self._additional_directories = tuple(merged)

    # ------------------------------------------------------------------
    # Hook
    # ------------------------------------------------------------------

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Refresh the ``project_memory`` section from cached discovery state."""
        if self._system_prompt_builder is None:
            return

        workspace_path = self.resolve_workspace_path()

        try:
            # ``paths:`` scoped rules are evaluated against the active workspace/cwd
            # for this turn. The surrounding DeepAgent callback does not provide a
            # stable "current target file" concept here.
            files = discover_and_load_memory_files(
                workspace=workspace_path,
                target_path=workspace_path,
                additional_directories=self._additional_directories,
            )
        except (OSError, ValueError, TypeError) as exc:
            # 不能让 rail 崩坏 model call；但要把根因留在日志里，方便排查。
            logger.exception(
                "[ProjectMemoryRail] discovery failed for workspace=%s: %s",
                workspace_path,
                exc,
            )
            files = []

        merged = merge_memory_content(files, max_chars=self._max_chars)

        # Always drop the previous section so the current state of disk wins.
        self._system_prompt_builder.remove_section(SECTION_NAME)

        if not merged.strip():
            return

        section = build_project_memory_section(
            merged,
            language=self._language,
            priority=self.SECTION_PRIORITY,
        )
        if section is not None:
            self._system_prompt_builder.add_section(section)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Explicitly invalidate memory discovery cache after write-like tools."""
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "").strip()
        if tool_name not in self.WRITE_LIKE_TOOLS:
            return
        clear_project_memory_cache(self.resolve_workspace_path())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def resolve_workspace_path(self) -> str:
        """Resolve the workspace root as a string path.

        Order of precedence:
          1. ``self.workspace.root_path`` -- injected by ``DeepAgent.register_rail``
             via ``DeepAgentRail.set_workspace(Workspace(...))``. Most up-to-date.
          2. ``self._workspace_path`` -- string path passed at construction.
        """
        ws_obj = getattr(self, "workspace", None)
        if ws_obj is not None:
            root = getattr(ws_obj, "root_path", None)
            if root:
                return str(root)
        return self._workspace_path


__all__ = ["ProjectMemoryRail"]
