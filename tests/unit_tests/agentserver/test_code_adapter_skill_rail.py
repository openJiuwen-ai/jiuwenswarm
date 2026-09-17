# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for code-mode SkillUseRail attribute alignment.

``JiuwenSwarmCodeAdapter`` builds its SkillUseRail via the dynamic-rails path
(``_build_skill_rail_via_config`` → ``_build_skill_rail``), which
``_instantiate_rails`` stores under ``_dynamic_SkillUseRail``. The parent
``refresh_skill_rails`` / ``_get_current_agent_rails`` only read
``self._skill_rail``, so the dynamic rail must ALSO be pinned to ``_skill_rail``
or session-level MCP reconcile (chat.send ``mcp`` field) cannot refresh a
cli/skill MCP's bundled skills in code mode.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import openjiuwen.agent_evolving.trajectory as _traj_mod
if not hasattr(_traj_mod, "InMemoryTrajectoryRegistry"):
    _traj_mod.InMemoryTrajectoryRegistry = MagicMock

from jiuwenswarm.server.runtime.agent_adapter.interface_code import (  # noqa: E402
    JiuwenSwarmCodeAdapter,
)


def test_build_skill_rail_via_config_pins_skill_rail_attribute() -> None:
    """The dynamic SkillUseRail must be reachable via ``_skill_rail``."""
    adapter = JiuwenSwarmCodeAdapter()
    fake_rail = MagicMock()

    # Pin the two collaborators so the method runs without a full agent build.
    adapter._is_acp_tool_profile = MagicMock(return_value=False)
    adapter._build_skill_rail = MagicMock(return_value=fake_rail)

    result = adapter._build_skill_rail_via_config()

    assert result is fake_rail
    # refresh_skill_rails / _get_current_agent_rails read this attribute.
    assert adapter._skill_rail is fake_rail


def test_build_skill_rail_via_config_none_does_not_pin() -> None:
    """A failed build (None) must not overwrite a prior ``_skill_rail``."""
    adapter = JiuwenSwarmCodeAdapter()
    existing = MagicMock()
    adapter._skill_rail = existing

    adapter._is_acp_tool_profile = MagicMock(return_value=False)
    adapter._build_skill_rail = MagicMock(return_value=None)

    result = adapter._build_skill_rail_via_config()

    assert result is None
    assert adapter._skill_rail is existing
