"""Gateway retains explicit user tool configuration."""
from test_adx_router import _config, _router


def test_adx_router_preserves_explicit_tool_configuration(monkeypatch):
    router, _ = _router(_config(monkeypatch))
    router.set_or_update_server_config(config={
        'sandbox': {'enabled': True, 'launcher_config': {'workspace_mounts': True, 'shared_scope': 'default'},
        'type': 'adx-environment', 'url': 'https://adx.example'},
    })
    assert router._server_config['sandbox']['enabled'] is True
    assert router._server_config['sandbox']['type'] == 'adx-environment'
