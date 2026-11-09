# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Tests for OBS_* object-store env loading."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from jiuwenswarm.channels.web.minio_upload import (
    MinioUploadConfig,
    huawei_obs_needs_virtual_style,
    huawei_obs_virtual_hosted_bucket_host,
    load_minio_upload_config,
    upload_local_file_to_minio,
)


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


def test_huawei_obs_enables_virtual_style_endpoint(tmp_path):
    cfg = MinioUploadConfig(
        endpoint="obs.cn-north-4.myhuaweicloud.com",
        access_key="ak",
        secret_key="sk",
        bucket="jiuwenswarm-ee",
        secure=True,
        region="cn-north-4",
    )
    local = tmp_path / "a.txt"
    local.write_bytes(b"hi")

    fake_client = MagicMock()
    fake_client.bucket_exists.return_value = True
    fake_client.presigned_get_object.return_value = (
        "https://jiuwenswarm-ee.obs.cn-north-4.myhuaweicloud.com/uploads/x"
    )

    with patch(
        "minio.Minio",
        return_value=fake_client,
    ) as minio_cls:
        result = upload_local_file_to_minio(cfg, str(local), filename="a.txt")

    minio_cls.assert_called_once()
    fake_client.enable_virtual_style_endpoint.assert_called_once_with()
    fake_client.fput_object.assert_called_once()
    assert result["name"] == "a.txt"
    assert "url" in result


def test_self_hosted_minio_skips_virtual_style_endpoint(tmp_path):
    cfg = MinioUploadConfig(
        endpoint="minio-headless.default:9000",
        access_key="ak",
        secret_key="sk",
        bucket="b",
        secure=False,
    )
    local = tmp_path / "a.txt"
    local.write_bytes(b"hi")

    fake_client = MagicMock()
    fake_client.bucket_exists.return_value = True
    fake_client.presigned_get_object.return_value = "http://minio/uploads/x"

    with patch("minio.Minio", return_value=fake_client):
        upload_local_file_to_minio(cfg, str(local), filename="a.txt")

    fake_client.enable_virtual_style_endpoint.assert_not_called()


def test_huawei_obs_virtual_style_helpers_aligned():
    endpoint = "obs.cn-north-4.myhuaweicloud.com"
    bucket = "jiuwenswarm-ee"
    assert huawei_obs_needs_virtual_style(endpoint, bucket) is True
    assert (
        huawei_obs_virtual_hosted_bucket_host(endpoint, bucket)
        == "jiuwenswarm-ee.obs.cn-north-4.myhuaweicloud.com"
    )
    # Case-insensitive Huawei marker.
    assert huawei_obs_needs_virtual_style(
        "OBS.CN-NORTH-4.MYHUAWEICLOUD.COM", bucket
    ) is True
    # Legacy OBS_URL already embeds bucket: do not double-prefix.
    legacy = "jiuwenswarm-ee.obs.cn-north-4.myhuaweicloud.com"
    assert huawei_obs_needs_virtual_style(legacy, bucket) is False
    assert huawei_obs_virtual_hosted_bucket_host(legacy, bucket) is None


def test_huawei_obs_skips_virtual_style_when_url_embeds_bucket(tmp_path):
    cfg = MinioUploadConfig(
        endpoint="jiuwenswarm-ee.obs.cn-north-4.myhuaweicloud.com",
        access_key="ak",
        secret_key="sk",
        bucket="jiuwenswarm-ee",
        secure=True,
        region="cn-north-4",
    )
    local = tmp_path / "a.txt"
    local.write_bytes(b"hi")

    fake_client = MagicMock()
    fake_client.bucket_exists.return_value = True
    fake_client.presigned_get_object.return_value = (
        "https://jiuwenswarm-ee.obs.cn-north-4.myhuaweicloud.com/uploads/x"
    )

    with patch("minio.Minio", return_value=fake_client):
        upload_local_file_to_minio(cfg, str(local), filename="a.txt")

    fake_client.enable_virtual_style_endpoint.assert_not_called()
