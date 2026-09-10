# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for unit tests that mock ``workspace_{key}/`` tenant paths."""

from __future__ import annotations

from pathlib import Path


def tenant_workspace_key(
    service_id: str | None = "default",
    agent_id: str | None = "default",
) -> str:
    """Legacy helper name; derive a workspace_key label from routing ids."""
    sid = str(service_id or "default").strip() or "default"
    aid = str(agent_id or "default").strip() or "default"
    if sid == "default" and aid == "default":
        return "default"
    return f"{sid}_{aid}"


def tenant_workspace_root(
    tmp_path: Path,
    service_id: str | None = "default",
    agent_id: str | None = None,
    *,
    workspace_key: str | None = None,
) -> Path:
    """``tmp_path/workspace_{key}`` matching production multi-tenant layout.

    Compatible call styles:
    - ``tenant_workspace_root(tmp, sid, aid)`` → key ``{sid}_{aid}`` (or ``default``)
    - ``tenant_workspace_root(tmp, workspace_key)`` (positional as key)
    - ``tenant_workspace_root(tmp, workspace_key=...)``
    """
    if workspace_key is not None:
        wk = str(workspace_key or "default").strip() or "default"
    elif agent_id is None and service_id is not None and "/" not in str(service_id):
        # positional single-arg style: tenant_workspace_root(tmp, "mykey")
        # vs tenant_workspace_root(tmp, "default", "office")
        # When agent_id is None, treat first id as workspace_key if it doesn't look
        # like a pure service id pair call — keep legacy pair helper via both args.
        wk = str(service_id or "default").strip() or "default"
    else:
        wk = tenant_workspace_key(service_id, agent_id)
    return tmp_path / f"workspace_{wk}"


def patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path: Path) -> None:
    """Redirect tenant disk roots under ``tmp_path/workspace_{key}/``.

    Patches both ``utils`` and modules that ``from utils import get_multi_tenant...``
    (import binding), resets ``_workspace_base_dir`` cache, and clears tenant
    ContextVar bindings so tests cannot leak ``service_*/agent_*`` defaults.
    """
    import jiuwenswarm.common.utils as utils_mod

    try:
        from jiuwenswarm.server.runtime.tenant_context import clear_tenant_bindings

        clear_tenant_bindings()
    except ImportError:
        pass

    # Drop cached workspace base so get_user_workspace_dir sees the patch.
    monkeypatch.setattr(utils_mod, "_workspace_base_dir", None, raising=False)

    def _mock_user_workspace_dir() -> Path:
        return tmp_path

    def _mock_multi_tenant(workspace_key: str, agent_id: str | None = None) -> Path:
        # New API: one workspace_key. Old API: (service_id, agent_id).
        if agent_id is not None:
            wk = tenant_workspace_key(workspace_key, agent_id)
        else:
            wk = str(workspace_key or "default").strip() or "default"
        return tmp_path / f"workspace_{wk}"

    targets = [
        "jiuwenswarm.common.utils.get_user_workspace_dir",
        "jiuwenswarm.common.utils.get_multi_tenant_user_workspace_dir",
        "jiuwenswarm.gateway.tenant_paths.get_multi_tenant_user_workspace_dir",
        "jiuwenswarm.agents.harness.common.tools.cron.cron_tools.get_multi_tenant_user_workspace_dir",
    ]
    for target in targets:
        try:
            if target.endswith("get_user_workspace_dir"):
                monkeypatch.setattr(target, _mock_user_workspace_dir)
            else:
                monkeypatch.setattr(target, _mock_multi_tenant)
        except AttributeError:
            # Module may be absent in slim installs; ignore.
            pass
