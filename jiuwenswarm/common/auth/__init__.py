# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（M1' 过渡形态）：真源已迁 ``swarm_base.auth``。"""

from swarm_base.auth import (  # noqa: F401
    AccountKitFlow,
    AuthService,
    AuthSession,
    AuthSessionStore,
    Credential,
    LoginOutcome,
    ModelAuthRequired,
    OAuthConfig,
    OAuthError,
    _EXPORTS,
    get_auth_service,
    get_session_store,
    live_session,
    login_enabled,
    resolve_id_token,
)
