# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for WeCom/DingTalk lazy per-tenant file workspace."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from jiuwenswarm.gateway.channel_manager.im_platforms.dingtalk.dingtalk_file_service import (
    DingTalkFileService,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.wecom.wecom_file_service import (
    WecomFileService,
)
from tests.unit_tests.tenant_workspace_test_helpers import (
    patch_multi_tenant_workspace_dirs,
)


def test_wecom_file_service_tenant_scope_isolates(tmp_path, monkeypatch):
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    svc = WecomFileService(ws_client=SimpleNamespace(), workspace_dir=None)
    assert not (tmp_path / "workspace_default_office").exists()

    with svc.tenant_scope("default", "office"):
        office_dir = Path(svc._get_download_dir("images"))
    with svc.tenant_scope("default", "default"):
        default_dir = Path(svc._get_download_dir("images"))

    assert "workspace_default_office" in office_dir.parts
    assert "workspace_default" in default_dir.parts
    assert "workspace_default_office" not in default_dir.parts
    assert office_dir != default_dir
    assert office_dir.exists()
    assert default_dir.exists()


def test_wecom_file_service_config_override(tmp_path):
    override = tmp_path / "custom_ws"
    svc = WecomFileService(ws_client=SimpleNamespace(), workspace_dir=str(override))
    with svc.tenant_scope("default", "office"):
        path = Path(svc._get_download_dir("files"))
    assert path == override / "wecom_files" / "downloads" / "files"


def test_dingtalk_file_service_tenant_scope_isolates(tmp_path, monkeypatch):
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)

    async def _token():
        return "tok"

    svc = DingTalkFileService(
        client_id="app",
        get_token_func=_token,
        http_client=SimpleNamespace(),
        api_base="https://example.com",
        oapi_base="https://oapi.example.com",
        workspace_dir=None,
    )
    with svc.tenant_scope("default", "office"):
        office_dir = Path(svc._get_download_dir("image"))
    with svc.tenant_scope("default", "default"):
        default_dir = Path(svc._get_download_dir("image"))

    assert "workspace_default_office" in office_dir.parts
    assert office_dir != default_dir
