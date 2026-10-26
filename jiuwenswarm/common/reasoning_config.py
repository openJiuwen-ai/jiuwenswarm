# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（M1' 过渡形态）：真源已迁 ``swarm_base.reasoning_config``。"""

from swarm_base.reasoning_config import *  # noqa: F401,F403
from swarm_base.reasoning_config import (  # noqa: F401
    ReasoningLevel,
    SAMPLING_OVERRIDE_RULES,
    SUPPORTED_REASONING_LEVELS,
    effective_endpoint_profile,
    normalize_reasoning_level,
    reasoning_config_for_level,
    reasoning_level_options,
    resolve_endpoint_profile_override,
    resolve_sampling_override,
    validate_reasoning_level_for_model,
)
