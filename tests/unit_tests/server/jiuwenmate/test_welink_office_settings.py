from types import SimpleNamespace
import pytest
from jiuwenswarm.server.jiuwenmate.welink_office_settings import WelinkOfficeSettings
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError


def binding():
    return SimpleNamespace(agent_id='im1', account='owner', cli_path='welink-cli', reader_credential_dir='/unused', env_name='pro')


class Cli:
    def __init__(self, *args, **kwargs): self.calls = []
    def auth_status(self): return SimpleNamespace(ok=True, stdout='', stderr='')
    def run(self, args, timeout=30):
        self.calls.append(args)
        return SimpleNamespace(ok=True, stdout='{"success":true}')


def test_connect_is_per_agent_persistent_and_disconnect_revokes(tmp_path, monkeypatch):
    import jiuwenswarm.server.jiuwenmate.welink_office_settings as module
    monkeypatch.setattr(module, 'parse_auth_status', lambda *args: (True, 'owner'))
    store = WelinkOfficeSettings(tmp_path / 'agents.db')
    assert store.status(binding())['status'] == 'not_connected'
    assert store.connect(binding(), 'owner@example.com', cli_factory=Cli)['status'] == 'connected'
    other = WelinkOfficeSettings(tmp_path / 'agents.db')
    assert other.status(binding())['email'] == 'owner@example.com'
    changed = binding(); changed.account = 'different'
    assert other.status(changed)['status'] == 'not_connected'
    assert other.disconnect(binding())['status'] == 'not_connected'
    assert store.status(binding())['status'] == 'not_connected'


def test_failed_auth_never_enables(tmp_path, monkeypatch):
    import jiuwenswarm.server.jiuwenmate.welink_office_settings as module
    monkeypatch.setattr(module, 'parse_auth_status', lambda *args: (True, 'someone-else'))
    store = WelinkOfficeSettings(tmp_path / 'agents.db')
    with pytest.raises(PersistentAgentError): store.connect(binding(), 'owner@example.com', cli_factory=Cli)
    assert store.status(binding())['status'] == 'not_connected'


def test_discovery_failure_never_enables(tmp_path, monkeypatch):
    import jiuwenswarm.server.jiuwenmate.welink_office_settings as module
    monkeypatch.setattr(module, 'parse_auth_status', lambda *args: (True, 'owner'))
    class Rejected(Cli):
        def run(self, args, timeout=30): return SimpleNamespace(ok=False, stdout='{}')
    store = WelinkOfficeSettings(tmp_path / 'agents.db')
    with pytest.raises(PersistentAgentError): store.connect(binding(), 'owner@example.com', cli_factory=Rejected)
    assert store.status(binding())['status'] == 'not_connected'
