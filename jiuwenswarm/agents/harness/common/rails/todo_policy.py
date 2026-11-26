# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""产品模式 todo rail 的统一注册参数。"""

from __future__ import annotations


def build_product_task_planning_kwargs() -> dict[str, bool]:
    """返回产品模式 TaskPlanningRail 的 todo 校验参数。"""
    return {"require_explicit_status": True}


__all__ = ["build_product_task_planning_kwargs"]
