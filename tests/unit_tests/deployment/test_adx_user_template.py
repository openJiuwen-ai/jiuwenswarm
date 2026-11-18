"""Runtime reads application configuration prepared before sandbox creation."""
import pytest


def test_personal_initialization_keeps_the_package_template(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils
    packaged = tmp_path / 'resources/config.yaml'
    packaged.parent.mkdir()
    packaged.write_text('sandbox: {type: jiuwenbox}\n')
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', '')
    monkeypatch.setenv('JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE', '/irrelevant/prepared.yaml')
    monkeypatch.setattr(utils, '_find_package_root', lambda: tmp_path)
    assert utils._find_config_template_path() == packaged


def test_paths_read_configuration_prepared_by_application_initialization(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils
    workspace = tmp_path / 'initialized-user'
    config = workspace / 'config'
    config.mkdir(parents=True)
    (config / 'config.yaml').write_text('sandbox: {type: adx-environment, runtime: runsc}\n')
    (config / 'config.yaml').chmod(0o600)
    (config / '.env').write_text('MODEL_NAME=prepared\n')
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE', str(tmp_path / 'no-runtime-template-needed'))
    monkeypatch.setattr(utils, '_workspace_base_dir', workspace)
    for name in ('_config_dir', '_root_dir', '_workspace_dir'):
        monkeypatch.setattr(utils, name, None)
    monkeypatch.setattr(utils, '_initialized', False)
    assert utils.get_config_file() == config / 'config.yaml'
    assert utils.get_config_file().stat().st_mode & 0o777 == 0o600
    assert (config / '.env').read_text() == 'MODEL_NAME=prepared\n'
    assert not hasattr(utils, '_seed_managed_workspace_config')


def test_path_lookup_does_not_copy_a_managed_template(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils
    package = tmp_path / 'package'
    (package / 'resources').mkdir(parents=True)
    (package / 'resources/config.yaml').write_text('sandbox: {type: local}\n')
    prepared = tmp_path / 'prepared.yaml'
    prepared.write_text('sandbox: {type: adx-environment}\n')
    prepared.with_suffix('.env').write_text('MODEL_KEY=prepared\n')
    workspace = tmp_path / 'uninitialized'
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE', str(prepared))
    monkeypatch.setattr(utils, '_find_package_root', lambda: package)
    monkeypatch.setattr(utils, '_workspace_base_dir', workspace)
    monkeypatch.setattr(utils, '_initialized', False)
    utils.get_config_file()
    assert not (workspace / 'config/config.yaml').exists()
    assert not (workspace / 'config/.env').exists()


@pytest.mark.parametrize('mode', [0o600, 0o640])
def test_agentos_config_updates_keep_the_prepared_mode(tmp_path, monkeypatch, mode):
    from jiuwenswarm.common.config import dump_yaml_round_trip
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    path = tmp_path / 'config.yaml'
    path.write_text('models: {}\n')
    path.chmod(mode)
    dump_yaml_round_trip(path, {'models': {'defaults': []}})
    assert path.stat().st_mode & 0o777 == mode


def test_agentos_template_lookup_uses_package_defaults_without_deployment_copying(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils
    package = tmp_path / 'package'
    (package / 'resources').mkdir(parents=True)
    (package / 'resources/config.yaml').write_text('sandbox: {type: local}\n')
    monkeypatch.setattr(utils, '_find_package_root', lambda: package)
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE', str(tmp_path / 'obsolete.yaml'))
    assert utils._find_config_template_path() == package / 'resources/config.yaml'


def test_managed_workspace_admission_requires_initialization(tmp_path):
    from agent_gateway.extensions.adx.workspace import check_workspace_config
    config = tmp_path / 'alice/config.yaml'
    with pytest.raises(ValueError, match='application workspace initialization required'):
        check_workspace_config(config, require_prepared=True)
    config.parent.mkdir()
    config.write_text('sandbox: {type: adx-environment}\n')
    check_workspace_config(config, require_prepared=True)
