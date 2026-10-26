"""Flash 环境占位符经真实 YAML/provider 加载后统一为布尔配置。"""

from copy import deepcopy

import pytest
import yaml

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common import config_provider as providers
from jiuwenswarm.common import local_env_config as env_config


FLASH_ENV = "JIUWENSWARM_FLASH_ENABLED"
SKILL_ENV = "JIUWENSWARM_FLASH_SKILL_SELECTION_ENABLED"


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_module, "get_config_file", lambda: path)
    monkeypatch.delenv(FLASH_ENV, raising=False)
    monkeypatch.delenv(SKILL_ENV, raising=False)
    providers.reset_config_provider()
    config_module.clear_config_cache()
    yield path
    config_module.clear_config_cache()
    providers.reset_config_provider()


def switches(config):
    flash = config.get("flash", {})
    return flash.get("enabled"), flash.get("skill_selection", {}).get("enabled")


def test_shipped_placeholders_resolve_to_false_without_modifying_raw_config(config_file):
    raw = config_module.get_config_raw()
    assert switches(raw) == (f"${{{FLASH_ENV}:-false}}", f"${{{SKILL_ENV}:-false}}")
    resolved = config_module.get_config()
    assert all(value is False for value in switches(resolved))
    assert config_module.get_config_raw() == raw
    assert not config_file.exists()


@pytest.mark.parametrize("name", [FLASH_ENV, SKILL_ENV])
@pytest.mark.parametrize("raw,expected", [
    ("true", True), (" TRUE ", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), (" FALSE ", False), ("0", False), ("no", False), ("off", False),
    ("", False), ("invalid", False),
])
def test_placeholder_value_is_boolean_and_other_switch_stays_off(config_file, monkeypatch, name, raw, expected):
    monkeypatch.setenv(name, raw)
    flash, skill = switches(config_module.get_config())
    if name == FLASH_ENV:
        assert flash is expected
        assert skill is False
    else:
        assert flash is False
        assert skill is expected


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("environment", [None, "true", "false"])
def test_legacy_booleans_are_environment_fallbacks_without_disk_changes(
    config_file, monkeypatch, enabled, environment,
):
    raw = {"flash": {"enabled": enabled, "skill_selection": {"enabled": enabled, "candidate_k": 3}}}
    config_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    original = config_file.read_bytes()
    if environment is not None:
        monkeypatch.setenv(FLASH_ENV, environment)
        monkeypatch.setenv(SKILL_ENV, environment)
    expected = enabled if environment is None else environment == "true"
    resolved = config_module.get_config()
    assert all(value is expected for value in switches(resolved))
    assert resolved.get("flash", {}).get("skill_selection", {}).get("candidate_k") == 3
    assert config_file.read_bytes() == original
    assert all(value is enabled for value in switches(config_module.get_config_raw()))


def test_empty_environment_preserves_legacy_true(config_file, monkeypatch):
    config_file.write_text("flash:\n  enabled: true\n  skill_selection:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv(FLASH_ENV, "")
    monkeypatch.setenv(SKILL_ENV, "")
    assert all(value is True for value in switches(config_module.get_config()))


def test_custom_placeholder_defaults_and_unrelated_strings_are_preserved(config_file, monkeypatch):
    config_file.write_text(
        "flash:\n  enabled: ${JIUWENSWARM_FLASH_ENABLED:-true}\n"
        "  skill_selection:\n    enabled: ${JIUWENSWARM_FLASH_SKILL_SELECTION_ENABLED:-false}\n",
        encoding="utf-8",
    )
    assert switches(config_module.get_config()) == (True, False)
    monkeypatch.setenv("FLASH_TEST_TEXT", "false")
    assert config_module.resolve_env_vars("${FLASH_TEST_TEXT}") == "false"
    assert config_module.resolve_env_vars("${FLASH_TEST_MISSING:-false}") == "false"
    assert config_module.resolve_env_vars("${FLASH_TEST_MISSING-false}") == "false"


def test_sandbox_values_survive_sealed_task_overlay_and_child_export(config_file, monkeypatch):
    monkeypatch.setenv(FLASH_ENV, "true")
    monkeypatch.setenv(SKILL_ENV, "false")
    assert {FLASH_ENV, SKILL_ENV} <= env_config.SPAWN_ENV_KEYS
    namespace = env_config.bind_agent_env_ns("flash-env-test", "agent")
    overlay = env_config.bind_task_env_overlay({FLASH_ENV: "false", SKILL_ENV: "true"})
    try:
        assert switches(config_module.get_config()) == (True, False)
        exported = env_config.export_agent_environ("flash-env-test", "agent")
        assert exported.get(FLASH_ENV) == "true"
        assert exported.get(SKILL_ENV) == "false"
    finally:
        env_config.reset_task_env_overlay(overlay)
        env_config.reset_agent_env_ns(namespace)


def test_provider_configuration_uses_same_resolution_and_keeps_input(config_file, monkeypatch):
    supplied = {"flash": {"enabled": False, "skill_selection": {"enabled": f"${{{SKILL_ENV}:-false}}"}}}
    original = deepcopy(supplied)

    class Provider(providers.ConfigProvider):
        def get_process_config(self):
            return supplied

    providers.register_config_provider(Provider())
    monkeypatch.setenv(FLASH_ENV, "true")
    monkeypatch.setenv(SKILL_ENV, "true")
    assert all(value is True for value in switches(config_module.get_config()))
    assert supplied == original


def test_cache_invalidation_reloads_environment_and_retains_placeholders(config_file, monkeypatch):
    monkeypatch.setenv(FLASH_ENV, "true")
    monkeypatch.setenv(SKILL_ENV, "true")
    assert all(value is True for value in switches(config_module.get_config()))
    monkeypatch.setenv(FLASH_ENV, "false")
    monkeypatch.setenv(SKILL_ENV, "false")
    config_module.clear_config_cache()
    assert all(value is False for value in switches(config_module.get_config()))
    assert switches(config_module.get_config_raw()) == (f"${{{FLASH_ENV}:-false}}", f"${{{SKILL_ENV}:-false}}")
