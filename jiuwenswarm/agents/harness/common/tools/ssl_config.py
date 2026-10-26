# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared SSL verification configuration for HTTP tools."""

from __future__ import annotations

import ssl

from jiuwenswarm.common.http_proxy_config import resolve_requests_verify, ssl_verify_enabled


def get_ssl_verify() -> bool:
    """Return whether SSL certificate verification is enabled."""
    return ssl_verify_enabled(default=True)


def get_requests_verify() -> bool | str:
    """Return the verify kwarg value for requests calls.

    Delegates to :func:`jiuwenswarm.common.http_proxy_config.resolve_requests_verify`
    so CA-bundle and tip/spawn SSL flags stay consistent with ``requests_request``.
    """
    return resolve_requests_verify()


def get_insecure_ssl_context() -> ssl.SSLContext:
    """Return an SSL context that skips certificate verification."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx
