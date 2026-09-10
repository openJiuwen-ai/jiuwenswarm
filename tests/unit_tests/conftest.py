# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Test-wide hooks for the unit_tests tree.

`agentserver/memory/test_external_memory_*.py` must patch
`jiuwenswarm.utils.get_config_file` and `get_agent_workspace_dir` before
importing the modules under test (so `jiuwenswarm.agentserver.memory.config` sees
a stable path at import). Those two files are collected early (under
`agentserver/...`), and their import-time patch would otherwise leak: other
modules then hit ``Path / "memory"`` with a ``str`` path or, after switching to
``Path`` stubs, return values that do not match ``test_utils`` expectations.

We restore the real callables before every test that is *not* in those two
files, and re-apply the same ``Path`` stubs for tests that *are* (in case
a previous test left the real callables in place). Path stubs are safe for
``get_agent_memory_dir()``-style operations even if they briefly leak to other
tests, but the assertions in ``TestPathResolution`` require the real
functions, hence this hook.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import jiuwenswarm.common.utils as _utils

_REAL_GET_CONFIG_FILE = _utils.get_config_file
_REAL_GET_AGENT_WORKSPACE_DIR = _utils.get_agent_workspace_dir

# Must match the stubs used in test_external_memory_{builder,config}.py
_STUB_CONFIG = Path("/tmp/test_config.yaml")
_STUB_WORKSPACE = Path("/tmp/test_workspace")

_EXTERNAL_MEMORY_BASENAMES = frozenset(
    {
        "test_external_memory_builder.py",
        "test_external_memory_config.py",
    }
)


def _stub_get_config_file() -> Path:
    return _STUB_CONFIG


def _stub_get_agent_workspace_dir() -> Path:
    return _STUB_WORKSPACE


def _is_external_memory_patched_module(node_path) -> bool:
    base = os.path.basename(str(node_path or ""))
    return base in _EXTERNAL_MEMORY_BASENAMES


def pytest_runtest_setup(item) -> None:
    path = getattr(item, "path", None) or getattr(item, "fspath", None)
    p = str(path) if path is not None else ""
    if _is_external_memory_patched_module(p):
        _utils.get_config_file = _stub_get_config_file
        _utils.get_agent_workspace_dir = _stub_get_agent_workspace_dir
    else:
        _utils.get_config_file = _REAL_GET_CONFIG_FILE
        _utils.get_agent_workspace_dir = _REAL_GET_AGENT_WORKSPACE_DIR


@pytest.fixture(autouse=True)
def _reset_local_env_config_state() -> None:
    """Reset the process Track-B tip/baseline so tests are isolated.

    ``stage_env_overrides`` / ``apply_env_overrides_to_active`` (e.g. via the
    agent manager) persist into the shared ``default/default`` tip bag; without
    a reset a prior test's business keys (MODEL_NAME, API_KEY, ...) shadow
    fresh ``monkeypatch.setenv`` values read by ``get_local_config``.
    """
    from jiuwenswarm.common.local_env_config import reset_local_env_state_for_tests
    from jiuwenswarm.server.runtime.tenant_context import clear_tenant_bindings

    reset_local_env_state_for_tests()
    clear_tenant_bindings()
    yield
    reset_local_env_state_for_tests()
    clear_tenant_bindings()


def patch_handler_name(monkeypatch, name, value):
    import importlib
    import pkgutil

    from jiuwenswarm.server import agent_ws_server as _ws_mod
    from jiuwenswarm.server import handlers as _handlers_pkg

    mods = [_ws_mod]
    for info in pkgutil.iter_modules(_handlers_pkg.__path__):
        mods.append(importlib.import_module(f"{_handlers_pkg.__name__}.{info.name}"))

    hit = False
    for mod in mods:
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, value)
            hit = True
    assert hit, f"没有任何目标模块定义 {name!r}，patch 会静默失效"
