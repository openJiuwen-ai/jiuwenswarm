# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Control-plane method sets. Import-safe for Front classification."""

from __future__ import annotations

from jiuwenswarm.common.schema.message import ReqMethod

CONTROL_HEALTH_METHODS = frozenset({ReqMethod.HEALTH_CHECK_GET_CONF.value})

CONTROL_SESSION_METHODS = frozenset(
    {
        ReqMethod.SESSION_LIST.value,
        ReqMethod.SESSION_GET_METADATA.value,
        ReqMethod.SESSION_PIN.value,
        ReqMethod.SESSION_COLOR_SET.value,
        ReqMethod.SESSION_PREVIEW.value,
    }
)
# Session create/switch/delete stay on the execution plane in phase 1:
# they still claim/prewarm Agent Runtime rather than writing metadata only.

CONTROL_PROJECT_METHODS = frozenset(
    {
        ReqMethod.PROJECT_INFO.value,
        ReqMethod.PROJECT_PINNED_SESSIONS.value,
        ReqMethod.PROJECT_GET_SESSIONS.value,
        ReqMethod.PROJECT_GET_CRON_SESSIONS.value,
        ReqMethod.PROJECT_CRON_RESOLVE_BINDING.value,
        ReqMethod.PROJECT_LIST.value,
    }
)
# Project create/rename/pin and all git operations stay on the execution plane.

CONTROL_CONFIG_METHODS = frozenset(
    {
        ReqMethod.CONFIG_GET.value,
        ReqMethod.LOCALE_GET_CONF.value,
        ReqMethod.PATH_GET.value,
        ReqMethod.MEMORY_FORBIDDEN_GET.value,
    }
)

CONTROL_HISTORY_METHODS = frozenset({ReqMethod.HISTORY_GET.value})

ALL_CONTROL_METHODS = (
    CONTROL_HEALTH_METHODS
    | CONTROL_SESSION_METHODS
    | CONTROL_PROJECT_METHODS
    | CONTROL_CONFIG_METHODS
    | CONTROL_HISTORY_METHODS
)
