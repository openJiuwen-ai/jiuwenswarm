# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility re-export for the shared model-routing registry."""

from jiuwenswarm.common.model_routing_registry import (  # noqa: F401
    ModelExecutionContext,
    ModelSelectionResolver,
)

__all__ = ["ModelExecutionContext", "ModelSelectionResolver"]
