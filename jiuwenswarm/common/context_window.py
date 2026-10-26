# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（M1' 过渡形态）：真源已迁 ``swarm_base.context_window``。"""

from swarm_base.context_window import (  # noqa: F401
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    MAX_CONTEXT_WINDOW_TOKENS,
    _CONTEXT_WINDOW_PATTERN,
    _CONTEXT_WINDOW_UNIT_MULTIPLIERS,
    _as_mapping,
    _normalize_model_window_mapping,
    parse_positive_int,
    resolve_context_window_tokens,
)
