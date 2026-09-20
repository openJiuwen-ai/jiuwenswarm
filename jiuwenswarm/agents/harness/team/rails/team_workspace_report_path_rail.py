# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prompt rail separating project deliverables from team-shared outputs.

The team workspace is the team's config / internal-data directory (prompts,
team.yaml, skill visibility, trajectories) accessed by absolute path — not a
task cwd and not a deliverables drop. Final deliverables for a projectless
member go to the shared team ``outputs/`` directory; deliverables for a member
bound to a project go in the project, matching single-agent behaviour.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)


class TeamWorkspaceReportPathRail(DeepAgentRail):
    """Inject the filesystem ownership policy for project and team artifacts."""

    priority = 5

    def __init__(
        self,
        *,
        root_dir: str,
        project_dir: str | None = None,
        outputs_dir: str | None = None,
        team_id: str | None = None,
        language: str = "cn",
    ) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._root_dir = str(Path(root_dir))
        self._project_dir = str(Path(project_dir)) if project_dir else None
        self._outputs_dir = str(Path(outputs_dir)) if outputs_dir else None
        self._team_id = team_id or ""
        self._language = language
        self._swarm_context: Any | None = None

    def bind_swarm_context(self, context: Any) -> None:
        """Capture team build context for evolution hot-reload registration.

        This rail is present even when skill evolution is disabled, so its
        lifecycle provides the manager with a mount context that can later
        rebuild the role-specific evolution rails. It does not add any
        evolution capability or prompt content by itself.
        """
        self._swarm_context = context

    def init(self, agent) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._register_swarm_context(agent)

    def uninit(self, agent) -> None:
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section("team_workspace_report_paths")
        self.system_prompt_builder = None

    def _register_swarm_context(self, agent: Any) -> None:
        """Register a team mount context when this rail came from swarm assembly."""
        context = self._swarm_context
        if context is None:
            return

        session_id = str(getattr(context, "session_id", "") or "").strip()
        channel = str(getattr(context, "channel", "default") or "default").strip()
        team_id = str(getattr(context, "team_id", "") or "").strip()
        role = str(getattr(context, "role", "") or "").strip()
        if not session_id or not team_id or role not in {"leader", "teammate"}:
            return

        try:
            from jiuwenswarm.agents.harness.team.team_manager import (
                _make_team_rail_mount_context,
                get_team_manager,
            )

            config = getattr(context, "config", None)
            mount_context = _make_team_rail_mount_context(
                agent=agent,
                role=role,
                channel=channel,
                language=str(getattr(context, "language", None) or self._language),
                member_name=getattr(context, "member_name", None),
                root_dir=getattr(context, "team_ws_root", None) or self._root_dir,
                project_dir=getattr(context, "project_dir", None) or self._project_dir,
                # No skills_dir: a team owns no Skill directory. Every agent
                # reads the single library and is narrowed by visibility
                # metadata, so there is nothing team-scoped to thread here.
                team_id=team_id,
                config=config,
                trajectory_span_processor=getattr(
                    context, "trajectory_span_processor", None
                ),
            )
            team_manager = get_team_manager(channel)
            if role == "leader":
                if team_manager.get_team_rail_context(session_id) is None:
                    team_manager.register_team_rail_context(session_id, mount_context)
            else:
                register_member_context = getattr(
                    team_manager,
                    "register_team_member_rail_context",
                    None,
                )
                if callable(register_member_context):
                    register_member_context(session_id, mount_context)
        except Exception as exc:
            logger.warning(
                "[TeamWorkspaceReportPathRail] team mount context registration failed: %s",
                exc,
            )

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        _ = ctx
        if self.system_prompt_builder is None:
            return

        # The team workspace is a config / internal-data directory accessed by
        # absolute path, never a deliverables drop. Final deliverables land in
        # the project for members bound to one (matching single-agent
        # behaviour), or in the shared team ``outputs/`` directory for
        # projectless members.
        outputs = self._outputs_dir
        if self._project_dir:
            deliverables_lines = (
                f"- User project root: `{self._project_dir}`\n"
                "- Source code, tests, configuration, project documentation, and "
                "files the user asks to create or modify belong in the active "
                "project working root. When worktree isolation is active, the "
                "worktree is the active project working root: use its cwd and "
                "never bypass it with an absolute path to the main checkout.\n"
                "- Without worktree isolation, use project-relative paths or "
                "absolute paths under the user project root.\n"
                "- This member is bound to a project, so final deliverables stay "
                "in the project — do not write them to the team shared "
                "deliverables directory.\n"
            )
        elif outputs:
            deliverables_lines = (
                "- No user project root is bound to this member. Final "
                "deliverables (reports, exports, data, and other output the user "
                "will receive) go to the shared team deliverables directory:\n"
                f"  - Final deliverables directory: `{outputs}`\n"
                "- The whole team shares this one directory; distinguish your "
                "outputs by filename.\n"
                "- Intermediate files, caches, and temporary scripts stay in "
                "your own working directory (the per-member temporary working "
                "directory named in the directory boundaries section); only "
                "final deliverables go to the shared directory above.\n"
            )
        else:
            deliverables_lines = (
                "- No user project root is available and no shared deliverables "
                "directory is configured. Resolve the project directory or "
                "confirm a destination before creating deliverables; do not "
                "silently drop them in the team workspace.\n"
            )

        content = (
            "# Project and Team Workspace File Policy\n\n"
            f"{deliverables_lines}"
            f"- Team shared workspace (config / internal data): `{self._root_dir}`\n"
            "- The team shared workspace holds the team's prompts, team.yaml, "
            "skill-visibility metadata, trajectories, and the shared "
            "deliverables directory. Access it by absolute path — do not create "
            "a `.team` sub-directory under it or under your own workspace.\n"
            "- Use the team shared workspace only for internal coordination "
            "artifacts such as plans, drafts, review notes, intermediate data, "
            "and handoff material that is not part of the user's project, or "
            "when the user explicitly requests that exact team-workspace "
            "destination.\n"
            "- Do not place final project files in the team shared workspace "
            "root; members bound to a project keep deliverables in the project "
            "and projectless members use the deliverables directory above.\n"
            "- `send_file_to_user` controls message delivery only; it never "
            "changes the required on-disk destination.\n"
            "- A path mentioned in `send_message` or a completion summary does "
            "not deliver the file. Call `send_file_to_user` only when the user "
            "requested a downloadable file.\n"
        )
        self.system_prompt_builder.add_section(
            PromptSection(
                name="team_workspace_report_paths",
                content={"cn": content, "en": content},
                priority=67,
            )
        )
