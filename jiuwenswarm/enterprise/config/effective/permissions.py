# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for applying enterprise permissions templates."""

from __future__ import annotations

import copy
from typing import Any


def resolve_permissions_body_from_enterprise(
    enterprise_config: Any,
) -> dict[str, Any] | None:
    """从企业配置 ``permissions`` 槽位取首个启用模板的 ``body``。"""
    if enterprise_config is None:
        return None
    templates = getattr(enterprise_config, "permissions", None)
    if not isinstance(templates, list):
        return None
    for tpl in templates:
        if not isinstance(tpl, dict):
            continue
        if tpl.get("enabled") is False:
            continue
        body = tpl.get("body")
        if isinstance(body, dict):
            return copy.deepcopy(body)
    return None
