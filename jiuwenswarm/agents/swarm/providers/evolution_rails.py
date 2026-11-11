# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Skill-evolution rail providers for swarm provider-based team assembly.

This module ports the leader-only ``TeamSkillEvolutionRail`` /
``TeamSkillCreateRail`` and the teammate-only ``SkillEvolutionRail`` branches of
the legacy ``team_manager`` customizer into config-sourced rail providers.

In the legacy flow the customizer built each evolution rail and then imperatively
registered it with the per-channel :class:`TeamManager` (live-rail bookkeeping,
approval/sync targets, hot-reload contexts). Providers, however, only return rail
instances and never see the live agent. To preserve that bookkeeping without a
customizer, each evolution rail is wrapped in a thin subclass that overrides
``init(self, agent)`` to perform the same ``TeamManager`` registration once
openjiuwen attaches the rail to its agent. The build-time context handles are
captured on the instance at construction time by the provider factory.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    context_field,
    ElementKind,
    harness_element,
    param_field,
)
from openjiuwen.extensions.observability.demand import (
    get_trajectory_span_processor,
)
from openjiuwen.harness.rails import (
    EvolutionInterruptRail,
    SkillEvolutionRail,
    TeamSkillCreateRail,
    TeamSkillEvolutionRail,
)
from openjiuwen.harness.rails.evolution import EvolutionReviewRuntime

from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.server.runtime.agent_adapter.evolution_helpers import (
    merge_evolution_disabled_skills,
)
from jiuwenswarm.common.utils import primary_team_skill_library_dir
from jiuwenswarm.server.runtime.skill import load_execution_disabled_skills

logger = logging.getLogger(__name__)

# Provider name constants; namespaced under the shared "swarm." prefix.
TEAM_SKILL_EVOLUTION = "swarm.team_skill_evolution"
TEAM_SKILL_CREATE = "swarm.team_skill_create"
MEMBER_SKILL_EVOLUTION = "swarm.member_skill_evolution"
TEAM_TTSE = "swarm.team_ttse"
MEMBER_TTSE = "swarm.member_ttse"
EVOLUTION_INTERRUPT = "swarm.evolution_interrupt"


def _skill_library_dir(ctx: SwarmBuildContext) -> str:
    """Return the one physical Skill library evolution writes into.

    Prefers the build-context pin; falls back to
    :func:`primary_team_skill_library_dir` (same writable tip as slash/Hub /
    ``configure_global_skills_dir``) so Ace SHARED tips do not silently write
    into the agent workspace tree.

    Args:
        ctx: The per-member build context.

    Returns:
        The absolute Skill library path.
    """
    raw = str(ctx.global_skills_dir or "").strip()
    return raw or str(primary_team_skill_library_dir())


def _build_team_workspace_info(
    *,
    team_ws_root: str | None,
    skills_dir: str | None,
    team_id: str,
    config: dict[str, Any] | None,
) -> Any:
    """Build a fully-populated ``TeamWorkspaceInfo`` for rail-context registration.

    The hot-reload path (``update_evolution_config`` →
    ``_build_and_mount_member_rails_for_context`` → ``build_member_rails``)
    rebuilds the leader evolution / create rails from the stored
    ``TeamRailMountContext.team_workspace``; populating every field keeps that
    rebuild working for provider-mounted teams (the legacy customizer passed the
    same fields, the swarm path previously passed an empty object).

    Args:
        team_ws_root: Team shared workspace root path.
        skills_dir: The single Skill library (rebuild gate + authoring root).
        team_id: Team name.
        config: The resolved ``config.yaml`` mapping.

    Returns:
        A populated ``TeamWorkspaceInfo``.
    """
    from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
        TeamWorkspaceInfo,
    )

    return TeamWorkspaceInfo(
        root_dir=team_ws_root,
        skills_dir=skills_dir,
        team_id=team_id,
        config=config,
    )


