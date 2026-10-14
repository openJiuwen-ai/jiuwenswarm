# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Optional Auto-Harness exports with no evolving SDK import at package load."""
from importlib import import_module

_EXPORTS = {
    "AutoHarnessService": ".service",
    "ActiveAutoHarnessRun": ".service",
    "validate_harness_config_fields": ".service",
    "validate_harness_config_paths": ".service",
    "validate_harness_config": ".service",
    "reset_harness_packages_state": ".state",
    "Scheduler": ".scheduler",
    "TaskStore": ".task_store",
    "ConfigValidator": ".config_validator",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
