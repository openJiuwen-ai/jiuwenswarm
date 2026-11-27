# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team member assembly for OfficeAceUserProfileRail (B_11).

Keeps imports narrow so the suite does not depend on interface_deep / RSI.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.swarm import SwarmBuildContext, register_swarm_providers
from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import (
    _CODE_RAIL_NAMES,
    _COMMON_RAIL_NAMES,
    build_member_capability_specs,
)
from jiuwenswarm.agents.swarm.providers import member_rails


def test_office_ace_user_profile_in_common_and_code_rail_names() -> None:
    assert registry.OFFICE_ACE_USER_PROFILE in _COMMON_RAIL_NAMES
    assert registry.OFFICE_ACE_USER_PROFILE in _CODE_RAIL_NAMES
    assert registry.OFFICE_ACE_USER_PROFILE == "swarm.office_ace_user_profile"


@pytest.mark.parametrize(
    ("mode", "role"),
    [
        ("team", "leader"),
        ("team", "teammate"),
        ("code.team", "leader"),
        ("code.team", "teammate"),
        ("team.plan", "leader"),
    ],
)
def test_office_ace_user_profile_declared_for_team_profiles(
    mode: str, role: str
) -> None:
    rails_specs, _ = build_member_capability_specs({}, mode, role)
    rail_names = {spec.type for spec in rails_specs}
    assert registry.OFFICE_ACE_USER_PROFILE in rail_names


def test_office_ace_user_profile_provider_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_swarm_providers()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.external_memory_config"
        ".get_office_ace_user_profile_config",
        lambda _config=None: {
            "enabled": False,
            "endpoint": "",
            "api_key": "",
            "user_id": "",
            "fetch_interval_minutes": 10,
            "max_chars": 8000,
            "timeout_seconds": 15.0,
            "space_id": "",
        },
    )
    rail = member_rails._build_office_ace_user_profile_rail({}, SwarmBuildContext())
    assert rail is None


def test_office_ace_user_profile_provider_builds_rail_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_swarm_providers()
    from jiuwenswarm.agents.harness.common.rails.office_ace_user_profile import (
        OfficeAceUserProfileRail,
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.external_memory_config"
        ".get_office_ace_user_profile_config",
        lambda _config=None: {
            "enabled": True,
            "endpoint": "https://example.test",
            "api_key": "test-key",
            "user_id": "user-1",
            "fetch_interval_minutes": 10,
            "max_chars": 8000,
            "timeout_seconds": 15.0,
            "space_id": "space-1",
        },
    )
    rail = member_rails._build_office_ace_user_profile_rail({}, SwarmBuildContext())
    assert isinstance(rail, OfficeAceUserProfileRail)
