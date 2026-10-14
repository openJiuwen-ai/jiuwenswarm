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
    **{
        name: ".account_kit"
        for name in (
            "AccountKitFlow",
            "Credential",
            "LoginOutcome",
            "OAuthConfig",
            "OAuthError",
            "login_enabled",
        )
    },
    **{
        name: ".service"
        for name in (
            "AuthService",
            "ModelAuthRequired",
            "get_auth_service",
            "live_session",
            "resolve_id_token",
        )
    },
    **{
        name: ".session_store"
        for name in ("AuthSession", "AuthSessionStore", "get_session_store")
    },
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
