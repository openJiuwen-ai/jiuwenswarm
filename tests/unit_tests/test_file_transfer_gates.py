# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Gates for file_transfer: edition-driven download; enterprise skips GW→Agent upload."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jiuwenswarm.common.file_transfer_config import (
    FileTransferConfig,
    clear_file_transfer_config_cache,
    resolve_file_transfer_enabled,
)
from jiuwenswarm.gateway.message_handler.file_transfer_mixin import FileTransferMixin


class _Host(FileTransferMixin):
    def __init__(self) -> None:
        self._file_transfer_handler = None
        self.agent_client = MagicMock()


def test_should_transfer_files_false_when_enterprise():
    host = _Host()
    env = SimpleNamespace(params={"files": [{"path": "/tmp/a.txt"}]})
    with patch(
        "jiuwenswarm.gateway.message_handler.file_transfer_mixin.is_enterprise",
        return_value=True,
    ):
        assert host._should_transfer_files(env) is False


def test_should_transfer_files_false_when_disabled():
    host = _Host()
    handler = MagicMock()
    handler.enabled = False
    host._file_transfer_handler = handler
    env = SimpleNamespace(params={"files": [{"path": "/tmp/a.txt"}]})
    with patch(
        "jiuwenswarm.gateway.message_handler.file_transfer_mixin.is_enterprise",
        return_value=False,
    ):
        assert host._should_transfer_files(env) is False


def test_file_transfer_config_default_disabled():
    assert FileTransferConfig().enabled is False


def test_resolve_enabled_defaults_false():
    # 默认关闭（个人/企业均如此，OBS 为主路径，不再读 is_enterprise）。
    assert resolve_file_transfer_enabled({}) is False


def test_resolve_enabled_yaml_override():
    assert resolve_file_transfer_enabled({"enabled": False}) is False
    assert resolve_file_transfer_enabled({"enabled": True}) is True


def test_resolve_enabled_escape_env(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH", "1")
    assert resolve_file_transfer_enabled({}) is True
    monkeypatch.setenv("JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH", "0")
    assert resolve_file_transfer_enabled({}) is False
    # YAML explicit wins over escape env
    monkeypatch.setenv("JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH", "1")
    assert resolve_file_transfer_enabled({"enabled": False}) is False


def test_get_file_transfer_config_resolves_enabled(monkeypatch):
    clear_file_transfer_config_cache()
    monkeypatch.delenv("JIUWENSWARM_FILE_DOWNLOAD_VIA_PUSH", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.file_transfer_config.get_config",
        lambda: {"file_transfer": {"chunk_size": 1024}},
    )
    with patch(
        "jiuwenswarm.common.local_env_config.is_enterprise",
        return_value=True,
    ):
        from jiuwenswarm.common.file_transfer_config import get_file_transfer_config

        cfg = get_file_transfer_config()
        assert cfg.enabled is False
        assert cfg.chunk_size == 1024
    clear_file_transfer_config_cache()
