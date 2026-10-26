# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（M1' 过渡形态）：真源已迁 ``swarm_base.model_vendor_registry``。"""

from swarm_base.model_vendor_registry import *  # noqa: F401,F403
from swarm_base.model_vendor_registry import (  # noqa: F401
    ANTHROPIC_CLIENT_PROVIDER,
    PlanKind,
    VendorPreset,
    get_all_presets,
    get_preset,
    get_preset_by_vendor_key,
    get_presets_by_plan,
    to_frontend_payload,
)
