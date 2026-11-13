"""The deployment seam: where the toolkit learns about the world outside itself.

Three facts the mechanism needs but must not hard-code -- where files live, what
the deployment's config says, whether a confirmation channel exists -- resolve
through this module. Each hook has a lazy default that reaches jiuwenswarm's own
config, so nothing changes for the product; the library extraction replaces the
defaults with its own config root and every other file stays untouched (§25.5,
cut ④).

Fail directions are per hook and deliberate: an unreadable config reads as "no
confirmation channel" (the floor must hold when its ground cannot be verified),
while an unresolvable workspace raises -- writing receipts to a guessed location
would scatter the ledger.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

_config_provider: Callable[[], dict[str, Any]] | None = None
_workspace_provider: Callable[[], Path] | None = None
_infra_config_provider: Callable[[], Path] | None = None
_agent_home_provider: Callable[[], Path] | None = None


def set_deployment(
    *,
    config: Callable[[], dict[str, Any]] | None = None,
    workspace: Callable[[], Path] | None = None,
    infra_config: Callable[[], Path] | None = None,
    agent_home: Callable[[], Path] | None = None,
) -> None:
    """Install the deployment's providers. Call once at wiring time; the lazy
    defaults below cover any caller that never does.
    """
    global _config_provider, _workspace_provider
    global _infra_config_provider, _agent_home_provider
    if config is not None:
        _config_provider = config
    if workspace is not None:
        _workspace_provider = workspace
    if infra_config is not None:
        _infra_config_provider = infra_config
    if agent_home is not None:
        _agent_home_provider = agent_home


def deployment_config() -> dict[str, Any]:
    if _config_provider is not None:
        return _config_provider() or {}
    from jiuwenswarm.common.config import get_config

    return get_config() or {}


def workspace_dir() -> Path:
    if _workspace_provider is not None:
        return _workspace_provider()
    from jiuwenswarm.common.utils import get_user_workspace_dir

    return get_user_workspace_dir()


def infra_config_dir() -> Path:
    """infra/config 分区（系统区）：clouddoc 状态文件落点。"""
    if _infra_config_provider is not None:
        return _infra_config_provider()
    from jiuwenswarm.common.utils import get_config_dir

    return get_config_dir()


def agent_home_dir() -> Path:
    """domain/agent 家目录（业务区）：旧版 clouddoc_state.json 位置。"""
    if _agent_home_provider is not None:
        return _agent_home_provider()
    from jiuwenswarm.common.utils import get_agent_home_dir

    return get_agent_home_dir()
