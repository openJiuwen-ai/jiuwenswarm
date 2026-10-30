# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import ssl
from unittest.mock import Mock

import pytest

from jiuwenswarm.common import obs_files
from jiuwenswarm.common.obs_files import AttachmentStore, object_prefix


@pytest.mark.parametrize("value,expected", [
    (None, "CERT_NONE"), ("", "CERT_NONE"), ("  ", "CERT_NONE"),
    ("false", "CERT_NONE"), (" False ", "CERT_NONE"), ("0", "CERT_NONE"),
    ("no", "CERT_NONE"), ("OFF", "CERT_NONE"),
    ("true", "CERT_REQUIRED"), (" TRUE ", "CERT_REQUIRED"),
    ("1", "CERT_REQUIRED"), ("yes", "CERT_REQUIRED"), ("on", "CERT_REQUIRED"),
    ("invalid", "CERT_REQUIRED"),
])
def test_obs_sdk_and_attachment_reader_share_verification_policy(monkeypatch, value, expected):
    monkeypatch.setenv("OBS_URL", "https://obs.example.test")
    monkeypatch.setenv("OBS_SECURE", "false")
    monkeypatch.setenv("OBS_ACCESS_KEY", "test-access")
    monkeypatch.setenv("OBS_SECRET_KEY", "test-secret")
    monkeypatch.setenv("OBS_BUCKET", "test-bucket")
    if value is None:
        monkeypatch.delenv("JIUWENCLAW_SSL_VERIFY", raising=False)
    else:
        monkeypatch.setenv("JIUWENCLAW_SSL_VERIFY", value)
    sdk_pool = Mock()
    response = Mock(status=200)
    response.read.return_value = b"attachment"
    url_pool = Mock()
    url_pool.request.return_value = response
    pools = Mock(side_effect=[sdk_pool, url_pool])
    sdk = Mock()
    monkeypatch.setattr(obs_files, "PoolManager", pools)
    monkeypatch.setattr(obs_files, "Minio", sdk)

    store = AttachmentStore.from_env()
    assert store.origin == "https://obs.example.test"
    assert sdk.call_args.kwargs["secure"] is True
    assert sdk.call_args.kwargs["http_client"] is sdk_pool
    prefix = object_prefix("g", "b", "u")
    url = f"{store.origin}/test-bucket/{prefix}{'a' * 32}/file.txt"
    assert store.read(url, prefix) == b"attachment"
    assert len(pools.call_args_list) == 2
    for call in pools.call_args_list:
        assert call.kwargs["cert_reqs"] == expected
        assert call.kwargs["retries"] is False
        timeout = call.kwargs["timeout"]
        assert timeout.connect_timeout == 5 and timeout.read_timeout == 60
    url_pool.request.assert_called_once_with("GET", url, redirect=False, preload_content=False)
    response.close.assert_called_once()
    response.release_conn.assert_called_once()
    url_pool.clear.assert_called_once()


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
@pytest.mark.parametrize("verify", ["false", "true"])
def test_obs_policy_does_not_change_internal_mtls_or_global_tls(monkeypatch, mode, verify):
    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_MODE", mode)
    monkeypatch.setenv("JIUWENCLAW_SSL_VERIFY", verify)
    original_context_factory = ssl._create_default_https_context
    pool = obs_files._obs_http_client()
    try:
        assert pool.connection_pool_kw["cert_reqs"] == (
            "CERT_REQUIRED" if verify == "true" else "CERT_NONE"
        )
        assert ssl._create_default_https_context is original_context_factory
        assert "cert_file" not in pool.connection_pool_kw
        assert "key_file" not in pool.connection_pool_kw
    finally:
        pool.clear()


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("secure", ["false", "true"])
def test_verification_switch_never_changes_explicit_obs_protocol(monkeypatch, scheme, secure):
    monkeypatch.setenv("OBS_URL", f"{scheme}://obs.example.test")
    monkeypatch.setenv("OBS_SECURE", secure)
    monkeypatch.setenv("OBS_ACCESS_KEY", "test-access")
    monkeypatch.setenv("OBS_SECRET_KEY", "test-secret")
    monkeypatch.setenv("JIUWENCLAW_SSL_VERIFY", "false")
    sdk = Mock()
    monkeypatch.setattr(obs_files, "Minio", sdk)
    store = AttachmentStore.from_env()
    try:
        assert store.origin == f"{scheme}://obs.example.test"
        assert sdk.call_args.kwargs["secure"] is (scheme == "https")
    finally:
        sdk.call_args.kwargs["http_client"].clear()