def _register_team_rail_context(
    channel: str,
    session_id: str,
    agent: Any,
    team_workspace: Any,
) -> None:
    """Register the leader rail-mount context for hot config reloads.

    Mirrors the legacy customizer's ``register_team_rail_context`` call. Imported
    lazily so this provider module never imports ``team_manager`` at module load
    (the swarm assembly entry point is imported from team code paths).

    Args:
        channel: Resolved channel key used to locate the team manager.
        session_id: Active session id owning the rail.
        agent: The live member agent the rail was attached to.
        team_workspace: Fully-populated ``TeamWorkspaceInfo`` so hot-reload can
            rebuild the leader rails.
    """
    from jiuwenswarm.agents.harness.team.team_manager import (
        TeamRailMountContext,
        get_team_manager,
    )
    from jiuwenswarm.agents.harness.team.team_runtime_inheritance import (
        MemberInfo,
        RuntimeInfo,
        get_default_model_name,
    )

    team_manager = get_team_manager(channel)
    context = team_manager.get_team_rail_context(session_id)
    if context is not None:
        return
    team_manager.register_team_rail_context(
        session_id,
        TeamRailMountContext(
            agent=agent,
            member_info=MemberInfo(
                agent_name=getattr(agent.card, "name", "team_member"),
                model_name=get_default_model_name(),
                role="leader",
            ),
            runtime=RuntimeInfo(channel=channel),
            team_workspace=team_workspace,
        ),
    )


class SwarmTeamSkillEvolutionRail(TeamSkillEvolutionRail):
    """``TeamSkillEvolutionRail`` that self-registers with the team manager.

    On ``init`` it records itself as a live rail, as the session's team skill
    rail, and registers the workspace-to-global skill sync target, reproducing
    the leader-side registration the legacy customizer performed.
    """

    def bind_swarm_context(
        self,
        *,
        channel: str,
        session_id: str,
        team_ws_root: str | None,
        skills_dir: str,
        team_id: str,
        config: dict[str, Any] | None,
    ) -> None:
        """Capture the build-time handles needed for team-manager registration.

        Args:
            channel: Resolved channel key used to locate the team manager.
            session_id: Active session id owning the rail.
            team_ws_root: Team shared workspace root path.
            skills_dir: The single Skill library.
            team_id: Team name.
            config: The resolved ``config.yaml`` mapping.
        """
        self._swarm_channel = channel
        self._swarm_session_id = session_id
        self._swarm_skills_dir = skills_dir
        self._swarm_team_workspace = _build_team_workspace_info(
            team_ws_root=team_ws_root,
            skills_dir=skills_dir,
            team_id=team_id,
            config=config,
        )

    def init(self, agent: Any) -> None:
        """Attach to *agent* and register with the per-channel team manager.

        Args:
            agent: The live member agent this rail is mounted on.
        """
        super().init(agent)
        try:
            from jiuwenswarm.agents.harness.team.team_manager import get_team_manager

            team_manager = get_team_manager(self._swarm_channel)
            team_manager.register_team_live_rail(self._swarm_session_id, agent, self)
            team_manager.register_team_skill_rail(self._swarm_session_id, self)
            if team_manager.consume_team_evolution_watcher_deferred(self._swarm_session_id):
                from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
                    ensure_team_evolution_watcher,
                )

                ensure_team_evolution_watcher(
                    self._swarm_channel,
                    self._swarm_session_id,
                    source="rail_registered",
                )
            _register_team_rail_context(
                self._swarm_channel,
                self._swarm_session_id,
                agent,
                self._swarm_team_workspace,
            )
            logger.info(
                "[swarm.team_skill_evolution] registered live rail "
                "(session=%s, skills_dir=%s)",
                self._swarm_session_id,
                self._swarm_skills_dir,
            )
        except Exception as exc:
            logger.warning(
                "[swarm.team_skill_evolution] team manager registration failed: %s", exc
            )


class SwarmTeamSkillCreateRail(TeamSkillCreateRail):
    """``TeamSkillCreateRail`` that self-registers with the team manager."""

    def bind_swarm_context(
        self,
        *,
        channel: str,
        session_id: str,
        team_ws_root: str | None,
        skills_dir: str,
        team_id: str,
        config: dict[str, Any] | None,
    ) -> None:
        """Capture the build-time handles needed for team-manager registration.

        Args:
            channel: Resolved channel key used to locate the team manager.
            session_id: Active session id owning the rail.
            team_ws_root: Team shared workspace root path.
            skills_dir: The single Skill library.
            team_id: Team name.
            config: The resolved ``config.yaml`` mapping.
        """
        self._swarm_channel = channel
        self._swarm_session_id = session_id
        self._swarm_team_workspace = _build_team_workspace_info(
            team_ws_root=team_ws_root,
            skills_dir=skills_dir,
            team_id=team_id,
            config=config,
        )

    def init(self, agent: Any) -> None:
        """Attach to *agent* and register with the per-channel team manager.

        Args:
            agent: The live member agent this rail is mounted on.
        """
        super().init(agent)
        try:
            from jiuwenswarm.agents.harness.team.team_manager import get_team_manager

            team_manager = get_team_manager(self._swarm_channel)
            team_manager.register_team_live_rail(self._swarm_session_id, agent, self)
            team_manager.register_team_skill_create_rail(self._swarm_session_id, self)
            _register_team_rail_context(
                self._swarm_channel,
                self._swarm_session_id,
                agent,
                self._swarm_team_workspace,
            )
            logger.info(
                "[swarm.team_skill_create] registered live rail (session=%s)",
                self._swarm_session_id,
            )
        except Exception as exc:
            logger.warning(
                "[swarm.team_skill_create] team manager registration failed: %s", exc
            )


