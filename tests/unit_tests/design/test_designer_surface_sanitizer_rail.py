from __future__ import annotations

from openjiuwen.core.foundation.tool import ToolCard

from jiuwenswarm.agents.harness.design.rails.designer_surface_sanitizer_rail import (
    DesignerSurfaceSanitizerRail,
)


class _FakeAbilityManager:
    def __init__(self, names: list[str]) -> None:
        self._cards = {name: ToolCard(name=name, description=name) for name in names}

    def list(self) -> list[ToolCard]:
        return list(self._cards.values())

    def remove_ability(self, name: str) -> None:
        del self._cards[name]

    def names(self) -> set[str]:
        return set(self._cards)


class _FakeAgent:
    def __init__(self, ability_manager: _FakeAbilityManager) -> None:
        self.ability_manager = ability_manager
        self.system_prompt_builder = None


def test_sanitizer_keeps_web_tools_and_drops_team_tools() -> None:
    ability_manager = _FakeAbilityManager(
        ["free_search", "fetch_webpage", "paid_search", "designer_graph_get", "send_message", "workspace_meta"]
    )

    DesignerSurfaceSanitizerRail().init(_FakeAgent(ability_manager))

    assert ability_manager.names() == {"free_search", "fetch_webpage", "paid_search", "designer_graph_get"}
