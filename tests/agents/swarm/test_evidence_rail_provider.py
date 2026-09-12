# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Declarative provider integration for EvidenceRail."""

from pathlib import Path

from openjiuwen.agent_teams.schema.deep_agent_spec import RailSpec
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenswarm.agents.harness.common.rails.evidence_rail import EvidenceRail
from jiuwenswarm.agents.swarm import register_swarm_providers, registry
from jiuwenswarm.agents.swarm.context import SwarmBuildContext


def test_railspec_builds_fresh_evidence_rail_below_member_workspace(
    tmp_path: Path,
) -> None:
    register_swarm_providers()
    workspace = Workspace(root_path=tmp_path / "member")
    context = SwarmBuildContext(language="en", workspace=workspace)
    spec = RailSpec(
        type=registry.EVIDENCE_RAIL,
        params={
            "artifact_root": "receipts",
            "evidence_items": [
                {
                    "evidence_id": "ev-1",
                    "source": "doi:10.0000/example",
                    "content": "verified",
                }
            ],
            "max_context_items": 8,
        },
    )

    first = spec.build(language="en", context=context)
    second = spec.build(language="en", context=context)

    assert isinstance(first, EvidenceRail)
    assert isinstance(second, EvidenceRail)
    assert first is not second
    assert first.config.max_context_items == 8
    assert first.config.evidence_items[0].evidence_id == "ev-1"
    assert first.store.artifact_root == (tmp_path / "member" / "receipts").resolve()


def test_existing_provider_remains_available_without_evidence_rail_params() -> None:
    """The new provider is additive; an existing RailSpec still builds unchanged."""
    register_swarm_providers()
    context = SwarmBuildContext(language="cn", channel="web")

    rail = RailSpec(type=registry.RESPONSE_PROMPT).build(
        language="cn",
        context=context,
    )

    assert type(rail).__name__ == "ResponsePromptRail"