class SwarmMemberSkillEvolutionRail(SkillEvolutionRail):
    """``SkillEvolutionRail`` that self-registers as a teammate evolution rail.

    The teammate evolution rail is not a "live" rail (no approval flow), so it
    only registers itself for hot config updates, matching the legacy
    ``register_team_member_skill_evolution_rail`` call.
    """

    def bind_swarm_context(self, *, channel: str, session_id: str) -> None:
        """Capture the build-time handles needed for team-manager registration.

        Args:
            channel: Resolved channel key used to locate the team manager.
            session_id: Active session id owning the rail.
        """
        self._swarm_channel = channel
        self._swarm_session_id = session_id

    def init(self, agent: Any) -> None:
        """Attach to *agent* and register with the per-channel team manager.

        Args:
            agent: The live member agent this rail is mounted on.
        """
        super().init(agent)
        try:
            from jiuwenswarm.agents.harness.team.team_manager import get_team_manager

            get_team_manager(
                self._swarm_channel
            ).register_team_member_skill_evolution_rail(
                self._swarm_session_id,
                self,
            )
            logger.info(
                "[swarm.member_skill_evolution] registered member evolution rail "
                "(session=%s)",
                self._swarm_session_id,
            )
        except Exception as exc:
            logger.warning(
                "[swarm.member_skill_evolution] team manager registration failed: %s",
                exc,
            )


def _build_evolution_llm_from(model_config: dict[str, Any]) -> tuple[Any, str]:
    """Build the evolution LLM Model from the serializable model-config param.

    Mirrors ``team_runtime_inheritance.build_evolution_llm`` but takes the already
    resolved (serializable) model config baked into ``params`` at spec-build time,
    so the live LLM handle is constructed here at build time (never in the schema).
    """
    from openjiuwen.core.foundation.llm import (
        Model,
        ModelClientConfig,
        ModelRequestConfig,
    )
    from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs

    model_client_config = model_config.get("model_client_config") or {}
    model_config_obj = model_config.get("model_config_obj") or {}
    model_name = model_config.get("model_name") or "gpt-4"
    request_config = ModelRequestConfig(
        **build_reasoning_model_request_kwargs(
            model_client_config=model_client_config,
            model_config_obj=model_config_obj,
            model_name=model_name,
        )
    )
    client_config = ModelClientConfig(**model_client_config)
    return Model(
        model_client_config=client_config, model_config=request_config
    ), model_name


def _build_evolution_approval_stack(
    rail: SkillEvolutionRail,
    *,
    review_runtime: EvolutionReviewRuntime,
    auto_save: bool,
    language: str,
) -> list[Any]:
    """Return the approval interrupt plus evolution rail in registration order."""
    return [
        EvolutionInterruptRail(
            review_runtime=review_runtime,
            submission_service=rail.approval_submission_service,
            auto_save=auto_save,
            language=language,
        ),
        rail,
    ]


harness_element(
    kind=ElementKind.RAIL,
    name=EVOLUTION_INTERRUPT,
    description="Active Skill evolution mutation approval interrupt rail.",
    builder=EvolutionInterruptRail,
)


