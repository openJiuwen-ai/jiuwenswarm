# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：模型路由解析已下沉 ``jiuwenswarm.common.model_routing_registry``。"""

from jiuwenswarm.common.model_routing_registry import (  # noqa: F401
    ModelExecutionContext,
    ModelSelectionResolver,
)

__all__ = ["ModelExecutionContext", "ModelSelectionResolver"]
