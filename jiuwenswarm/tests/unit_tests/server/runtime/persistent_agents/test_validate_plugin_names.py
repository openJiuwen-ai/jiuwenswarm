from types import SimpleNamespace
from uuid import uuid4

from jiuwenswarm.server.runtime import extension_package_manager as packages


def test_plugin_asset_id_and_package_name_resolve_to_same_plugin(monkeypatch) -> None:
    asset_id = uuid4().hex
    installed = SimpleNamespace(package_id="office-docs")
    store = SimpleNamespace(
        get={asset_id: installed}.get,
        get_by_package_id={"office-docs": installed}.get,
    )
    monkeypatch.setattr(packages, "hub_install_state_store", lambda kind: store)

    for identifier in (asset_id, "office-docs"):
        assert packages.resolve_equipment_runtime_id("plugin_packages", identifier) == "office-docs"
