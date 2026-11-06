"""Keep RSI installation tests out of the user's extension catalog."""

import pytest

try:
    from jiuwenswarm.server.runtime import extension_package_manager as catalog
except ImportError:  # 插件运行时未随本构建迁移；仅依赖该 fixture 的用例跳过
    catalog = None


@pytest.fixture
def rsi_catalog_workspace(monkeypatch, tmp_path):
    if catalog is None:
        pytest.skip("extension_package_manager unavailable in this build")
    workspace = tmp_path / "agent-workspace"
    monkeypatch.setattr(catalog, "get_agent_workspace_dir", lambda: workspace)
    monkeypatch.setattr(catalog, "get_equipment_resources_plugin_packages_dir", lambda: None)
    return workspace
