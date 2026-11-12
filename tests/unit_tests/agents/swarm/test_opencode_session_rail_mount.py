# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The OpenCode Go session-header rail mounts for every member profile."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import (
    build_member_capability_specs,
    build_reviewer_deep_agent_spec,
)
from jiuwenswarm.agents.swarm.providers.member_rails import (
    _build_opencode_session_rail,
)


@pytest.mark.parametrize("mode", ["team", "team.design", "code.team"])
@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_opencode_session_rail_mounts_for_every_member_profile(mode: str, role: str) -> None:
    """The rail self-gates on the vendor, so mounting needs no config gate."""
    rails, _ = build_member_capability_specs({}, mode, role)

    assert registry.OPENCODE_SESSION in {spec.type for spec in rails}


@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan.code"])
def test_reviewer_spec_keeps_the_opencode_session_rail(mode: str) -> None:
    """A reviewer calls a model too; dropping the rail would leave it headerless."""
    from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec
    from openjiuwen.harness.schema.deep_agent_spec import DeepAgentSpec

    teammate = build_member_deep_agent_spec({}, mode, "teammate", DeepAgentSpec())
    reviewer = build_reviewer_deep_agent_spec(mode, teammate)

    assert registry.OPENCODE_SESSION in {rail.type for rail in reviewer.rails or []}


@pytest.mark.parametrize(
    ("session_id", "member_name"),
    [("", "teammate"), ("session-1", ""), ("", "")],
)
def test_rail_is_not_built_without_a_member_scope(session_id: str, member_name: str) -> None:
    """No session or no member identity means one shared scope: do not mount.

    ``OpenCodeSessionRail`` derives ``member:<session>:<identity>``; with an
    empty half every such member of every session would send the same header.
    """
    context = SimpleNamespace(
        session_id=session_id,
        member_name=member_name,
        member_card_id=None,
    )

    assert _build_opencode_session_rail({}, context) is None


def test_rail_is_built_with_both_halves_of_the_scope() -> None:
    from jiuwenswarm.agents.harness.team.rails.opencode_session_rail import (
        OpenCodeSessionRail,
    )

    context = SimpleNamespace(
        session_id="session-1",
        member_name="teammate",
        member_card_id="card-1",
    )

    rail = _build_opencode_session_rail({}, context)

    assert isinstance(rail, OpenCodeSessionRail)
    assert rail.session_id == "session-1"
    assert rail.member_identity == "card-1"
