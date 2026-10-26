# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（M1' 过渡形态）：真源已迁 ``swarm_base.auth.service``。"""

from swarm_base.auth.service import (  # noqa: F401
    AuthService,
    ModelAuthRequired,
    REFRESH_AHEAD_S,
    _service,
    _service_lock,
    get_auth_service,
    live_session,
    logger,
    reset_auth_service_for_test,
    resolve_id_token,
)
