# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""/mcp handler 须走 build_mcp_server_config（鉴权与 http 别名）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_mcp_handler_module():
    """绕过 handlers 包 __init__（会拉 harness rails）。"""
    path = (
        Path(__file__).resolve().parents[3]
        / "jiuwenswarm"
        / "server"
        / "handlers"
        / "mcp.py"
    )
    spec = importlib.util.spec_from_file_location("jws_mcp_handler_isolated", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_fetch_mcp_tools_maps_headers_and_http_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_handler = _load_mcp_handler_module()
    captured: dict = {}

    class FakeClient:
        async def connect(self):
            return True

        async def list_tools(self):
            return [
                SimpleNamespace(
                    id="t1",
                    name="qa_echo",
                    description="echo",
                    input_params={"type": "object"},
                )
            ]

        async def disconnect(self):
            return True

    def fake_create_client(cfg):
        captured["cfg"] = cfg
        return FakeClient()

    monkeypatch.setattr(
        "openjiuwen.core.runner.resources_manager.tool_manager.ToolMgr._create_client",
        staticmethod(fake_create_client),
    )

    tools = await mcp_handler._fetch_mcp_tools_from_config(
        {
            "name": "qa-noauth",
            "transport": "http",
            "url": "http://192.168.1.96:18014/mcp",
            "headers": {"Authorization": "Bearer sds-token"},
            "timeout_s": 0.5,
        }
    )
    assert len(tools) == 1
    cfg = captured["cfg"]
    assert cfg.client_type == "streamable-http"
    assert cfg.auth_headers == {"Authorization": "Bearer sds-token"}
    assert cfg.params.get("timeout_s") == 0.5
    assert "headers" not in (cfg.params or {})
