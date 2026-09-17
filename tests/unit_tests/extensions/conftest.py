# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared fixtures for AgentOS router unit tests.

After the gateway stopped auto-creating agent workspaces, every test that
relies on the default workspace root must point at a directory it can actually
create. CI (and local dev) run as non-root, so ``/home/agentos/users`` is not
writable. This module redirects the runtime default workspace root to a
per-test temporary directory and pre-creates the user directories used by the
tests.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def agentos_workspace_root(tmp_path: pytest.TempPathFactory) -> str:
    """Return a writable workspace root with the test user directories created."""
    root = tmp_path / "agentos_users"
    for user in ("u1", "alice", "default"):
        user_dir = root / user
        user_dir.mkdir(parents=True, exist_ok=True)
        try:
            user_dir.chmod(0o777)
        except OSError:
            pass
    return str(root)


@pytest.fixture(autouse=True)
def _agentos_workspace_root(
    agentos_workspace_root: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect the AgentOS default workspace root used at runtime to a temp dir.

    Only the runtime module namespace is patched. The ``DEFAULT_AGENT_WORKSPACE_ROOT``
    constant in ``config`` (used by ``load_router_config``) is left untouched so
    config-loading tests keep asserting the real default.
    """
    import jiuwenswarm.extensions.agentos.agentos_router.router_client as router_mod

    monkeypatch.setattr(router_mod, "DEFAULT_AGENT_WORKSPACE_ROOT", agentos_workspace_root)