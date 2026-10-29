# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（auth 拆分过渡）：AuthService 登录栈（OAuth 流程 + 回调 + claim）真源已迁 ``gateway.auth.service``。

server/agents 侧消费方经本路径取用（拍板 #10 方向：母仓反向依赖
gateway 仓）；B3 拆仓时随 shim 收敛退役。
"""

from gateway.auth.service import *  # noqa: F401,F403
from gateway.auth.service import (  # noqa: F401
    AccountKitFlow,
    Any,
    AuthService,
    AuthSession,
    AuthSessionStore,
    CLAIM_POLL_INTERVAL_S,
    CLAIM_POLL_SLOW_AFTER_S,
    CLAIM_POLL_SLOW_INTERVAL_S,
    ModelAuthRequired,
    OAuthError,
    REFRESH_AHEAD_S,
    STATE_TTL_S,
    _service,
    _service_lock,
    annotations,
    campaign_state,
    get_auth_service,
    get_session_store,
    live_session,
    logger,
    logging,
    login_enabled,
    mask,
    reset_auth_service_for_test,
    resolve_id_token,
    threading,
    time,
)
