# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared SSL verification configuration for HTTP tools."""

from __future__ import annotations

import os
import ssl


def _env_bool(key: str, default: bool = True) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


def get_ssl_verify() -> bool:
    """Return whether SSL certificate verification is enabled."""
    return _env_bool("JIUWENCLAW_SSL_VERIFY", default=True)


def get_requests_verify() -> bool:
    """Return the verify kwarg value for requests calls."""
    return get_ssl_verify()


def get_insecure_ssl_context() -> ssl.SSLContext:
    """Return an SSL context that skips certificate verification."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx
