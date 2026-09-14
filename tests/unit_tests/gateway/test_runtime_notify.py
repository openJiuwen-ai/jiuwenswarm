# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manager ConfigReceiver runtime_notify：写库后触发 agent-runtime config_refresh。"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

from jiuwenswarm.common.security.link_mtls import (
    LinkMTLSConfig,
    LinkMTLSMode,
)

_RECEIVER_DIR = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "jiuwenclaw-ee"
    / "gateway"
    / "extensions"
    / "manager_config_receiver"
    / "routers"
)
if str(_RECEIVER_DIR) not in sys.path:
    sys.path.insert(0, str(_RECEIVER_DIR))

from runtime_notify import (  # noqa: E402
    _request_agentserver_refresh,
    trigger_runtime_config_update,
)


@pytest.mark.asyncio
async def test_trigger_runtime_config_update_skips_without_runtime_url(
    monkeypatch,
) -> None:
    monkeypatch.delenv("GATEWAY_RUNTIME_MANAGER_URL", raising=False)
    trigger_runtime_config_update()


@pytest.mark.asyncio
async def test_request_agentserver_refresh_posts_empty_payload(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GATEWAY_RUNTIME_MANAGER_URL", "http://runtime-manager:8091")
    captured: dict = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            self._kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(
            self,
            url: str,
            *,
            json: dict | None = None,
            headers: dict | None = None,
        ):
            captured["url"] = url
            captured["body"] = json
            captured["headers"] = headers

            class _Resp:
                status_code = 200
                text = ""

                @staticmethod
                def json() -> dict:
                    return {
                        "rawdata": {"ok": True, "scopes_refreshed": 1, "pods_sunset": 2}
                    }

            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    await _request_agentserver_refresh()

    assert captured["url"] == "http://runtime-manager:8091/api/session/config_refresh"
    assert captured["body"]["type"] == "config_refresh"
    assert captured["body"]["rawdata"] == {}
    assert captured["headers"] == {}


@pytest.mark.asyncio
async def test_request_agentserver_refresh_enforce_sends_binding_headers(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("GATEWAY_RUNTIME_MANAGER_URL", "http://runtime-manager:8091")
    captured: dict = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(
            self,
            url: str,
            *,
            json: dict | None = None,
            headers: dict | None = None,
        ):
            captured.update(url=url, body=json, headers=headers)

            class _Resp:
                status_code = 200
                text = ""

                @staticmethod
                def json() -> dict:
                    return {"rawdata": {"cleaned": 1}}

            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    from openjiuwen_runtime.foundation.security.link_profile import LinkProfile

    from tests.fixtures.link_mtls import provision

    provision(
        tmp_path / "bundle",
        mtls_deployment_id="deployment-1",
        mtls_binding_epoch=2,
        endpoints={"runtime": "runtime-manager:8091"},
    )
    p = LinkProfile.load(str(tmp_path / "bundle/gateway/profile.json"))
    config = LinkMTLSConfig(
        mode=LinkMTLSMode.ENFORCE,
        profile=p,
    )

    await _request_agentserver_refresh(config)

    assert captured["url"] == "https://runtime-manager:8091/api/session/config_refresh"
    assert captured["body"]["type"] == "config_refresh"
    assert captured["body"]["rawdata"] == {}
    assert captured["headers"] == p.headers()
    assert "transport" in captured["client_kwargs"]
