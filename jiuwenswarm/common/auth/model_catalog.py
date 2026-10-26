# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（M1' 过渡形态）：真源已迁 ``swarm_base.auth.model_catalog``。"""

from swarm_base.auth.model_catalog import (  # noqa: F401
    LOGIN_MODEL_SETTINGS_KEY,
    LOGIN_MODEL_SOURCE,
    LoginModel,
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
    build_model_entry,
    clear_cache,
    discover_models,
    get_models,
    is_login_model,
    list_login_model_entries,
    logger,
    login_model_settings,
    model_whitelist,
    refresh,
)
