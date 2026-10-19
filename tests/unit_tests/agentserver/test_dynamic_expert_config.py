"""动态专家的配置门禁与代码默认值。"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common.config import is_dynamic_experts_enabled
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, True),
        ({}, True),
        ({"symphony": None}, True),
        ({"symphony": {}}, True),
        ({"symphony": {"dynamic_experts": None}}, True),
        ({"symphony": {"dynamic_experts": {}}}, True),
        ({"symphony": {"dynamic_experts": {"enabled": None}}}, True),
        ({"symphony": "true"}, False),
        ({"symphony": {"dynamic_experts": True}}, False),
        ({"symphony": {"dynamic_experts": {"enabled": False}}}, False),
        ({"symphony": {"dynamic_experts": {"enabled": "true"}}}, False),
        ({"symphony": {"dynamic_experts": {"enabled": 1}}}, False),
        ({"symphony": {"dynamic_experts": {"enabled": True}}}, True),
    ],
)
def test_dynamic_experts_uses_code_default_unless_explicitly_set(config: Any, expected: bool) -> None:
    assert is_dynamic_experts_enabled(config) is expected


@pytest.mark.parametrize(
    ("enabled", "desktop", "mode", "expected"),
    [
        (False, True, "agent", False),
        (True, False, "agent", False),
        (True, True, "code", False),
        (True, True, "agent", True),
    ],
)
def test_dynamic_expert_rail_only_exists_for_enabled_desktop_agent(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    desktop: bool,
    mode: str,
    expected: bool,
) -> None:
    class RailBuildStub:
        _dynamic_experts_enabled = enabled
        _config_cache: dict[str, Any] = {}

        def __getattr__(self, _name: str) -> Any:
            return lambda *args, **kwargs: False

        def _instantiate_rails(self, infos: list[Any], _config: dict[str, Any]) -> list[str]:
            return [info.attr_name for info in infos]

    if desktop:
        monkeypatch.setenv("JIUWENSWARM_DESKTOP", "1")
    else:
        monkeypatch.delenv("JIUWENSWARM_DESKTOP", raising=False)
    names = JiuWenSwarmDeepAdapter._build_agent_rails(
        RailBuildStub(), {}, {}, mode=mode
    )
    assert ("_dynamic_expert_rail" in names) is expected
