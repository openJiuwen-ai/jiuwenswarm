from types import SimpleNamespace
from jiuwenswarm.server.runtime.mcp import registry


def test_connector_search_names_preserve_only_declared_display_names(monkeypatch, tmp_path):
    package = SimpleNamespace(
        name='hidden-6',
        description='',
        integration_type='mcp',
        skill_dirs=(),
        icon_file=None,
        root=tmp_path,
        raw={'display_name': {'zh': '回归连接器', 'en': 'Regression Connector'}},
    )
    monkeypatch.setattr(registry, '_resolve_package', lambda name: package)
    meta = registry._load_index_entry('hidden-6')
    assert meta['display_name_i18n'] == package.raw['display_name']
    card = registry._build_summary('hidden-6', package, meta, set())
    assert card['display_name_i18n'] == package.raw['display_name']
    package.raw['display_name'] = {'zh': '回归连接器'}
    meta = registry._load_index_entry('hidden-6')
    assert meta['display_name_i18n'] == {'zh': '回归连接器'}