class TeamSkillEvolutionInput(ConstructionInput):
    """Construction inputs for the leader team skill-evolution rail."""

    evolution_model_config: dict[str, Any] = param_field(
        default_factory=dict,
        description="Serializable evolution model config (LLM built at build time).",
    )
    review_trigger: bool = param_field(
        default=False, description="Evolution review trigger flag."
    )
    auto_save: bool = param_field(
        default=False, description="Evolution auto-save approval flag."
    )
    team_skill_visibility_path: str | None = context_field(
        attr="team_skill_visibility_path",
        description="Team skills-visibility.json path (authoring audience).",
    )
    global_skills_dir: str | None = context_field(
        attr="global_skills_dir", description="The single Skill library."
    )
    language: str = context_field(
        attr="language", default="cn", description="Member language code."
    )
    role: str | None = context_field(attr="role", description="Team role value.")
    team_id: str = context_field(attr="team_id", default="", description="Team name.")
    channel: str = context_field(
        attr="channel", default="default", description="Resolved channel key."
    )
    session_id: str | None = context_field(
        attr="session_id", description="Active session id."
    )
    team_ws_root: str | None = context_field(
        attr="team_ws_root", description="Team shared workspace root."
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_SKILL_EVOLUTION,
    description="Leader-only team skill evolution rail (builds the evolution LLM); "
    "skipped for non-leaders.",
    input_model=TeamSkillEvolutionInput,
)
def build_team_skill_evolution_rail(
    params: dict[str, Any],
    ctx: SwarmBuildContext,
) -> list[Any]:
    """Build the leader-only team skill evolution rail from the config source.

    Mirrors the leader branch of the legacy ``build_member_rails``: builds the
    evolution LLM, resolves auto-scan, and wires the team-manager registration
    via the rail subclass ``init``.

    Args:
        params: Provider params (unused; kept for the provider contract).
        ctx: The per-member build context.

    Returns:
        ``[EvolutionInterruptRail, SwarmTeamSkillEvolutionRail]`` for the leader,
        or ``[]`` when the member is not the leader or no team skills directory
        is configured.
    """
    # Cheap gate before resolving (avoids building the evolution LLM for non-leaders).
    if ctx.role != "leader" or not ctx.global_skills_dir:
        return []

    try:
        inp = TeamSkillEvolutionInput.resolve(params, ctx)
        skills_dir = _skill_library_dir(ctx)
        Path(skills_dir).mkdir(parents=True, exist_ok=True)
        llm_model, actual_model_name = _build_evolution_llm_from(
            inp.evolution_model_config
        )
        review_runtime = EvolutionReviewRuntime()
        rail = SwarmTeamSkillEvolutionRail(
            skills_dir,
            llm=llm_model,
            model=actual_model_name,
            review_runtime=review_runtime,
            language=inp.language,
            trajectory_span_processor=get_trajectory_span_processor(),
            member_role=inp.role,
            auto_save=inp.auto_save,
            review_trigger=inp.review_trigger,
            team_id=inp.team_id,
            disabled_skills=merge_evolution_disabled_skills(
                load_execution_disabled_skills()
            ),
        )
        rail.bind_swarm_context(
            channel=inp.channel,
            session_id=inp.session_id,
            team_ws_root=inp.team_ws_root,
            skills_dir=skills_dir,
            team_id=inp.team_id,
            config=ctx.config,
        )
        logger.info(
            "[swarm.team_skill_evolution] built: skills_dir=%s, model=%s, "
            "review_trigger=%s",
            skills_dir,
            actual_model_name,
            inp.review_trigger,
        )
        return _build_evolution_approval_stack(
            rail,
            review_runtime=review_runtime,
            auto_save=inp.auto_save,
            language=inp.language,
        )
    except Exception as exc:
        logger.warning(
            "[swarm.team_skill_evolution] build failed: %s", exc, exc_info=True
        )
        return []


