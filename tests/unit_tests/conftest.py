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
