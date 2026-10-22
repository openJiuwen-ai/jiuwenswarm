# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""team.design worker backend wiring — the TeamWorkerBackend integration.

User ruling (2026-10-14, SDD-0030 adaptation): agent-core provides the
expert-table mechanism (``derive_swarmflow_expert_specs`` — ``agents[m]``
joined with ``predefined_members[m]`` once at assembly, member name →
``SwarmflowExpertEntry``); this module is the design-side derivation of that
table. The graph compiler emits bare member names
(``agent(options={'agent_template': 'clip'})``) and the engine resolves them
against the bound AgentGroup (fail-closed: unknown member / external runtime /
``agent_type`` clash raise before any resource is touched).

The design team assembles WITHOUT a predefined roster
(``_apply_design_agent_group`` clears ``predefined_members`` — members only
exist as worker templates), so the expert table cannot ride the configurator
chain the regular team path uses. Instead the bridge derives it here from the
same enriched spec, synthesizing the roster data the join needs without ever
writing it back to the live spec (writing it back would spawn teammates).

Persona / tools / per-role tool surfaces live in the designer AgentGroup
package; the claw repo only compiles graph scripts and reads events.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_DESIGNER_GROUP = "designer"


def derive_design_expert_specs(team_spec: Any) -> dict[str, Any]:
    """Derive the swarmflow expert table for the design team's enriched spec.

    The design spec carries the designer group's member templates in
    ``agents[m].agent_template_spec`` (capability snapshots written by
    ``_apply_design_agent_group``) but no ``predefined_members`` roster. The
    agent-core join (``derive_swarmflow_expert_specs``) needs both halves, so
    this synthesizes the roster half from the SAME bound group package on
    disk — persona (flattened prompt sections) + display name + external
    runtime config — and joins it against the spec's snapshots on a THROWAWAY
    spec copy (never the live one: writing members back would spawn them).

    Observability rails match the configurator's worker derivation exactly
    (``TEAM_OBSERVABILITY`` + ``OBSERVABILITY``).
    """
    from openjiuwen.agent_teams.rails.elements import TEAM_OBSERVABILITY
    from openjiuwen.agent_teams.schema.deep_agent_spec import RailSpec
    from openjiuwen.harness.manifest.builtin_elements import OBSERVABILITY

    try:
        from openjiuwen.agent_teams.workflow.backends.expert_specs import (
            derive_swarmflow_expert_specs,
        )
    except ImportError:
        # agent-core builds without the swarmflow expert-worker feature: no
        # expert table, every node runs as a plain teammate-base worker.
        logger.warning(
            "[design.team_worker] agent-core lacks swarmflow expert specs; "
            "designer members run as plain workers"
        )
        return {}

    members = _design_member_roster(team_spec)
    scratch = team_spec.model_copy(update={"predefined_members": members})
    observability_rail_specs = [
        RailSpec(type=TEAM_OBSERVABILITY),
        RailSpec(type=OBSERVABILITY),
    ]
    return derive_swarmflow_expert_specs(scratch, observability_rail_specs)


def _design_member_roster(team_spec: Any) -> list[Any]:
    """Synthesize the predefined-member roster from the bound designer group.

    Reads the SAME package the assembly step read (``agents/<m>/`` member
    templates of the bound group): persona flattened per language, display
    name resolved, external-runtime members typed as ``ExternalCliMemberSpec``
    so the expert table lists them with the precise "not run by swarmflow yet"
    error instead of a silent native run. The teammate/leader entries of the
    spec never enter this roster (they carry no template snapshot and would be
    skipped by the join anyway, but excluding them here keeps the roster
    exactly the worker cast).
    """
    from openjiuwen.agent_teams.experts import load_agent_group_package, render_template_persona
    from jiuwenswarm.server.runtime.extension_package_manager import (
        resolve_agent_group_dir,
        resolve_agent_group_member_display_name,
    )
    from openjiuwen.agent_teams.external import external_cli_agent_spec_from_template
    from openjiuwen.agent_teams.schema.team import (
        ExternalCliMemberSpec,
        TeamMemberSpec,
        TeamRole,
    )

    package_dir = resolve_agent_group_dir(_DESIGNER_GROUP)
    templates = load_agent_group_package(package_dir)
    teammate_base = team_spec.agents.get("teammate")

    roster: list[Any] = []
    for member_name, template in templates.items():
        if member_name in ("leader", "teammate"):
            continue
        member_prompt = (
            render_template_persona(
                template,
                language=teammate_base.language,
                workspace=teammate_base.workspace.root_path if teammate_base.workspace is not None else None,
            )
            if teammate_base is not None
            else ""
        )
        display_name = resolve_agent_group_member_display_name(
            package_dir,
            member_name,
            fallback=template.agent_card.name or member_name,
        )
        external_config = external_cli_agent_spec_from_template(template)
        if external_config is None:
            roster.append(
                TeamMemberSpec(
                    member_name=member_name,
                    display_name=display_name,
                    desc=template.agent_card.description or "",
                    prompt=member_prompt,
                    role_type=TeamRole.TEAMMATE,
                )
            )
        else:
            roster.append(
                ExternalCliMemberSpec(
                    member_name=member_name,
                    display_name=display_name,
                    desc=template.agent_card.description or "",
                    prompt=member_prompt,
                    external_cli=external_config,
                )
            )
    return roster


def build_worker_backend(
    *,
    team_name: str,
    worker_base_spec: Any,
    build_context: Any,
    run_id: str,
    session_id: str | None = None,
    expert_specs: dict[str, Any] | None = None,
) -> Any:
    """Construct the agent-core TeamWorkerBackend for one design trigger.

    ``expert_specs`` comes from :func:`derive_design_expert_specs` (derived
    once per design session by the bridge, cached on the design context).
    Empty / None when no group is bound — scripts that never name an
    ``agent_template`` are unaffected (plain teammate-base workers).
    """
    from openjiuwen.agent_teams.workflow.backends.team_worker_backend import (
        TeamWorkerBackend,
    )

    # Only forwarded when non-empty: agent-core builds without the expert-worker
    # feature reject the keyword, and an empty table is the backend default.
    expert_kwargs: dict[str, Any] = {"expert_specs": expert_specs} if expert_specs else {}
    return TeamWorkerBackend(
        model=None,
        team_name=team_name,
        worker_base_spec=worker_base_spec,
        build_context=build_context,
        run_id=run_id,
        session_id=session_id,
        **expert_kwargs,
    )


__all__ = ["build_worker_backend", "derive_design_expert_specs"]
