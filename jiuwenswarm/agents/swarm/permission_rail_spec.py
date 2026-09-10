# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PermissionInterruptRail provider for chat-team leader.

Registers a custom rail provider type that the SDK's :func:`apply_deep_agent_parts`
resolves to a live ``PermissionInterruptRail`` instance during
``DeepAgentSpec.resolve_parts``. No SDK changes needed — this is a
project-side mechanism using the existing ``register_rail_provider`` API.

Comparison vs the post-apply factory approach (openjiuwen.harness
.register_post_apply_rail_factory):

- This module: ~0 SDK lines, all logic in jiuwenswarm. RailSpec is resolved
  *before* the leader's ``Model`` instance is constructed, so ``llm`` must
  be passed as ``None``. Risk-based ASK (LLM-driven) will degrade, but
  config-driven ASK (write_file: ask) still works — which is what the
  original bug requires.
- Factory approach: ~80 SDK lines (the extension point + tests + docs),
  ~30 jiuwenswarm lines. Factory fires *after* ``agent.configure(...)``
  in ``apply_deep_agent_parts``, so the leader's live ``Model`` instance
  is available and can be passed as ``llm`` for risk-based ASK.

Both routes are project-revertable; pick the one whose trade-off matches
the team's appetite for SDK contributions.
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.schema.build_context import BuildContext
from openjiuwen.harness.schema.deep_agent_spec import register_rail_provider

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    apply_permission_trusted_dirs,
    build_permission_rail,
)
from jiuwenswarm.common.config import get_config

logger = logging.getLogger(__name__)

PERMISSION_RAIL_BUNDLE = "jiuwenswarm.permission_rail_bundle"


def _resolve_model_name() -> str:
    """Best-effort ``model_name`` derivation from jiuwenswarm config.

    The rail factory fires *before* the leader's ``Model`` instance is
    constructed, so we cannot reach into the agent for a live ``Model``.
    Fall back to ``config.models.default.model_client_config.model_name``;
    if even that is missing, use the literal ``"default"`` sentinel.
    """
    model_name = "default"
    try:
        config = get_config() or {}
        models_cfg = config.get("models", {}) if isinstance(config, dict) else {}
        defaults_cfg = (
            models_cfg.get("default", {})
            if isinstance(models_cfg, dict)
            else {}
        )
        mcc_cfg = (
            defaults_cfg.get("model_client_config", {})
            if isinstance(defaults_cfg, dict)
            else {}
        )
        cfg_model_name = (
            mcc_cfg.get("model_name") if isinstance(mcc_cfg, dict) else None
        )
        if cfg_model_name:
            model_name = str(cfg_model_name)
    except Exception as model_name_err:
        logger.debug(
            "[PermissionRail] model_name derivation fell back to "
            "'default': %s",
            model_name_err,
        )
    return model_name


def _build_permission_rail_bundle(
    params: dict[str, Any],
    context: BuildContext | None,
) -> list[Any]:
    """Factory: build a ``PermissionInterruptRail`` for chat-team leader.

    The factory returns a single-element list (or ``[]`` to skip). It is
    registered via :func:`register_rail_provider` so a ``RailSpec`` of
    ``type="jiuwenswarm.permission_rail_bundle"`` resolves to this
    function during ``DeepAgentSpec.resolve_parts``.

    Args:
        params: ``RailSpec.params`` dict. Expected key:
            - ``permissions_config`` (dict): the ``permissions`` sub-config
              from jiuwenswarm's ``config.yaml``. ``enabled`` is the master
              switch.
        context: Runtime ``BuildContext``. May carry ``session_id``,
            ``project_dir``, ``trusted_dirs`` (set by
            :func:`enrich_team_spec_for_swarm`).

    Returns:
        A single-element list with a fresh ``PermissionInterruptRail`` if
        the config gate is on; an empty list to opt out. Never raises.
    """
    permissions_config: dict[str, Any] = {}
    if isinstance(params, dict):
        permissions_config = params.get("permissions_config") or {}
        if not isinstance(permissions_config, dict):
            permissions_config = {}
    if not permissions_config.get("enabled"):
        return []

    session_id = "chat-team-leader"
    project_dir: str | None = None
    trusted_dirs: list[str] | None = None
    if context is not None:
        session_id = getattr(context, "session_id", None) or session_id
        project_dir = getattr(context, "project_dir", None)
        trusted_dirs = getattr(context, "trusted_dirs", None)

    model_name = _resolve_model_name()

    try:
        rail = build_permission_rail(
            config={"permissions": permissions_config},
            llm=None,
            model_name=model_name,
            session_id=str(session_id),
        )
    except Exception as build_err:
        logger.warning(
            "[PermissionRail] build_permission_rail failed for "
            "session_id=%s: %s",
            session_id,
            build_err,
        )
        return []

    if rail is None:
        return []

    try:
        if project_dir or trusted_dirs:
            apply_permission_trusted_dirs(
                rail,
                trusted_dirs=trusted_dirs,
                project_dir=project_dir,
            )
    except Exception as trusted_err:
        logger.debug(
            "[PermissionRail] apply_permission_trusted_dirs failed "
            "(non-fatal): %s",
            trusted_err,
        )

    return [rail]


_PROVIDERS_REGISTERED = False


def register_permission_rail_provider() -> None:
    """Idempotent registration. Safe to call multiple times per process."""
    global _PROVIDERS_REGISTERED
    if _PROVIDERS_REGISTERED:
        return
    register_rail_provider(PERMISSION_RAIL_BUNDLE, _build_permission_rail_bundle)
    _PROVIDERS_REGISTERED = True


def is_permission_rail_provider_registered() -> bool:
    """Test hook: expose registration state for unit tests."""
    return _PROVIDERS_REGISTERED
