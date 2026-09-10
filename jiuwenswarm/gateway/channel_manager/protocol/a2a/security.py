"""Shared ingress authentication validation and ASGI enforcement."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any


def hash_credential(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[!-~]{16,512}", value):
        raise ValueError(
            "credential must contain 16 to 512 printable ASCII characters without spaces"
        )
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def validate_security(config: Any) -> None:
    if config.auth_type not in {"none", "bearer", "api_key"}:
        raise ValueError("auth_type must be none, bearer, or api_key")
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", config.api_key_header):
        raise ValueError("api_key_header must be a valid HTTP header name")
    if config.api_key_header.lower() in {
        "authorization",
        "host",
        "content-length",
        "content-type",
        "connection",
        "transfer-encoding",
        "cookie",
        "set-cookie",
        "accept",
        "origin",
    }:
        raise ValueError("api_key_header must be a dedicated credential header")
    if not isinstance(config.card_auth_required, bool):
        raise ValueError("card_auth_required must be a boolean")
    if config.credential_hash and not re.fullmatch(
        r"[0-9a-f]{64}", config.credential_hash
    ):
        raise ValueError("Invalid stored ingress credential digest")
    if config.auth_type != "none" and not config.credential_hash:
        raise ValueError("A credential is required when authentication is enabled")
    if config.card_auth_required and config.auth_type == "none":
        raise ValueError("Card authentication requires an authentication method")


def agent_card_security(config: Any) -> dict[str, Any]:
    if config.auth_type == "none":
        return {}
    from a2a.types import SecurityScheme

    scheme = (
        SecurityScheme(http_auth_security_scheme={"scheme": "bearer"})
        if config.auth_type == "bearer"
        else SecurityScheme(
            api_key_security_scheme={
                "location": "header",
                "name": config.api_key_header,
            }
        )
    )
    return {
        "security_schemes": {"ingress": scheme},
        "security_requirements": [{"schemes": {"ingress": {"list": []}}}],
    }


class A2AAuthenticationMiddleware:
    """Check headers before routing or reading request bodies, including streams."""

    def __init__(self, app: Any, *, config: Any) -> None:
        validate_security(config)
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send) -> None:
        config = self.config
        public_card = (
            not config.card_auth_required
            and scope.get("path") == config.card_path
            and scope.get("method") in {"GET", "HEAD"}
        )
        if scope["type"] != "http" or config.auth_type == "none" or public_card:
            await self.app(scope, receive, send)
            return
        header = (
            "authorization"
            if config.auth_type == "bearer"
            else config.api_key_header.lower()
        )
        values = [
            value
            for key, value in scope.get("headers", [])
            if key.lower() == header.encode("ascii")
        ]
        credential = b""
        if len(values) == 1:
            credential = values[0]
            if config.auth_type == "bearer":
                scheme, separator, credential = credential.partition(b" ")
                if not separator or scheme.lower() != b"bearer":
                    credential = b""
        try:
            digest = hash_credential(credential.decode("ascii"))
        except ValueError:
            digest = ""
        if credential and hmac.compare_digest(digest, config.credential_hash):
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse

        response = JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={
                "WWW-Authenticate": "Bearer"
                if config.auth_type == "bearer"
                else 'ApiKey realm="a2a"',
                "Cache-Control": "no-store",
            },
        )
        await response(scope, receive, send)
