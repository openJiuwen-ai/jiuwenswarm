# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（auth 拆分过渡）：模型目录视图构建器，真源已迁
``gateway.auth.model_catalog``。

母仓消费方（config 视图、config_panel、agents 推荐）经本路径取用；
B3 拆仓时随 shim 收敛退役。
"""

from gateway.auth.model_catalog import *  # noqa: F401,F403
from gateway.auth.model_catalog import (  # noqa: F401
    Any,
    LOGIN_MODEL_SETTINGS_KEY,
    LOGIN_MODEL_SOURCE,
    LoginModel,
    Mapping,
    ModelAuthRequired,
    Path,
    _CACHE_FILE_NAME,
    _CACHE_TTL_S,
    _DISCOVERY_RETRY_AFTER_FAILURE_S,
    _blocked_models,
    _cache_path,
    _last_discovery_failure_at,
    _lock,
    _login_model_config_obj,
    _memo,
    _read_cache,
    _to_login_models,
    _write_cache,
    annotations,
    build_model_entry,
    clear_cache,
    dataclass,
    discover_models,
    field,
    get_auth_service,
    get_models,
    get_remote_config,
    is_login_model,
    json,
    list_login_model_entries,
    logger,
    logging,
    login_model_settings,
    model_whitelist,
    refresh,
    resolve_id_token,
    threading,
    time,
)
