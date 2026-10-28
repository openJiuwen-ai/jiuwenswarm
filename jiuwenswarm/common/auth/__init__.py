# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""华为账号 Account Kit 登录（本地回环回调）+ APIG 免费模型。

用法速查::

    from jiuwenswarm.common.auth import get_auth_service, resolve_id_token

    service = get_auth_service()
    req = service.create_authorization_request()      # 打开 req["authorizeUrl"]
    ...                                               # 华为回调 → service.complete_callback(...)
    session = service.claim(req["state"], req["claimToken"])

    token = resolve_id_token()                        # 调 APIG：Authorization: Bearer <token>
"""

from importlib import import_module

# Request credential bookkeeping is also used by API-key models. Loading it
# must not construct the Account Kit / OAuth stack for ordinary chat.
_EXPORTS = {
    "AccountKitFlow": ".account_kit",
    "Credential": ".account_kit",
    "LoginOutcome": ".account_kit",
    "OAuthConfig": ".account_kit",
    "OAuthError": ".account_kit",
    "login_enabled": ".account_kit",
    "AuthService": ".service",
    "ModelAuthRequired": ".service",
    "get_auth_service": ".service",
    "live_session": ".service",
    "resolve_id_token": ".service",
    "AuthSession": ".session_store",
    "AuthSessionStore": ".session_store",
    "get_session_store": ".session_store",
}

__all__ = [
    "AccountKitFlow",
    "AuthService",
    "AuthSession",
    "AuthSessionStore",
    "Credential",
    "LoginOutcome",
    "ModelAuthRequired",
    "OAuthConfig",
    "OAuthError",
    "get_auth_service",
    "get_session_store",
    "live_session",
    "login_enabled",
    "resolve_id_token",
]


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
