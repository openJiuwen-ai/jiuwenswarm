"""Local Gateway launchers prepare the effective extension search path."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from types import ModuleType

import pytest
from agent_gateway.common import paths as gateway_paths

from jiuwenswarm.common import utils


@pytest.mark.parametrize("extension_dirs", ["", "D:/custom/ext;D:/other/ext"])
def test_gateway_uses_local_instance_config_and_extension_dirs(tmp_path: Path, monkeypatch, extension_dirs):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    dotenv = config_dir / ".env"
    dotenv.write_text(f'EXTENSION_DIRS="{extension_dirs}"\nUNRELATED_SETTING=yes\n', encoding="utf-8")
    monkeypatch.setattr(utils, "get_config_dir", lambda: config_dir)
    # get_env_file 在 paths 层解析 config 目录，需一并重定向；gateway 落盘名
    # 已改为 config/config-gateway.yaml
    monkeypatch.setattr("jiuwenswarm.common.paths.get_config_dir", lambda: config_dir)
    monkeypatch.setattr(gateway_paths, "get_config_file", lambda: config_dir / "config-gateway.yaml")
    monkeypatch.setattr(utils, "is_agentos_runtime", lambda: False)

    env: dict[str, str] = {}
    utils.prepare_local_gateway_config(env)
    assert env == {
        "JIUWENSWARM_GATEWAY_CONFIG_DIR": str(config_dir),
        "EXTENSION_DIRS": extension_dirs,
    }


@pytest.mark.parametrize("gateway_config", ["explicit", "infra", "agentos"])
def test_gateway_preserves_deployment_config(tmp_path: Path, monkeypatch, gateway_config):
    config = tmp_path / "infra" / "config" / "config.yaml"
    monkeypatch.setattr(gateway_paths, "get_config_file", lambda: config)
    monkeypatch.setattr(utils, "is_agentos_runtime", lambda: gateway_config == "agentos")
    env: dict[str, str] = {"EXTENSION_DIRS": "D:/custom/ext"}
    extension_dirs = "D:/custom/ext" if gateway_config == "infra" else "${EXTENSION_DIRS:-agent_gateway/extensions}"
    config.parent.mkdir(parents=True)
    original_config = f'extensions:\n  extension_dirs: "{extension_dirs}"\n'
    config.write_text(original_config, encoding="utf-8")
    if gateway_config == "explicit":
        env["JIUWENSWARM_GATEWAY_CONFIG_DIR"] = str(tmp_path / "gateway")

    original = env.copy()
    utils.prepare_local_gateway_config(env)
    assert env == original
    assert config.read_text(encoding="utf-8") == original_config


def test_gateway_migrates_stock_infra_config(tmp_path: Path, monkeypatch):
    config = tmp_path / "infra" / "config" / "config.yaml"
    config.parent.mkdir(parents=True)
    legacy_dirs = "${EXTENSION_DIRS:-agent_gateway/extensions}"
    original = f'extensions:\n  extension_dirs: "{legacy_dirs}"\n  include_package_extensions: false\n'
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(gateway_paths, "get_config_file", lambda: config)
    monkeypatch.setattr(utils, "is_agentos_runtime", lambda: False)
    env: dict[str, str] = {}

    for _ in range(2):
        utils.prepare_local_gateway_config(env)
        assert env == {}
        assert config.read_text(encoding="utf-8") == original.replace(
            legacy_dirs, f"{legacy_dirs};jiuwenswarm/extensions",
        )


@pytest.mark.parametrize("existing_gateway_config", [False, True])
def test_desktop_gateway_prepares_config_before_import(tmp_path, monkeypatch, existing_gateway_config):
    from jiuwenswarm.channels.desktop.gateway_entry import main

    swarm_dir = tmp_path / "config"
    swarm_dir.mkdir()
    gateway_file = tmp_path / "infra" / "config.yaml"
    gateway_file.parent.mkdir()
    legacy_dirs = "${EXTENSION_DIRS:-agent_gateway/extensions}"
    if existing_gateway_config:
        gateway_file.write_text(f'extensions:\n  extension_dirs: "{legacy_dirs}"\n', encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["gateway_entry"])
    monkeypatch.setenv("JIUWENSWARM_GATEWAY_CONFIG_DIR", "")
    monkeypatch.setattr(utils, "is_agentos_runtime", lambda: False)
    monkeypatch.setattr(utils, "get_config_dir", lambda: swarm_dir)
    monkeypatch.setattr(gateway_paths, "get_config_file", lambda: gateway_file)
    gateway = ModuleType("agent_gateway.app_gateway")
    launches = []

    def get_gateway_main(name):
        if name != "main":
            raise AttributeError(name)
        if existing_gateway_config:
            assert f"{legacy_dirs};jiuwenswarm/extensions" in gateway_file.read_text(encoding="utf-8")
        else:
            assert os.environ["JIUWENSWARM_GATEWAY_CONFIG_DIR"] == str(swarm_dir)
        return lambda: launches.append(True)

    gateway.__getattr__ = get_gateway_main
    monkeypatch.setitem(sys.modules, gateway.__name__, gateway)

    main()

    assert launches == [True]
