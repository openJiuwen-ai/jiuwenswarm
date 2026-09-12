# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight MACRO scheduler (LAS-inspired): Agent / Cluster."""

from jiuwenswarm.agents.harness.macro_routing.config import load_macro_routing_config
from jiuwenswarm.agents.harness.macro_routing.gate import route_with_gate
from jiuwenswarm.agents.harness.macro_routing.router import route_macro_mode
from jiuwenswarm.agents.harness.macro_routing.schemas import (
    MacroRoutingDecision,
    is_auto_mode,
    macro_mode_label,
    normalize_macro_mode,
)

__all__ = [
    "MacroRoutingDecision",
    "is_auto_mode",
    "load_macro_routing_config",
    "macro_mode_label",
    "normalize_macro_mode",
    "route_macro_mode",
    "route_with_gate",
]
