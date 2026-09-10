# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Tests for OBS_* object-store env loading."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jiuwenswarm.channels.web.minio_upload import load_minio_upload_config


def test_load_from_obs_env(monkeypatch):
    monkeypatch.setenv("OBS_URL", "obs.example:9000")
    monkeypatch.setenv("OBS_ACCESS_KEY", "obs-ak")
    monkeypatch.setenv("OBS_SECRET_KEY", "obs-sk")
    monkeypatch.setenv("OBS_BUCKET", "obs-bucket")
    monkeypatch.setenv("OBS_SECURE", "true")

    cfg = load_minio_upload_config()
    assert cfg.endpoint == "obs.example:9000"
    assert cfg.access_key == "obs-ak"
    assert cfg.secret_key == "obs-sk"
    assert cfg.bucket == "obs-bucket"
    assert cfg.secure is True


def test_load_falls_back_to_yaml_minio(monkeypatch):
    for key in (
        "OBS_URL",
        "OBS_ACCESS_KEY",
        "OBS_SECRET_KEY",
        "OBS_BUCKET",
        "OBS_SECURE",
        "OBS_PUBLIC_BASE_URL",
        "OBS_REGION",
    ):
        monkeypatch.delenv(key, raising=False)

    with patch(
        "jiuwenswarm.common.config.get_config",
        return_value={
            "minio": {
                "endpoint": "127.0.0.1:9000",
                "access_key": "ak",
                "secret_key": "sk",
                "bucket": "b",
                "secure": False,
            }
        },
    ):
        cfg = load_minio_upload_config()
    assert cfg.endpoint == "127.0.0.1:9000"
    assert cfg.access_key == "ak"
    assert cfg.bucket == "b"
    assert cfg.secure is False


def test_load_raises_when_missing(monkeypatch):
    for key in (
        "OBS_URL",
        "OBS_ACCESS_KEY",
        "OBS_SECRET_KEY",
        "OBS_BUCKET",
    ):
        monkeypatch.delenv(key, raising=False)
    with patch("jiuwenswarm.common.config.get_config", return_value={}):
        with pytest.raises(RuntimeError, match="OBS_URL"):
            load_minio_upload_config()
