"""Fresh managed workspaces copy a prepared template without backend migration."""
import pytest


def test_agentos_initialization_selects_the_prepared_template(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils
    template = tmp_path / 'prepared.yaml'
    template.write_text('sandbox: {type: adx-environment, runtime: conch, image: prepared-conch}\n')
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE', str(template))
    assert utils._find_config_template_path() == template


def test_missing_prepared_template_fails_instead_of_using_a_legacy_template(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE', str(tmp_path / 'missing.yaml'))
    with pytest.raises(RuntimeError, match='template is unavailable'):
        utils._find_config_template_path()


def test_personal_initialization_keeps_the_package_template(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils
    packaged = tmp_path / 'resources/config.yaml'
    packaged.parent.mkdir()
    packaged.write_text('sandbox: {type: jiuwenbox}\n')
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', '')
    monkeypatch.setenv('JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE', '/irrelevant/prepared.yaml')
    monkeypatch.setattr(utils, '_find_package_root', lambda: tmp_path)
    assert utils._find_config_template_path() == packaged


def test_fresh_workspace_copies_prepared_yaml_and_dotenv(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils
    template = tmp_path / 'prepared.yaml'
    template.write_text('sandbox: {type: adx-environment, runtime: conch, image: prepared-conch}\n')
    template.chmod(0o600)
    template.with_suffix('.env').write_text('MODEL_NAME=prepared-model\n')
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE', str(template))
    workspace = tmp_path / 'new-user'
    utils.prepare_workspace(overwrite=False, preferred_language='zh', workspace_dir=workspace)
    assert 'prepared-conch' in (workspace / 'config/config.yaml').read_text()
    assert (workspace / 'config/.env').read_text() == 'MODEL_NAME=prepared-model\n'
    assert (workspace / 'config/config.yaml').stat().st_mode & 0o777 == 0o600


def test_early_paths_use_writable_user_config_instead_of_package_resources(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils
    template = tmp_path / 'prepared.yaml'
    template.write_text('sandbox: {type: adx-environment, runtime: runsc}\n')
    template.with_suffix('.env').write_text('MODEL_NAME=prepared\n')
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    monkeypatch.setenv('JIUWENSWARM_AGENTOS_CONFIG_TEMPLATE', str(template))
    workspace = tmp_path / 'empty-user'
    monkeypatch.setattr(utils, '_workspace_base_dir', workspace)
    for name, value in (('_initialized', False), ('_config_dir', None), ('_root_dir', None), ('_workspace_dir', None)):
        monkeypatch.setattr(utils, name, value)
    assert utils.get_config_file() == workspace / 'config/config.yaml'
    assert utils.get_config_file().read_text() == template.read_text()
    assert (workspace / 'config/.env').read_text() == 'MODEL_NAME=prepared\n'
    assert utils.get_config_file().stat().st_mode & 0o777 == 0o600
    utils.get_config_file().write_text('personal: keep\n')
    monkeypatch.setattr(utils, '_initialized', False)
    assert utils.get_config_file().read_text() == 'personal: keep\n'


@pytest.mark.parametrize('mode', [0o600, 0o640])
def test_agentos_config_updates_keep_the_prepared_mode(tmp_path, monkeypatch, mode):
    from jiuwenswarm.common.config import dump_yaml_round_trip
    monkeypatch.setenv('JIUWENSWARM_RUNTIME_PROFILE', 'agentos')
    path = tmp_path / 'config.yaml'
    path.write_text('models: {}\n')
    path.chmod(mode)
    dump_yaml_round_trip(path, {'models': {'defaults': []}})
    assert path.stat().st_mode & 0o777 == mode