class TeamSkillCreateInput(ConstructionInput):
    """Construction inputs for the leader team skill-create rail."""

    skill_create: bool = param_field(
        default=False,
        description="Skill-create flag (env SKILL_CREATE overrides config).",
    )
    team_skill_visibility_path: str | None = context_field(
        attr="team_skill_visibility_path",
        description="Team skills-visibility.json path (authoring audience).",
    )
    global_skills_dir: str | None = context_field(
        attr="global_skills_dir", description="The single Skill library."
    )
    language: str = context_field(
        attr="language", default="cn", description="Member language code."
    )
    channel: str = context_field(
        attr="channel", default="default", description="Resolved channel key."
    )
    session_id: str | None = context_field(
        attr="session_id", description="Active session id."
    )
    team_ws_root: str | None = context_field(
        attr="team_ws_root", description="Team shared workspace root."
    )
    team_id: str = context_field(attr="team_id", default="", description="Team name.")


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_SKILL_CREATE,
    description="Leader-only team skill creation rail (gated by SKILL_CREATE and a "
    "configured team skills directory); skipped otherwise.",
    input_model=TeamSkillCreateInput,
)
def build_team_skill_create_rail(
    params: dict[str, Any],
    ctx: SwarmBuildContext,
) -> SwarmTeamSkillCreateRail | None:
    """Build the leader-only team skill creation rail from the config source.

    Mirrors the leader branch of the legacy ``build_member_rails``: gated on
    ``get_skill_create_enabled`` (env ``SKILL_CREATE`` overrides config) and a
    configured team skills directory.

    Args:
        params: Provider params (unused; kept for the provider contract).
        ctx: The per-member build context.

    Returns:
        A ``SwarmTeamSkillCreateRail`` for the leader, or ``None`` when the
        member is not the leader, no team skills directory is configured, or
        skill creation is disabled.
    """
    if ctx.role != "leader" or not ctx.global_skills_dir:
        return None

    inp = TeamSkillCreateInput.resolve(params, ctx)
    skills_dir = _skill_library_dir(ctx)
    if not inp.skill_create:
        return None

    try:
        rail = SwarmTeamSkillCreateRail(
            skills_dir,
            language=inp.language,
            auto_trigger=True,
            trajectory_span_processor=get_trajectory_span_processor(),
        )
        rail.bind_swarm_context(
            channel=inp.channel,
            session_id=inp.session_id,
            team_ws_root=inp.team_ws_root,
            skills_dir=skills_dir,
            team_id=inp.team_id,
            config=ctx.config,
        )
        logger.info(
            "[swarm.team_skill_create] built: skills_dir=%s", skills_dir
        )
        return rail
    except Exception as exc:
        logger.warning("[swarm.team_skill_create] build failed: %s", exc, exc_info=True)
        return None


class MemberSkillEvolutionInput(ConstructionInput):
    """Construction inputs for the teammate member skill-evolution rail."""

    evolution_model_config: dict[str, Any] = param_field(
        default_factory=dict,
        description="Serializable evolution model config (LLM built at build time).",
    )
    signal_trigger: bool = param_field(
        default=True,
        description="Passive signal scan after each invoke.",
    )
    review_trigger: bool = param_field(
        default=False,
        description="Teammates use passive signal scans, not leader review follow-ups.",
    )
    team_skill_visibility_path: str | None = context_field(
        attr="team_skill_visibility_path",
        description="Team skills-visibility.json path (authoring audience).",
    )
    global_skills_dir: str | None = context_field(
        attr="global_skills_dir", description="The single Skill library."
    )
    language: str = context_field(
        attr="language", default="cn", description="Member language code."
    )
    team_id: str = context_field(attr="team_id", default="", description="Team name.")
    channel: str = context_field(
        attr="channel", default="default", description="Resolved channel key."
    )
    session_id: str | None = context_field(
        attr="session_id", description="Active session id."
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=MEMBER_SKILL_EVOLUTION,
    description="Teammate-only member skill evolution rail (passive scan after mount, "
    "auto-save, disabled skills).",
    input_model=MemberSkillEvolutionInput,
)
def build_member_skill_evolution_rail(
    params: dict[str, Any],
    ctx: SwarmBuildContext,
) -> list[Any]:
    """Build the teammate-only member skill evolution rail from the config source.

    Replicates ``build_skill_evolution_rail`` (passive evolution when mounted,
    ``auto_save=True``, disabled skills) but constructs the swarm subclass so
    the rail can self-register with the team manager from its ``init``.

    Args:
        params: Provider params (unused; kept for the provider contract).
        ctx: The per-member build context.

    Returns:
        ``[EvolutionInterruptRail, SwarmMemberSkillEvolutionRail]`` for non-leader
        members, or ``[]`` when the member is the leader or no team skills
        directory is configured.
    """
    # Cheap gate before resolving (avoids building the evolution LLM for the leader).
    if ctx.role == "leader" or not ctx.global_skills_dir:
        return []

    try:
        inp = MemberSkillEvolutionInput.resolve(params, ctx)
        skills_dir = _skill_library_dir(ctx)
        llm_model, actual_model_name = _build_evolution_llm_from(
            inp.evolution_model_config
        )
        review_runtime = EvolutionReviewRuntime()
        rail = SwarmMemberSkillEvolutionRail(
            skills_dir,
            llm=llm_model,
            model=actual_model_name,
            review_runtime=review_runtime,
            language=inp.language,
            auto_save=True,
            signal_trigger=inp.signal_trigger,
            review_trigger=inp.review_trigger,
            trajectory_span_processor=get_trajectory_span_processor(),
            disabled_skills=merge_evolution_disabled_skills(
                load_execution_disabled_skills()
            ),
        )
        rail.bind_swarm_context(channel=inp.channel, session_id=inp.session_id)
        logger.info(
            "[swarm.member_skill_evolution] built: model=%s",
            actual_model_name,
        )
        return _build_evolution_approval_stack(
            rail,
            review_runtime=review_runtime,
            auto_save=True,
            language=inp.language,
        )
    except Exception as exc:
        logger.warning(
            "[swarm.member_skill_evolution] build failed: %s", exc, exc_info=True
        )
        return []


