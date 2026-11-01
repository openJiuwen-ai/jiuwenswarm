# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Manager → Gateway 同步请求解析（剥离历史信封字段，无签密）。"""

from __future__ import annotations

from typing import Any

# 历史信封字段，不进入业务落库。``revision`` 也是 A2A 策略业务字段，
# 由各显式 schema 决定是否接收，不能在这里全局剥离。
_LEGACY_KEYS = frozenset({"sig", "enc"})


def split_business(body: dict[str, Any]) -> dict[str, Any]:
    """返回业务字段；忽略历史 ``sig`` / ``enc``。"""
    return {k: v for k, v in body.items() if k not in _LEGACY_KEYS}
