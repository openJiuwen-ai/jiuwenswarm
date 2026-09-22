"""ModelRoutingRail package — 四档模式（写死）模型路由。"""
from __future__ import annotations

from .capability import ModelCapability, build_capability_table_from_config
from .model_routing_rail import ModelRoutingRail

__all__ = [
    "ModelRoutingRail",
    "ModelCapability",
    "build_capability_table_from_config",
]