class TeamTTSEInput(ConstructionInput):
    """Construction inputs shared by the leader and member TTSE rails.

    Spec-build bakes harness settings into params so spawn/recover rebuilds do
    not re-read the receiving process ``config.yaml`` or the tenant workspace CV.
    """

    evolution_model_config: dict[str, Any] = param_field(
        default_factory=dict,
        description="Serializable evolution model config (LLM built at build time).",
    )
    evolve_enabled: bool = param_field(
        default=True,
        description="Whether the leader bank may induce FACT/TIP rules.",
    )
    inject_enabled: bool = param_field(
        default=True,
        description="Whether ttse_consult / catalog injection is enabled.",
    )
    dream_enabled: bool = param_field(
        default=True,
        description="Whether the leader bank may run silent dream hygiene.",
    )
    leader_bank_path: str = param_field(
        default="",
        description="Absolute path of the shared leader bank.json.",
    )
    member_bank_path: str = param_field(
        default="",
        description="Absolute path of the shared member bank.json.",
    )
    embedding_config: dict[str, str] = param_field(
        default_factory=dict,
        description="Serializable embedding provider fields (api_key/base_url/model).",
    )
    dream_interval: int = param_field(
        default=50,
        description=(
            "Non-follow-up iterations between dream attempts. "
            "Matches the single-agent default, not TTSEConfig's 20."
        ),
    )
    dream_min_hours: float = param_field(
        default=24.0,
        description="Minimum hours between dream runs.",
    )
    dream_ttl_days: int = param_field(
        default=90,
        description="Age in days after which dream may prune rules.",
    )
    consult_top_k: int = param_field(
        default=8,
        description="Per-track FACT/TIP hit count for ttse_consult.",
    )
    consult_retrieve_mode: str = param_field(
        default="hybrid",
        description="ttse_consult scoring path: hybrid, embed, or bm25.",
    )
    consult_rrf_k: int = param_field(
        default=60,
        description="RRF constant for BM25 and embedding fusion.",
    )
    trajectory_export_enabled: bool = param_field(
        default=False,
        description="Whether to snapshot invoke messages for an offline bench.",
    )
    trajectory_export_path: str = param_field(
        default="",
        description="JSON path used when trajectory export is enabled.",
    )


def _ttse_embedding_provider(emb_cfg: dict[str, str] | None) -> Any:
    """Build the embedding provider from baked params. Empty config means off."""

    if not emb_cfg:
        return None
    api_key = str(emb_cfg.get("api_key") or "").strip()
    base_url = str(emb_cfg.get("base_url") or "").strip()
    model = str(emb_cfg.get("model") or "").strip()
    if not (api_key and base_url and model):
        return None
    from jiuwenswarm.agents.harness.common.memory.embeddings import (
        OpenAICompatibleEmbeddingProvider,
    )

    return OpenAICompatibleEmbeddingProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
    )


def _ttse_config_fields(inp: TeamTTSEInput) -> dict[str, Any]:
    """Knobs both builders pass through so a team rail matches the single-agent rail."""

    return {
        "dream_interval": inp.dream_interval,
        "dream_min_hours": inp.dream_min_hours,
        "dream_ttl_days": inp.dream_ttl_days,
        "consult_top_k": inp.consult_top_k,
        "consult_retrieve_mode": inp.consult_retrieve_mode,
        "consult_rrf_k": inp.consult_rrf_k,
        "trajectory_export_enabled": inp.trajectory_export_enabled,
        "trajectory_export_path": inp.trajectory_export_path,
    }


