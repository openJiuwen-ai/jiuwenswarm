# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Swarm assembly. Providers register at enrichment, before use."""
from importlib import import_module

_EXPORTS = {
    "enrich_team_spec_for_swarm": ".assembly",
    "preflight_team_mcps": ".assembly",
    "SwarmBuildContext": ".context",
    "register_swarm_providers": ".registry",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
