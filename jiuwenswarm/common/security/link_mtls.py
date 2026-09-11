# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP/SSE internal-link mTLS configuration shared by Gateway and AgentServer.

The feature is deliberately opt-in.  With the default ``off`` mode this module
does not create SSL contexts, rewrite URLs, or change the existing HTTP path.
"""

from __future__ import annotations

import hashlib
import logging
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from openjiuwen_runtime.foundation.security.link_mtls_config import (
    MODE_ENV,
    MTLSDeploymentIdentity,
    LinkMTLSMode,
)
from openjiuwen_runtime.foundation.security.link_profile import (
    LinkProfile,
    LinkProfileError,
    load_service_identity,
)

logger = logging.getLogger(__name__)

LinkMTLSError = LinkProfileError


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


@dataclass(frozen=True)
class LinkMTLSConfig:
    mode: LinkMTLSMode

    profile: LinkProfile | None = None
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    identity: MTLSDeploymentIdentity | None = None

    @classmethod
    def from_env(cls, *, role: str = "gateway") -> "LinkMTLSConfig":
        try:
            mode = LinkMTLSMode(_env(MODE_ENV) or "off")
        except ValueError as exc:
            raise LinkMTLSError(f"{MODE_ENV} must be off, observe or enforce") from exc

        shared = {}
        if mode is LinkMTLSMode.OFF:
            return cls(mode=mode, **shared)
        try:
            if role not in {"gateway", "agentserver"}:
                raise LinkMTLSError("unsupported JiuwenSwarm service role")
            profile = load_service_identity(role)
        except (ValueError, OSError) as exc:
            if mode is LinkMTLSMode.ENFORCE:
                raise LinkMTLSError(str(exc)) from exc
            logger.warning("link mTLS observe preflight failed: %s", exc)
            return cls(mode=mode, **shared)
        if mode is LinkMTLSMode.OBSERVE:
            logger.info(
                "link mTLS observe material preflight passed; business transport unchanged"
            )
            return cls(mode=mode, **shared)
        return cls(
            mode=mode,
            profile=profile,
            ca_file=profile.ca_file,
            cert_file=profile.cert_file,
            key_file=profile.key_file,
            identity=MTLSDeploymentIdentity(
                profile.mtls_deployment_id,
                profile.mtls_binding_id,
                profile.mtls_binding_epoch,
            ),
            **shared,
        )

    def resolve_endpoint(self, url: str, *, role: str) -> str:
        if self.profile is not None:
            return self.profile.endpoint(url, role=role, enforced=self.enforced)
        self.require_secure_url(url, label=f"{role} URL")
        return url

    def client_kwargs(self, *, role: str) -> dict:
        if not self.enforced:
            return {}
        if self.profile is None:
            raise LinkMTLSError("enforce requires a pinned deployment profile")
        return self.profile.client_kwargs(role=role)

    def authorize_request(self, request) -> None:
        if self.enforced:
            if self.profile is None:
                raise LinkMTLSError("enforce requires a pinned deployment profile")
            self.profile.authorize(request.scope, request.headers)

    @property
    def enforced(self) -> bool:
        return self.mode is LinkMTLSMode.ENFORCE

    def client_ssl_context(self) -> ssl.SSLContext | None:
        if not self.ca_file or not self.cert_file or not self.key_file:
            return None
        context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH,
            cafile=self.ca_file,
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_cert_chain(certfile=self.cert_file, keyfile=self.key_file)
        return context

    def uvicorn_ssl_kwargs(self) -> dict[str, object]:
        if not self.enforced:
            return {}
        if self.profile is None:
            raise LinkMTLSError("enforce requires a pinned deployment profile")
        return self.profile.server_kwargs()

    def require_secure_url(self, url: str, *, label: str) -> None:
        if self.enforced and urlsplit(url).scheme.lower() != "https":
            raise LinkMTLSError(
                f"{label} must use https:// when {MODE_ENV}=enforce: {url!r}"
            )

    def binding_headers(self) -> dict[str, str]:
        if self.profile is not None:
            return self.profile.headers()
        return self.identity.headers() if self.identity is not None else {}

    def cert_fingerprint(self) -> str:
        """Return a stable SHA-256 fingerprint for the configured public cert."""
        if not self.cert_file:
            return ""
        return hashlib.sha256(
            ssl.PEM_cert_to_DER_cert(Path(self.cert_file).read_text())
        ).hexdigest()


def validate_incoming_binding(
    headers: Mapping[str, str], config: LinkMTLSConfig
) -> None:
    """Validate application binding metadata after the mTLS handshake."""
    if config.enforced:
        raise LinkMTLSError(
            "header-only validation is unsafe; use authorize_request with TLS peer scope"
        )
