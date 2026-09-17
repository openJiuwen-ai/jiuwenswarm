#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""统一的成功/失败 JSON 生成函数，供所有脚本复用。"""
from __future__ import annotations

from typing import Any


def make_failure(stage: str, message: str, **extra: Any) -> dict[str, Any]:
    """生成统一格式的失败 JSON。"""
    payload: dict[str, Any] = {
        "ok": False,
        "stage": stage,
        "error": message,
    }
    payload.update(extra)
    return payload


def make_success(stage: str, **extra: Any) -> dict[str, Any]:
    """生成统一格式的成功 JSON。"""
    payload: dict[str, Any] = {"ok": True, "stage": stage}
    payload.update(extra)
    return payload
