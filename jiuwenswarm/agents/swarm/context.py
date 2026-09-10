# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime build context for swarm provider-based team assembly.

``SwarmBuildContext`` carries the live, non-serializable jiuwenswarm handles
a capability provider needs at build time. It extends openjiuwen's
``BuildContext`` so openjiuwen's ``setup_agent`` can fill the per-member view
(``member_name`` / ``role`` / ``workspace`` / ``member_card_id``) via
``derive()`` while the platform attaches the per-team/per-process handles.

It deliberately holds no parent DeepAgent: team members build from the shared
config source (``config.yaml``), not by inheriting a pre-built single agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams import paths as ojw_paths
from openjiuwen.agent_teams.schema.build_context import BuildContext


_heartbeat_job_service: Any | None = None


def set_heartbeat_job_service(service: Any | None) -> None:
    """Publish the AgentServer-owned Heartbeat service to Team builders."""
    global _heartbeat_job_service
    _heartbeat_job_service = service


def get_heartbeat_job_service() -> Any | None:
    """Return the process-local Heartbeat service, if AgentServer installed it."""
    return _heartbeat_job_service


@dataclass
class SwarmBuildContext(BuildContext):
    """BuildContext subclass carrying jiuwenswarm runtime handles.

    Per-team / per-process fields are set once when enriching the team spec.
    Per-member fields (``member_name`` / ``role`` / ``language`` / ``workspace``
    / ``member_card_id``) are inherited from ``BuildContext`` and filled by
    openjiuwen's ``setup_agent`` through ``derive()``.

    Attributes:
        session_id: Active session id.
        request_id: Originating request id (may be None).
        user_id: Authenticated request owner, used by user-routed tools.
        channel_id: Raw channel id from the request (may be None).
        channel: Resolved channel key for ``get_team_manager`` (``channel_id``
            or "default").
        request_metadata: Request metadata dict (carries ``mode`` etc.).
        mode: Request mode (e.g. "team"); replaces the old parent
            ``_jiuwenswarm_adapter_mode`` lookup.
        project_dir: Resolved project directory (from request / session /
            config); replaces the old parent ``_jiuwenswarm_project_dir``.
        trusted_dirs: Directories the client declared as trusted for this
            request. Members feed them to the runtime-prompt and permission
            rails so an external-directory check treats these subtrees as
            internal, matching single-agent behaviour.
        team_id: Team name.
        team_ws_root: Team shared workspace root path.
        task_workspace_root: Root of the shared final-deliverables directory
            a projectless team member is told about (``team-workspace/artifacts
            /<date>/chat-<n>/``). Only set when ``project_dir`` is None — a
            team member bound to a project keeps deliverables in the project
            and these stay None so the runtime prompt rail's
            ``has_projectless_task`` branch stays off. The per-member
            ``task_work_dir`` (the member workspace the prompt names as the
            temporary working directory) is resolved in the rail from each
            member's workspace root, not carried here — it is per-member.
        team_outputs_dir: The final-deliverables directory (under
            ``task_workspace_root``). Declared here explicitly (though the
            base ``BuildContext`` also carries it once openjiuwen catches up)
            so construction does not depend on the pinned openjiuwen version,
            and the openjiuwen configurator and the team policy rail can
            surface it to the team info body without a platform-specific
            accessor. None when project_dir is set.
        team_skill_visibility_path: Team-level Skill visibility metadata file
            (``team_ws_root/skills-visibility.json``). Replaces the former
            ``team_skills_dir``: a team owns no Skill directory of its own, only
            a metadata document composed with each member's document. The
            member-level counterpart is not a field but the
            :meth:`resolve_member_skill_visibility_path` method, because it can
            only be derived once the per-member view has been filled in.
        global_skills_dir: The one physical Skill library every agent reads.
        trajectory_span_processor: Process-level trajectory span processor
            shared by Team evolution rails.
        heartbeat_job_service: Process-level AgentServer Heartbeat service.
            This live handle is intentionally omitted from serialized seeds and
            reacquired from the receiving JiuwenSwarm process.
        config: The resolved ``config.yaml`` mapping (``get_config()``).
        skill_retrieval_toolkit: Per-member, non-serializable Symphony
            discovery runtime shared by skill_index and its prompt rail.

    Note:
        Backward incompatible: the ``team_skills_dir`` field and its seed key
        were removed. A seed produced by an older process carries no
        ``team_skill_visibility_path`` and rebuilds with ``None``, which leaves
        the member document as the only visibility authority.
    """

    session_id: str = ""
    request_id: str | None = None
    user_id: str | None = None
    channel_id: str | None = None
    channel: str = "default"
    request_metadata: dict[str, Any] | None = None
    mode: str = "team"
    project_dir: str | None = None
    trusted_dirs: list[str] | None = None
    disable_teammate_worktree: bool = False
    team_id: str = ""
    team_ws_root: str | None = None
    task_workspace_root: str | None = None
    team_outputs_dir: str | None = None
    team_skill_visibility_path: str | None = None
    global_skills_dir: str | None = None
    trajectory_span_processor: Any = None
    heartbeat_job_service: Any = None
    config: dict[str, Any] | None = None
    skill_retrieval_toolkit: Any = None

    def resolve_member_skill_visibility_path(self) -> str | None:
        """Resolve the current member's Skill visibility metadata file path.

        Named ``resolve_*`` on purpose: its team-level counterpart
        ``team_skill_visibility_path`` is a plain field, and a consumer that
        reads both must not mistake this one for a value. A ``getattr`` that
        silently returned a bound method here used to be handed to ``Path()``,
        which failed at rail construction for every member.

        Derived rather than stored: the base context is per-team and
        ``member_name`` / ``workspace`` are only filled per member by
        ``setup_agent`` through ``derive()``, so a per-member path could never
        travel in :meth:`to_seed`.

        The member workspace root is preferred over recomputing the stable
        layout path, so a workspace that was relocated (an independent
        DeepAgent workspace exposed under the team tree by symlink) keeps its
        metadata next to the workspace it actually uses.

        Returns:
            The absolute metadata path, or ``None`` when neither a workspace
            nor a (team, member) identity pair is available.
        """
        workspace = self.workspace
        root_path = getattr(workspace, "root_path", None) if workspace else None
        if root_path:
            return str(Path(root_path) / ojw_paths.SKILL_VISIBILITY_FILENAME)
        member_name = str(self.member_name or "").strip()
        if not member_name or not self.team_id:
            return None
        return str(ojw_paths.member_skill_visibility_path(self.team_id, member_name))

    def resolve_member_work_dir(self) -> str | None:
        """Resolve this member's isolated temporary working directory.

        Named ``resolve_*`` on purpose: like
        :meth:`resolve_member_skill_visibility_path`, it can only be computed
        once the per-member view (``member_name``) is filled by ``setup_agent``
        through ``derive()``. The member work directory lives under the shared
        ``task_workspace_root`` (``team-workspace/artifacts/<date>/chat-<n>/
        work/<member_slug>/``), so it is per-member and never travels in
        :meth:`to_seed`.

        Returns:
            The absolute per-member work directory path, or ``None`` when no
            shared artifact root is configured (a member bound to a project,
            whose deliverables stay in the project).
        """
        if not self.task_workspace_root:
            return None
        from jiuwenswarm.common.team_artifacts import resolve_member_work_dir

        return str(resolve_member_work_dir(self.task_workspace_root, self.member_name))

    def to_seed(self) -> dict[str, Any]:
        """Export the serializable per-team / per-process fields as a seed.

        The seed travels on ``TeamAgentSpec.build_context_seed`` across a
        serialization boundary; :meth:`from_seed` rebuilds the context from it
        plus locally-sourced non-serializable handles. Per-member fields
        (``member_name`` / ``role`` / ``language`` / ``workspace`` /
        ``member_card_id``) are intentionally excluded — ``setup_agent`` fills
        them per member through ``derive()``. The live ``config`` and
        ``trajectory_span_processor`` are excluded too: the receiver supplies them.

        Returns:
            A plain mapping of serializable primitives.
        """
        return {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "user_id": self.user_id,
            "channel_id": self.channel_id,
            "channel": self.channel,
            "request_metadata": self.request_metadata,
            "mode": self.mode,
            "project_dir": self.project_dir,
            "trusted_dirs": list(self.trusted_dirs) if self.trusted_dirs else None,
            "disable_teammate_worktree": self.disable_teammate_worktree,
            "team_id": self.team_id,
            "team_ws_root": self.team_ws_root,
            "task_workspace_root": self.task_workspace_root,
            "team_outputs_dir": self.team_outputs_dir,
            "team_skill_visibility_path": self.team_skill_visibility_path,
            "global_skills_dir": self.global_skills_dir,
        }

    @classmethod
    def from_seed(
        cls,
        seed: dict[str, Any],
        *,
        config: dict[str, Any] | None,
        trajectory_span_processor: Any,
    ) -> "SwarmBuildContext":
        """Rebuild a context from a :meth:`to_seed` mapping plus local handles.

        Args:
            seed: The serializable mapping produced by :meth:`to_seed`.
            config: The receiving process's resolved ``config.yaml`` mapping.
            trajectory_span_processor: The process-level Team trajectory processor.

        Returns:
            A ``SwarmBuildContext`` with the seed fields restored and the
            non-serializable handles sourced from the receiving process.
        """
        return cls(
            session_id=seed.get("session_id", ""),
            request_id=seed.get("request_id"),
            user_id=seed.get("user_id"),
            channel_id=seed.get("channel_id"),
            channel=seed.get("channel") or "default",
            request_metadata=seed.get("request_metadata"),
            mode=seed.get("mode") or "team",
            project_dir=seed.get("project_dir"),
            trusted_dirs=seed.get("trusted_dirs"),
            disable_teammate_worktree=bool(
                seed.get("disable_teammate_worktree", False)
            ),
            team_id=seed.get("team_id", ""),
            team_ws_root=seed.get("team_ws_root"),
            task_workspace_root=seed.get("task_workspace_root"),
            team_outputs_dir=seed.get("team_outputs_dir"),
            team_skill_visibility_path=seed.get("team_skill_visibility_path"),
            global_skills_dir=seed.get("global_skills_dir"),
            trajectory_span_processor=trajectory_span_processor,
            heartbeat_job_service=get_heartbeat_job_service(),
            config=config,
        )


__all__ = [
    "SwarmBuildContext",
    "get_heartbeat_job_service",
    "set_heartbeat_job_service",
]
