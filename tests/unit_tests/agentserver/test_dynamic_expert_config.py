"""动态专家的配置门禁与代码默认值。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from jiuwenswarm.common.config import is_dynamic_experts_enabled
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def test_packaged_dynamic_default_is_independent_from_skill_symphony():
    config_path = Path(__file__).resolve().parents[3] / "jiuwenswarm/resources/config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["symphony"]["enabled"] is False
    assert config["symphony"]["dynamic_experts"]["enabled"] is None
    assert is_dynamic_experts_enabled(config) is True


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (None, True),
        ({}, True),
        ({"symphony": {"dynamic_experts": {"enabled": None}}}, True),
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
