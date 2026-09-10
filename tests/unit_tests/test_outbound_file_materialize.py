# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Unit tests for enterprise outbound OBS materialize helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jiuwenswarm.gateway.message_handler.outbound_file_materialize import (
    assert_minio_url_allowed,
    build_obs_proxy_download_url,
    chat_file_needs_obs_materialize,
    materialize_outbound_files,
)


def test_chat_file_needs_obs_materialize_personal_false():
    with patch(
        "jiuwenswarm.gateway.message_handler.outbound_file_materialize.is_enterprise",
        return_value=False,
    ):
        assert (
            chat_file_needs_obs_materialize(
                {
                    "event_type": "chat.file",
                    "files": [{"url": "http://minio/x", "name": "a.txt"}],
                }
            )
            is False
        )


def test_chat_file_needs_obs_materialize_enterprise_url_only():
    with patch(
        "jiuwenswarm.gateway.message_handler.outbound_file_materialize.is_enterprise",
        return_value=True,
    ):
        assert (
            chat_file_needs_obs_materialize(
                {
                    "event_type": "chat.file",
                    "files": [{"url": "http://127.0.0.1:9000/b/x", "name": "a.txt"}],
                }
            )
            is True
        )
        assert (
            chat_file_needs_obs_materialize(
                {
                    "event_type": "chat.file",
                    "files": [
                        {
                            "url": "http://127.0.0.1:9000/b/x",
                            "name": "a.txt",
                            "download_token": "tok",
                            "download_url": "/file-api/download?token=tok",
                        }
                    ],
                }
            )
            is False
        )
        assert (
            chat_file_needs_obs_materialize(
                {
                    "files": [{"url": "http://127.0.0.1:9000/b/x", "name": "a.txt"}],
                }
            )
            is True
        )


def test_chat_file_needs_obs_materialize_already_proxied_false():
    with patch(
        "jiuwenswarm.gateway.message_handler.outbound_file_materialize.is_enterprise",
        return_value=True,
    ):
        href = build_obs_proxy_download_url(
            "http://127.0.0.1:9000/b/x", name="a.txt"
        )
        assert (
            chat_file_needs_obs_materialize(
                {
                    "event_type": "chat.file",
                    "files": [{"download_url": href, "name": "a.txt"}],
                }
            )
            is False
        )


def test_materialize_outbound_files_rewrites_to_proxy_href():
    class _Cfg:
        endpoint = "127.0.0.1:9000"
        public_base_url = ""

    with (
        patch(
            "jiuwenswarm.channels.web.minio_upload.load_minio_upload_config",
            return_value=_Cfg(),
        ),
        patch(
            "jiuwenswarm.gateway.message_handler.outbound_file_materialize.is_enterprise",
            return_value=True,
        ),
    ):
        out = materialize_outbound_files(
            [
                {
                    "url": "http://127.0.0.1:9000/b/downloads/x_a.txt",
                    "name": "a.txt",
                    "size": 3,
                }
            ]
        )
    assert len(out) == 1
    assert out[0]["name"] == "a.txt"
    assert out[0]["size"] == 3
    assert out[0]["download_url"].startswith("/file-api/download?url=")
    assert "token=" not in out[0]["download_url"]
    assert "download_token" not in out[0]


def test_assert_minio_url_allowed_rejects_foreign_host():
    class _Cfg:
        endpoint = "127.0.0.1:9000"
        public_base_url = ""

    with patch(
        "jiuwenswarm.channels.web.minio_upload.load_minio_upload_config",
        return_value=_Cfg(),
    ):
        assert_minio_url_allowed("http://127.0.0.1:9000/bucket/obj")
        with pytest.raises(ValueError, match="not allowed"):
            assert_minio_url_allowed("http://evil.example/steal")
        # Same hostname, different port must not pass when endpoint includes port.
        with pytest.raises(ValueError, match="not allowed"):
            assert_minio_url_allowed("http://127.0.0.1:8080/bucket/obj")
        with pytest.raises(ValueError, match="not allowed"):
            assert_minio_url_allowed("http://127.0.0.1/bucket/obj")
