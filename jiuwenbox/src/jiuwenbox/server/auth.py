# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in Bearer token authentication for the jiuwenbox HTTP API."""

from __future__ import annotations

import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_ENV_API_TOKEN_NAME = "JIUWENBOX_API_TOKEN"


def get_configured_token() -> str | None:
    """Return the configured API token, or ``None`` when auth is disabled."""
    raw = os.environ.get(_ENV_API_TOKEN_NAME)
    if raw is None:
        return None
    token = raw.strip()
    return token or None


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    stripped = token.strip()
    return stripped or None


def token_is_valid(provided: str | None, expected: str) -> bool:
    if provided is None:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8"),
        expected.encode("utf-8"),
    )


class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <token>`` when the API token env is set."""

    async def dispatch(self, request: Request, call_next) -> Response:
        expected = get_configured_token()
        if expected is None:
            return await call_next(request)

        provided = extract_bearer_token(request.headers.get("Authorization"))
        if not token_is_valid(provided, expected):
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
