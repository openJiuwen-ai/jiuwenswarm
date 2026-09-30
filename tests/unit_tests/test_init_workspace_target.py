"""Regression tests for init target selection; no workspace is written."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import jiuwenswarm.init_workspace as cli
import jiuwenswarm.common.utils as utils


@pytest.fixture
def target_env(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, '_workspace_base_dir', None)
    monkeypatch.delenv('JIUWENSWARM_DATA_DIR', raising=False)
    monkeypatch.setattr(utils, 'get_user_home', lambda: tmp_path)
    # Older CLI versions import this directly; keep its fallback isolated too.
    if hasattr(cli, 'get_user_home'):
        monkeypatch.setattr(cli, 'get_user_home', lambda: tmp_path)
    init = Mock(return_value='cancelled')
    monkeypatch.setattr(cli, 'init_user_workspace', init)
    return init


def test_default_target(monkeypatch, tmp_path, target_env):
    assert cli.run_init() == 1
    target_env.assert_called_once_with(overwrite=False, workspace_dir=tmp_path / '.jiuwenswarm')


@pytest.mark.parametrize('force', [False, True])
def test_environment_target(monkeypatch, tmp_path, target_env, force):
    custom = tmp_path / 'isolated-research'
    monkeypatch.setenv('JIUWENSWARM_DATA_DIR', str(custom))
    monkeypatch.setattr(cli, 'get_default_instance_status', lambda: SimpleNamespace(running=False))
    assert cli.run_init(force=force) == 1
    target_env.assert_called_once_with(overwrite=force, workspace_dir=custom)


def test_cached_target(monkeypatch, tmp_path, target_env):
    custom = tmp_path / 'configured'
    monkeypatch.setattr(utils, '_workspace_base_dir', custom)
    assert cli.run_init() == 1
    target_env.assert_called_once_with(overwrite=False, workspace_dir=custom)


def test_named_target_wins(monkeypatch, tmp_path, target_env):
    named = tmp_path / 'alice'
    monkeypatch.setenv('JIUWENSWARM_DATA_DIR', str(tmp_path / 'other'))
    monkeypatch.setattr(cli, 'validate_instance_name', lambda name: None)
    monkeypatch.setattr(cli, 'get_instance_workspace_path', lambda name: named)
    monkeypatch.setattr(cli, 'get_instance_config', lambda name: SimpleNamespace())
    monkeypatch.setattr(cli, 'get_instance_status', lambda config: SimpleNamespace(running=False))
    assert cli.run_init(name='alice') == 1
    target_env.assert_called_once_with(overwrite=False, workspace_dir=named)
