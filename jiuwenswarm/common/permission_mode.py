# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（M1' 过渡形态）：真源已迁 ``swarm_base.permission_mode``。"""

from swarm_base.permission_mode import (  # noqa: F401
    AUTO_PERMISSION_MODE,
    MANUAL_PERMISSION_MODE,
    _VALID_RUNTIME_MODES,
    is_auto_permission_enabled,
    is_auto_permission_mode,
    is_permission_boundary_enabled,
    logger,
    resolve_permission_runtime_mode,
)
