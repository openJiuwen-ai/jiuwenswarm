# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（M1' 过渡形态）：真源已迁 ``swarm_base.model_config_validation``。"""

from swarm_base.model_config_validation import *  # noqa: F401,F403
from swarm_base.model_config_validation import (  # noqa: F401
    EXAMPLE_DOMAINS,
    FORBIDDEN_REQUEST_KEYS,
    ModelProbeResult,
    PLACEHOLDER_API_BASES,
    PLACEHOLDER_API_KEYS,
    PLACEHOLDER_MODEL_NAMES,
    is_placeholder_api_base,
    is_placeholder_model_entry,
    model_client_config_view,
    probe_model_connection,
    raise_if_invalid,
    validate_models_config,
)