@harness_element(
    kind=ElementKind.RAIL,
    name=TEAM_TTSE,
    description="Leader TTSE rail: split team spans into leader and member banks.",
    input_model=TeamTTSEInput,
)
def build_team_ttse_rail(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the leader TeamTTSERail. Empty when this member is not the leader."""

    if ctx.role != "leader":
        return []
    try:
        from openjiuwen.agent_evolving.ttse.config import TTSEConfig
        from openjiuwen.harness.rails.evolution.team_ttse_rail import TeamTTSERail

        inp = TeamTTSEInput.resolve(params, ctx)
        if not inp.leader_bank_path or not inp.member_bank_path:
            logger.warning("[swarm.team_ttse] skipped: bank paths missing from params")
            return []
        processor = get_trajectory_span_processor()
        if processor is None:
            logger.warning("[swarm.team_ttse] skipped: TrajectorySpanProcessor unavailable")
            return []
        llm_model, model_name = _build_evolution_llm_from(inp.evolution_model_config)
        embedding = _ttse_embedding_provider(inp.embedding_config)
        rail = TeamTTSERail(
            llm=llm_model,
            model=model_name,
            member_store_path=inp.member_bank_path,
            ttse_config=TTSEConfig(
                store_path=inp.leader_bank_path,
                evolve_enabled=inp.evolve_enabled,
                inject_enabled=inp.inject_enabled,
                dream_enabled=inp.dream_enabled,
                embedding=embedding,
                **_ttse_config_fields(inp),
            ),
            trajectory_span_processor=processor,
        )
        logger.info(
            "[swarm.team_ttse] built: leader_bank=%s member_bank=%s evolve=%s inject=%s dream=%s",
            inp.leader_bank_path,
            inp.member_bank_path,
            inp.evolve_enabled,
            inp.inject_enabled,
            inp.dream_enabled,
        )
        return [rail]
    except Exception as exc:
        logger.warning("[swarm.team_ttse] build failed: %s", exc, exc_info=True)
        return []


@harness_element(
    kind=ElementKind.RAIL,
    name=MEMBER_TTSE,
    description="Member TTSE rail: inject from the shared member bank, never evolve or dream.",
    input_model=TeamTTSEInput,
)
def build_member_ttse_rail(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build an inject-only stock TTSERail for a teammate."""

    if ctx.role == "leader":
        return []
    try:
        from openjiuwen.agent_evolving.ttse.config import TTSEConfig
        from openjiuwen.harness.rails.evolution.ttse_rail import TTSERail

        inp = TeamTTSEInput.resolve(params, ctx)
        if not inp.member_bank_path:
            logger.warning("[swarm.member_ttse] skipped: member bank path missing from params")
            return []
        processor = get_trajectory_span_processor()
        if processor is None:
            logger.warning("[swarm.member_ttse] skipped: TrajectorySpanProcessor unavailable")
            return []
        llm_model, model_name = _build_evolution_llm_from(inp.evolution_model_config)
        embedding = _ttse_embedding_provider(inp.embedding_config)
        rail = TTSERail(
            llm=llm_model,
            model=model_name,
            ttse_config=TTSEConfig(
                store_path=inp.member_bank_path,
                evolve_enabled=False,
                inject_enabled=inp.inject_enabled,
                dream_enabled=False,
                embedding=embedding,
                **_ttse_config_fields(inp),
            ),
            trajectory_span_processor=processor,
            catalog_omit_categories=("team-planning-and-acceptance",),
        )
        logger.info(
            "[swarm.member_ttse] built: member_bank=%s inject=%s evolve=False dream=False",
            inp.member_bank_path,
            inp.inject_enabled,
        )
        return [rail]
    except Exception as exc:
        logger.warning("[swarm.member_ttse] build failed: %s", exc, exc_info=True)
        return []


__all__ = [
    "EVOLUTION_INTERRUPT",
    "TEAM_SKILL_EVOLUTION",
    "TEAM_SKILL_CREATE",
    "MEMBER_SKILL_EVOLUTION",
    "TEAM_TTSE",
    "MEMBER_TTSE",
    "build_team_skill_evolution_rail",
    "build_team_skill_create_rail",
    "build_member_skill_evolution_rail",
    "build_team_ttse_rail",
    "build_member_ttse_rail",
]
