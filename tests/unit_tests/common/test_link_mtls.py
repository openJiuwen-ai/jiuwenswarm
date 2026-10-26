# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from openjiuwen_runtime.foundation.security.link_mtls_config import (
    MTLS_BINDING_EPOCH_HEADER,
    MTLS_BINDING_ID_HEADER,
)

from jiuwenswarm.common.security.link_mtls import (
    MTLSDeploymentIdentity,
    LinkMTLSConfig,
    LinkMTLSError,
    LinkMTLSMode,
    validate_incoming_binding,
)


def test_default_mode_is_off_and_ignores_unused_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_LINK_MTLS_MODE", raising=False)
    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_CA_FILE", "/missing/ca.pem")

    config = LinkMTLSConfig.from_env()

    assert config.mode is LinkMTLSMode.OFF
    assert config.client_ssl_context() is None
    assert config.uvicorn_ssl_kwargs() == {}
    assert config.binding_headers() == {}


def test_invalid_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_MODE", "optional")
    with pytest.raises(LinkMTLSError, match="off, observe or enforce"):
        LinkMTLSConfig.from_env()


def test_enforce_requires_pinned_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_MODE", "enforce")
    for name in (
        "JIUWENSWARM_LINK_MTLS_CA_FILE",
        "JIUWENSWARM_LINK_MTLS_CERT_FILE",
        "JIUWENSWARM_LINK_MTLS_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("JIUWENSWARM_LINK_MTLS_PROFILE", raising=False)
    with pytest.raises(LinkMTLSError, match="not installed"):
        LinkMTLSConfig.from_env()


def test_mtls_binding_headers_do_not_expose_deployment_id() -> None:
    identity = MTLSDeploymentIdentity("deployment-1", "binding-1", 3)
    assert identity.headers() == {
        MTLS_BINDING_ID_HEADER: "binding-1",
        MTLS_BINDING_EPOCH_HEADER: "3",
    }


def test_incoming_binding_mismatch_is_rejected() -> None:
    config = LinkMTLSConfig(
        mode=LinkMTLSMode.ENFORCE,
        identity=MTLSDeploymentIdentity("deployment-1", "binding-1", 2),
    )
    with pytest.raises(LinkMTLSError, match="header-only validation is unsafe"):
        validate_incoming_binding(
            {
                MTLS_BINDING_ID_HEADER: "binding-1",
                MTLS_BINDING_EPOCH_HEADER: "1",
            },
            config,
        )


def test_enforce_rejects_plain_http() -> None:
    config = LinkMTLSConfig(mode=LinkMTLSMode.ENFORCE)
    with pytest.raises(LinkMTLSError, match="must use https"):
        config.require_secure_url("http://runtime:8091", label="runtime")


def test_agent_http_binding_guard_rejects_missing_and_stale_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.agent_http_routes import build_fastapi_app

    config = LinkMTLSConfig(
        mode=LinkMTLSMode.ENFORCE,
        identity=MTLSDeploymentIdentity("deployment-1", "binding-1", 2),
    )
    monkeypatch.setattr(
        LinkMTLSConfig, "from_env", classmethod(lambda cls, **kwargs: config)
    )
    client = TestClient(build_fastapi_app(object()))

    assert client.get("/api/v1/health").status_code == 403
    assert (
        client.get(
            "/api/v1/health",
            headers={
                MTLS_BINDING_ID_HEADER: "binding-1",
                MTLS_BINDING_EPOCH_HEADER: "1",
            },
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/health",
            headers=config.binding_headers(),
        ).status_code
        == 403
    )  # Correct headers cannot substitute for a TLS peer certificate.


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_non_enforce_incomplete_profile_does_not_change_business(monkeypatch, mode):
    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_MODE", mode)
    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_PROFILE", "/not-installed/profile.json")
    monkeypatch.setenv("JIUWENCLAW_ID", "existing-instance")
    c = LinkMTLSConfig.from_env()
    assert (
        c.resolve_endpoint("http://runtime:8091", role="runtime")
        == "http://runtime:8091"
    )
    assert c.binding_headers() == {} and c.client_kwargs(role="runtime") == {}
    assert c.uvicorn_ssl_kwargs() == {}


def test_profile_is_not_business_override_or_tool_child_credential(monkeypatch):
    import os

    from jiuwenswarm.common.local_env_config import (
        LINK_SERVICE_ENV_KEYS,
        export_agent_environ,
        export_spawn_environ,
        set_os_environ,
    )

    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_PROFILE", "/service/gateway/profile.json")
    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_MODE", "enforce")
    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_KEY_FILE", "/service/gateway/tls.key")
    set_os_environ(
        "JIUWENSWARM_LINK_MTLS_PROFILE",
        "/untrusted/profile.json",
        service_id="link-test",
        agent_id="link-agent",
    )
    assert (
        os.environ["JIUWENSWARM_LINK_MTLS_PROFILE"] == "/service/gateway/profile.json"
    )
    assert not (LINK_SERVICE_ENV_KEYS & export_spawn_environ().keys())
    assert not (
        LINK_SERVICE_ENV_KEYS & export_agent_environ("link-test", "link-agent").keys()
    )
