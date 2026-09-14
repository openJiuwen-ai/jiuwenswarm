"""Keep RSI installation tests out of the user's extension catalog."""

import pytest

from jiuwenswarm.server.runtime import extension_package_manager as catalog


@pytest.fixture
def rsi_catalog_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "agent-workspace"
    monkeypatch.setattr(catalog, "get_agent_workspace_dir", lambda: workspace)
    monkeypatch.setattr(catalog, "get_equipment_resources_plugin_packages_dir", lambda: None)
    return workspace
