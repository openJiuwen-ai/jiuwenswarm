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
from typing import Any

from openjiuwen.agent_teams.schema.build_context import BuildContext


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
        channel_id: Raw channel id from the request (may be None).
        channel: Resolved channel key for ``get_team_manager`` (``channel_id``
            or "default").
        request_metadata: Request metadata dict (carries ``mode`` etc.).
        mode: Request mode (e.g. "team"); replaces the old parent
            ``_jiuwenswarm_adapter_mode`` lookup.
        project_dir: Resolved project directory (from request / session /
            config); replaces the old parent ``_jiuwenswarm_project_dir``.
        team_id: Team name.
        team_ws_root: Team shared workspace root path.
        team_skills_dir: Team shared skills directory (``team_ws_root/skills``).
        global_skills_dir: Global agent skills directory.
        config: The resolved ``config.yaml`` mapping (``get_config()``).
    """

    session_id: str = ""
    request_id: str | None = None
    channel_id: str | None = None
    channel: str = "default"
    request_metadata: dict[str, Any] | None = None
    mode: str = "team"
    project_dir: str | None = None
    disable_teammate_worktree: bool = False
    team_id: str = ""
    team_ws_root: str | None = None
    team_skills_dir: str | None = None
    global_skills_dir: str | None = None
    config: dict[str, Any] | None = None

    def to_seed(self) -> dict[str, Any]:
        """Export the serializable per-team / per-process fields as a seed.

        The seed travels on ``TeamAgentSpec.build_context_seed`` across a
        serialization boundary; :meth:`from_seed` rebuilds the context from it
        plus locally-sourced non-serializable handles. Per-member fields
        (``member_name`` / ``role`` / ``language`` / ``workspace`` /
        ``member_card_id``) are intentionally excluded — ``setup_agent`` fills
        them per member through ``derive()``. The live ``config`` is excluded too:
        the receiver supplies it.

        Returns:
            A plain mapping of serializable primitives.
        """
        return {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "channel_id": self.channel_id,
            "channel": self.channel,
            "request_metadata": self.request_metadata,
            "mode": self.mode,
            "project_dir": self.project_dir,
            "disable_teammate_worktree": self.disable_teammate_worktree,
            "team_id": self.team_id,
            "team_ws_root": self.team_ws_root,
            "team_skills_dir": self.team_skills_dir,
            "global_skills_dir": self.global_skills_dir,
            "language": self.language,
        }

    @classmethod
    def from_seed(
        cls,
        seed: dict[str, Any],
        *,
        config: dict[str, Any] | None,
    ) -> "SwarmBuildContext":
        """Rebuild a context from a :meth:`to_seed` mapping plus local handles.

        Args:
            seed: The serializable mapping produced by :meth:`to_seed`.
            config: The receiving process's resolved ``config.yaml`` mapping.

        Returns:
            A ``SwarmBuildContext`` with the seed fields restored and the
            non-serializable handles sourced from the receiving process.
        """
        language = str(seed.get("language") or "").strip().lower()
        if language not in {"cn", "en"}:
            language = "cn"
        return cls(
            session_id=seed.get("session_id", ""),
            request_id=seed.get("request_id"),
            channel_id=seed.get("channel_id"),
            channel=seed.get("channel") or "default",
            request_metadata=seed.get("request_metadata"),
            mode=seed.get("mode") or "team",
            project_dir=seed.get("project_dir"),
            disable_teammate_worktree=bool(seed.get("disable_teammate_worktree", False)),
            team_id=seed.get("team_id", ""),
            team_ws_root=seed.get("team_ws_root"),
            team_skills_dir=seed.get("team_skills_dir"),
            global_skills_dir=seed.get("global_skills_dir"),
            config=config,
            language=language,
        )


__all__ = ["SwarmBuildContext"]
