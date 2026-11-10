# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The OpenCode Go session-header rail mounts for every member profile."""

import pytest

from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs


@pytest.mark.parametrize("mode", ["team", "team.design", "code.team"])
@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_opencode_session_rail_mounts_for_every_member_profile(mode: str, role: str) -> None:
    """The rail self-gates on the vendor, so mounting needs no config gate."""
    rails, _ = build_member_capability_specs({}, mode, role)

    assert registry.OPENCODE_SESSION in {spec.type for spec in rails}
