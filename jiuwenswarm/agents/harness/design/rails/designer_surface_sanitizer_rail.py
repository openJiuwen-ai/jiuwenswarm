# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Design leader surface sanitizer (team.design only, single-repo policy).

The design leader is assembled through the standard agent-core configurator,
which unconditionally mounts capability surfaces the design canvas has no use
for (team collaboration/task tools, org control, workspace_meta, a build_team
bootstrap prompt section). Rather than patching agent-core, this rail keeps
only what the design profile and its base support explicitly need and
**subtracts everything else** after the fact — a keep-list walked over the
live surface, not a removal list that has to track every tool agent-core
adds. All removal happens in jiuwenswarm, so tuning the design leader's
surface is a single-repo change.

Tools kept (card names as registered by ability_manager.add_ability):

* the design canvas toolset — designer_graph_* (design profile), the
  authoring/execution tools (designer_graph_autorun / swarmflow),
  and the picked model/media tools (call_model / read_upstream /
  call_image_model / call_video_model / ffmpeg_compose / mix_audio).
* ``ask_user`` — the structured-question contract with the user.
* vision / audio tools (image_ocr / visual_question_answering /
  audio_transcription / audio_question_answering / audio_metadata).
* web tools (free_search / fetch_webpage / paid_search).
* base sys_operation support (list_files / glob / bash / grep / read_file —
  kept per user decision 2026-10-10; the skill rail reads SKILL.md front
  matter through the sys_operation engine).

Everything else — team collaboration (build_team / send_message / spawn_* /
task tools / swarmflow …), org_* control tools, ``workspace_meta``, the
progressive-disclosure meta tools (``tool_search`` / ``tool_call`` — the
design assembly strips ``progressive_tool`` so they never register, and the
keep-list drops them should that ever change), or any tool a future
agent-core build mounts unconditionally — is dropped.

Prompt sections are subtracted by name instead: the leader's section set is
stable (core harness sections + the team/design policy sections), and only
``team_bootstrap`` — the build_team instruction — must never reach the
model, so a one-entry removal list stays correct. Tools use the opposite
strategy precisely because the tool surface is *not* stable: agent-core adds
collaboration tools over time, and a keep-list drops any future one without
a name change here.

Timing (why init is enough):
  rails initialize lazily in priority-descending order on the first stream
  use — *after* activate() already mounted the org tools. This rail uses a
  low priority (10) so it initializes last and sees every polluted surface.
  Native rebuilds re-run init, and the removals are idempotent.
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

# Tool *card names* kept on the design leader's surface. The leader is a pure
# conversation surface over the canvas: only the design profile's own tools,
# the ask-user contract, and the base sys_operation support survive. Anything
# else mounted by agent-core's team path — or by a future build — is dropped
# wholesale by walking the ability manager and removing every card not on
# this list.
_KEEP_TOOL_NAMES: frozenset[str] = frozenset({
    # Design canvas (design profile / designer rails).
    "designer_graph_get",
    "designer_graph_patch",
    "designer_story_edit",
    "designer_node_run",
    "designer_node_complete",
    "designer_graph_autorun",
    "swarmflow",
    "call_model",
    "read_upstream",
    "call_image_model",
    "call_video_model",
    "ffmpeg_compose",
    "mix_audio",
    # Conversation contract.
    "ask_user",
    # Vision / audio (design profile tool group).
    "image_ocr",
    "visual_question_answering",
    "audio_transcription",
    "audio_question_answering",
    "audio_metadata",
    # Web search / fetch (design leader tool group).
    "free_search",
    "fetch_webpage",
    "paid_search",
    # Base sys_operation support (user decision 2026-10-10).
    "list_files",
    "glob",
    "bash",
    "grep",
    "read_file",
})

# Prompt sections removed on every model call. ``team_bootstrap`` is the
# build_team instruction — the design leader has no build_team tool, so it
# must never reach the model (agent-core spec flag removed 2026-10; this is
# the only mechanism keeping it off). The section set is stable, so a
# one-entry removal list stays correct — unlike tools, which use a keep-list
# because the tool surface keeps growing.
_REMOVED_SECTIONS: tuple[str, ...] = ("team_bootstrap",)


class DesignerSurfaceSanitizerRail(DeepAgentRail):
    """Subtract non-design capabilities from the design leader surface."""

    # Low priority: initialize (and run before_model_call) *after* the core
    # team rails (TeamToolRail=90, TeamPolicyRail=12) so every removal sees
    # the fully assembled surface.
    priority = 10

    def __init__(self) -> None:
        super().__init__()
        self._removed_tools: list[str] = []
        self._agent: Any = None
        self.system_prompt_builder = None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _remove_tools(self, agent: Any) -> None:
        """Drop every tool card outside the keep-list from the ability manager."""
        from openjiuwen.core.foundation.tool import ToolCard

        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is None:
            return
        for card in ability_manager.list():
            if not isinstance(card, ToolCard):
                continue
            name = card.name
            if not name or name in _KEEP_TOOL_NAMES:
                continue
            try:
                ability_manager.remove_ability(name)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[DesignerSurfaceSanitizer] tool %s already absent", name
                )
                continue
            self._removed_tools.append(name)

    # ------------------------------------------------------------------
    # rail lifecycle
    # ------------------------------------------------------------------

    def init(self, agent: Any) -> None:
        super().init(agent)
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._agent = agent
        self._remove_tools(agent)
        if self._removed_tools:
            logger.info(
                "[DesignerSurfaceSanitizer] removed %d tools from the design "
                "leader surface: %s",
                len(self._removed_tools),
                sorted(set(self._removed_tools)),
            )
            self._removed_tools = []

    def uninit(self, agent: Any) -> None:
        # Nothing to restore: the removed tools belong to rails that manage
        # their own teardown, and the harness is being torn down anyway.
        self.system_prompt_builder = None
        self._agent = None
        self._removed_tools = []

    async def before_model_call(self, ctx: Any) -> None:
        _ = ctx
        # Defensive re-removal: a native rebuild re-mounts the dynamic tool
        # registry before this rail re-initializes; if any mounting path
        # slipped a polluted tool back in between init and this model call,
        # drop it again. Idempotent and cheap (dict lookups).
        if self._agent is not None:
            self._remove_tools(self._agent)
        builder = self.system_prompt_builder
        if builder is None:
            return
        for section in _REMOVED_SECTIONS:
            try:
                builder.remove_section(section)
            except Exception:  # noqa: BLE001
                continue


__all__ = ["DesignerSurfaceSanitizerRail"]
