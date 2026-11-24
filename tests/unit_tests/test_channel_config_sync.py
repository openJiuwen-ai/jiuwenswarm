# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from pathlib import Path

import yaml

from jiuwenswarm.common import config


def _write(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_channel_snapshot_replaces_removed_apps(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    _write(path, {
        "models": {"default": {"model_name": "keep"}},
        "channels": {
            "feishu": {
                "apps": [{"app_id": "old", "app_secret": "secret"}],
                "send_file_allowed": True,
            },
            "telegram": {"bot_token": "keep"},
        },
    })
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)

    assert config.apply_channel_config_snapshot({
        "feishu": {"apps": [], "send_file_allowed": False},
    }) is True

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["channels"]["feishu"] == {
        "apps": [],
        "send_file_allowed": False,
    }
    assert saved["channels"]["telegram"] == {"bot_token": "keep"}
    assert saved["models"] == {"default": {"model_name": "keep"}}


def test_channel_snapshot_without_apps_clears_old_apps(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    _write(path, {
        "channels": {
            "dingtalk": {"apps": [{"client_id": "old"}], "enabled": True},
        },
    })
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)

    assert config.apply_channel_config_snapshot({
        "dingtalk": {"enabled": False},
    }) is True

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["channels"]["dingtalk"] == {"enabled": False}


def test_channel_snapshot_only_updates_send_file_for_other_channels(
    tmp_path: Path, monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    _write(path, {
        "channels": {
            "telegram": {"bot_token": "token", "send_file_allowed": True},
        },
    })
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)

    assert config.apply_channel_config_snapshot({
        "telegram": {"send_file_allowed": False},
    }) is True

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["channels"]["telegram"] == {
        "bot_token": "token",
        "send_file_allowed": False,
    }


def test_xiaoyi_snapshot_replaces_apps_but_keeps_as_only_fields(
    tmp_path: Path, monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    _write(path, {
        "channels": {
            "xiaoyi": {
                "apps": [{"app_id": "old"}],
                "phone_tools_enabled": True,
            },
        },
    })
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)

    assert config.apply_channel_config_snapshot({
        "xiaoyi": {"apps": [], "send_file_allowed": False},
    }) is True

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["channels"]["xiaoyi"] == {
        "apps": [],
        "phone_tools_enabled": True,
        "send_file_allowed": False,
    }


def test_dingtalk_snapshot_keeps_local_file_settings(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    _write(path, {
        "channels": {
            "dingtalk": {
                "apps": [{"client_id": "old"}],
                "enabled": False,
                "max_download_size": 4096,
                "download_timeout": 15,
                "enable_file_download": False,
                "workspace_dir": "custom-workspace",
            },
        },
    })
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)

    assert config.apply_channel_config_snapshot({
        "dingtalk": {
            "apps": [{"client_id": "new", "client_secret": "secret"}],
            "enabled": True,
        },
    }) is True

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["channels"]["dingtalk"] == {
        "apps": [{"client_id": "new", "client_secret": "secret"}],
        "enabled": True,
        "max_download_size": 4096,
        "download_timeout": 15,
        "enable_file_download": False,
        "workspace_dir": "custom-workspace",
    }


def test_feishu_enterprise_snapshot_replaces_removed_bots(
    tmp_path: Path, monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    _write(path, {
        "channels": {
            "feishu_enterprise": {
                "old": {"app_id": "old", "app_secret": "secret"},
                "keep": {"app_id": "old-keep", "workspace_dir": "local-workspace"},
            },
        },
    })
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)

    assert config.apply_channel_config_snapshot({
        "feishu_enterprise": {
            "new": {"app_id": "new"},
            "keep": {"app_id": "new-keep"},
        },
    }) is True

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["channels"]["feishu_enterprise"] == {
        "new": {"app_id": "new"},
        "keep": {"app_id": "new-keep", "workspace_dir": "local-workspace"},
    }


def test_channel_snapshot_returns_false_for_unapplied_payloads(
    tmp_path: Path, monkeypatch,
) -> None:
    path = tmp_path / "config.yaml"
    _write(path, {"models": {"default": {"model_name": "keep"}}})
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)

    assert config.apply_channel_config_snapshot(None) is False
    assert config.apply_channel_config_snapshot({}) is False
    assert config.apply_channel_config_snapshot({"unknown": {"enabled": True}}) is False
    assert config.apply_channel_config_snapshot({"feishu": "invalid"}) is False
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "models": {"default": {"model_name": "keep"}},
    }
