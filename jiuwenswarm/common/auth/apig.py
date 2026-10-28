# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（auth 拆分过渡）：APIG 免费模型与配额真源已迁 ``gateway.auth.apig``。

server/agents 侧消费方经本路径取用（拍板 #10 方向：母仓反向依赖
gateway 仓）；B3 拆仓时随 shim 收敛退役。
"""

from gateway.auth.apig import *  # noqa: F401,F403
from gateway.auth.apig import (  # noqa: F401
    Any,
    ApigConfig,
    ApigError,
    LOGIN_REQUIRED_CODE,
    LOW_BALANCE_RATIO,
    ModelQuota,
    QUOTA_EXHAUSTED_CODE,
    QuotaExhausted,
    RATE_LIMITED_CODE,
    SERVICE_UNAVAILABLE_CODE,
    _APIG_CODE_MAP,
    _APIG_CODE_RE,
    _QUOTA_EXHAUSTED_CODES,
    _QUOTA_EXHAUSTED_STATUSES,
    _QUOTA_TEXT_HINTS,
    _RATE_LIMIT_TEXT_HINTS,
    _SERVICE_TEXT_HINTS,
    _STATUS_RE,
    _TIMEOUT_S,
    _apig_error_code,
    _error_detail,
    _extract_model_items,
    _join,
    _looks_exhausted,
    _optional_float,
    _pick,
    _request,
    annotations,
    build_auth_headers,
    classify_model_error,
    dataclass,
    fetch_models,
    fetch_quota,
    field,
    get_remote_config,
    logger,
    logging,
    parse_quota,
    re,
    requests_request,
    resolve_apig_config,
)
