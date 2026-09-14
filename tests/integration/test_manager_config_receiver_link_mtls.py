# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway config receiver rejection logging, including real TLS requests."""

from __future__ import annotations

import asyncio
import importlib
import logging
import socket
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from jiuwenswarm.common.security.link_mtls import (
    LinkMTLSConfig,
    LinkMTLSError,
    LinkMTLSMode,
)


@pytest.fixture
def receiver_module(monkeypatch, caplog):
    # Match the dynamic extension namespace without running registration hooks.
    parent_name = "jiuwenswarm.loaded_extension"
    package_name = parent_name + ".manager_config_receiver"
    extension_root = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "jiuwenclaw-ee/gateway/extensions/manager_config_receiver"
    )
    for name, paths in ((parent_name, []), (package_name, [str(extension_root)])):
        if name not in sys.modules:
            package = ModuleType(name)
            package.__path__ = paths
            monkeypatch.setitem(sys.modules, name, package)
    module = importlib.import_module(package_name + ".http.app")
    monkeypatch.setattr(module.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(module.logger, "propagate", False)
    monkeypatch.setattr(module.logger, "level", logging.WARNING)
    return module


@pytest.mark.parametrize(
    "reason",
    [
        "request binding does not match authenticated deployment",
        "peer certificate is not authorized for this binding and role",
        "authenticated TLS peer certificate is missing",
        "link binding is not active",
        "sensitive-exception-detail\nFORGED-LOG",
    ],
)
def test_rejection_logs_only_fixed_reason(receiver_module, monkeypatch, caplog, reason):
    class RejectConfig:
        def authorize_request(self, request):
            raise LinkMTLSError(reason)

    monkeypatch.setattr(
        receiver_module.LinkMTLSConfig, "from_env", lambda: RejectConfig()
    )
    with TestClient(receiver_module.create_app()) as client:
        response = client.post(
            "/api/v1/private-path-marker?token=private-query-marker",
            headers={
                "Authorization": "Bearer private-token-marker",
                "X-Jiuwenswarm-Mtls-Binding-Id": "private-binding-marker",
                "X-Forwarded-Client-Cert": "private-certificate-marker",
            },
            json={"private_key": "private-key-marker"},
        )
    assert response.status_code == 403
    # Observability only: preserve the existing error response contract.
    assert response.json() == {
        "ok": False,
        "error": {"code": "LINK_BINDING_MISMATCH", "message": reason},
    }
    records = [r for r in caplog.records if r.name == receiver_module.logger.name]
    assert len(records) == 1
    record = records[0]
    expected = (
        reason
        if reason in receiver_module._SAFE_LINK_REJECTION_REASONS
        else "link authorization failed"
    )
    assert (
        record.getMessage()
        == "[ManagerConfigReceiver] rejected link binding: " + expected
    )
    assert record.levelno == logging.WARNING and record.exc_info is None
    assert "private-" not in record.getMessage()
    assert "sensitive-exception-detail" not in record.getMessage()
    assert "FORGED-LOG" not in record.getMessage()


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_non_enforce_health_unchanged_without_rejection_log(
    receiver_module, monkeypatch, caplog, tmp_path, mode
):
    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_MODE", mode)
    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_PROFILE", str(tmp_path / "absent.json"))
    with TestClient(receiver_module.create_app()) as client:
        response = client.get("/api/v1/ready")
    assert response.status_code == 200 and response.json() == {"status": "ready"}
    assert not any(r.name == receiver_module.logger.name for r in caplog.records)


@pytest.mark.asyncio
async def test_actual_tls_receiver_logs_binding_and_role_rejections(
    receiver_module, monkeypatch, caplog, tmp_path
):
    from openjiuwen_runtime.foundation.security.link_certificate_bundle import (
        issue_bundle,
        materialize_role,
    )
    from openjiuwen_runtime.foundation.security.link_profile import LinkProfile

    bundle = issue_bundle(
        mtls_deployment_id="test-rejection-logs",
        endpoints={},
        sans={"gateway": ["127.0.0.1"]},
    )
    profiles = {
        role: LinkProfile.load(str(materialize_role(bundle, role, tmp_path / role)))
        for role in ("gateway", "runtime", "manager")
    }
    config = LinkMTLSConfig(mode=LinkMTLSMode.ENFORCE, profile=profiles["gateway"])
    monkeypatch.setattr(receiver_module.LinkMTLSConfig, "from_env", lambda: config)
    app = receiver_module.create_app()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"https://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            lifespan="off",
            access_log=False,
            log_level="warning",
            **profiles["gateway"].server_kwargs(),
        )
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(300):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        assert server.started
        for role in ("manager", "runtime"):
            profile = profiles[role]
            async with httpx.AsyncClient(
                trust_env=False, timeout=5, **profile.client_kwargs(role="gateway")
            ) as client:
                healthy = await client.get(
                    base + "/api/v1/ready", headers=profile.headers()
                )
                assert healthy.status_code == 200
                headers = profile.headers()
                if role == "manager":
                    allowed = await client.get(
                        base + "/api/v1/__link_binding_probe__", headers=headers
                    )
                    assert allowed.status_code == 404  # Passed auth; no write or route.
                    headers["X-Jiuwenswarm-Mtls-Binding-Id"] = "private-binding-marker"
                response = await client.get(
                    base + "/api/v1/__link_binding_probe__", headers=headers
                )
                assert response.status_code == 403
                assert response.json()["error"]["code"] == "LINK_BINDING_MISMATCH"
                healthy = await client.get(
                    base + "/api/v1/ready", headers=profile.headers()
                )
                assert healthy.status_code == 200
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()
    records = [r for r in caplog.records if r.name == receiver_module.logger.name]
    assert [r.getMessage() for r in records] == [
        "[ManagerConfigReceiver] rejected link binding: request binding does not match authenticated deployment",
        "[ManagerConfigReceiver] rejected link binding: peer certificate is not authorized for this binding and role",
    ]
    assert all(r.levelno == logging.WARNING and r.exc_info is None for r in records)
