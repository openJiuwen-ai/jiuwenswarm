from jiuwenswarm.server.runtime.persistent_agents.capabilities import validate_plugin_names


def test_validate_plugin_names_resolves_hub_asset_id(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.persistent_agents.capabilities.extension_package_manager.list_plugin_packages",
        lambda _params: [{"id": "office-docs"}],
    )

    def resolve(_kind: str, identifier: str) -> str:
        if identifier == "440c3c6e0e654b1eb9ec32c17bcd2923":
            return "office-docs"
        return identifier

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.persistent_agents.capabilities.extension_package_manager.resolve_equipment_runtime_id",
        resolve,
    )

    assert validate_plugin_names(
        ["440c3c6e0e654b1eb9ec32c17bcd2923", "office-docs"]
    ) == ["office-docs"]
