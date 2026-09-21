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

from jiuwenswarm.common.auth.account_kit import (
    AccountKitFlow,
    Credential,
    LoginOutcome,
    OAuthConfig,
    OAuthError,
    login_enabled,
)
from jiuwenswarm.common.auth.service import (
    AuthService,
    ModelAuthRequired,
    get_auth_service,
    live_session,
    resolve_id_token,
)
from jiuwenswarm.common.auth.session_store import (
    AuthSession,
    AuthSessionStore,
    get_session_store,
)

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
