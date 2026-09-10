# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SlimTools extension entry — discovered by ExtensionLoader at startup.

Gate: SLIM_TOOLS_ENABLED=1.
When enabled, runtime-patches the adapter to slim the tool surface:

1. wiki_ingest / wiki_query / wiki_lint — dropped from _get_tool_cards
2. acp_chat — dropped from _get_tool_cards
3. audio_metadata — metadata-only fallback retired (_iter_runtime_audio_tools)
4. powershell / list_files — SlimSysOperationRail replaces _build_filesystem_rail
5. list_files — removed from progressive eager-tools default
6. install_skill / uninstall_skill — folded into search_skill (SlimSkillToolkit)

Zero modification to any stock jiuwenswarm source file — all wiring is
monkey-patched here at startup, before any adapter is created.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PATCH_APPLIED = False

# Tool names dropped from registration (wiki三件套 + acp_chat)
_DROPPED_TOOL_NAMES = frozenset({
    "wiki_ingest",
    "wiki_query",
    "wiki_lint",
    "acp_chat",
})


def _is_enabled() -> bool:
    raw = os.getenv("SLIM_TOOLS_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _patch_get_tool_cards() -> None:
    """Wrap _get_tool_cards to filter out wiki/acp tool cards."""
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as _iface

    original = _iface.JiuWenSwarmDeepAdapter._get_tool_cards

    async def _patched_get_tool_cards(self, agent_id):
        tool_cards = await original(self, agent_id)
        before = len(tool_cards)
        tool_cards = [
            card for card in tool_cards
            if str(getattr(card, "name", "") or "") not in _DROPPED_TOOL_NAMES
        ]
        dropped = before - len(tool_cards)
        if dropped:
            logger.info("[SlimTools] dropped %d tool card(s) from registration", dropped)
        return tool_cards

    _iface.JiuWenSwarmDeepAdapter._get_tool_cards = _patched_get_tool_cards
    logger.info("[SlimTools] patched _get_tool_cards (drop wiki/acp)")


def _patch_audio_tools() -> None:
    """Retire the metadata-only audio fallback."""
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as _iface

    def _patched_iter_audio(self, agent_id=None):
        """Audio tools register only with a complete audio model config.

        The metadata-only fallback (audio_metadata without any model config)
        is retired: return nothing unless the full audio suite is configured.
        """
        if self._audio_model_config is None:
            return []
        from openjiuwen.harness.tools.multimodal import create_audio_tools

        return list(
            create_audio_tools(
                language=self._resolve_runtime_language(),
                audio_model_config=self._audio_model_config,
                agent_id=agent_id,
            )
        )

    _iface.JiuWenSwarmDeepAdapter._iter_runtime_audio_tools = _patched_iter_audio
    logger.info("[SlimTools] patched _iter_runtime_audio_tools (no metadata-only)")


def _patch_filesystem_rail() -> None:
    """Build SlimSysOperationRail instead of stock SysOperationRail."""
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as _iface
    from jiuwenswarm.extensions.slim_tools.SlimSysOperationRail import (
        SlimSysOperationRail,
    )

    def _patched_build_fs():
        try:
            rail = SlimSysOperationRail()
            logger.info(
                "[SlimTools] SlimSysOperationRail create success "
                "(powershell/list_files not registered)"
            )
            return rail
        except Exception as exc:
            logger.warning("[SlimTools] SlimSysOperationRail create failed: %s", exc)
            return None

    _iface.JiuWenSwarmDeepAdapter._build_filesystem_rail = staticmethod(_patched_build_fs)
    logger.info("[SlimTools] patched _build_filesystem_rail (slim rail)")


def _patch_eager_tools() -> None:
    """Remove list_files from the progressive eager-tools default."""
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as _iface

    original = _iface._normalize_progressive_eager_tools

    def _patched_normalize(value, default=None):
        result = original(value, default)
        if "list_files" in result:
            result = [name for name in result if name != "list_files"]
            logger.info("[SlimTools] removed list_files from eager tools")
        return result

    _iface._normalize_progressive_eager_tools = _patched_normalize
    logger.info("[SlimTools] patched _normalize_progressive_eager_tools (no list_files)")


def _patch_skill_toolkit() -> None:
    """Use SlimSkillToolkit (merged search_skill) instead of stock SkillToolkit."""
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as _iface
    from jiuwenswarm.extensions.slim_tools.SlimSkillToolkit import SlimSkillToolkit

    # Patch the module-level import so any `SkillToolkit(...)` call in
    # interface_deep resolves to SlimSkillToolkit.
    _iface.SkillToolkit = SlimSkillToolkit
    logger.info("[SlimTools] patched SkillToolkit → SlimSkillToolkit (merged search_skill)")


async def register_extensions(registry):
    """ExtensionLoader entry point."""
    global _PATCH_APPLIED

    if not _is_enabled():
        logger.info("[SlimTools] disabled (SLIM_TOOLS_ENABLED not set)")
        return []

    if _PATCH_APPLIED:
        return []

    try:
        _patch_get_tool_cards()
        _patch_audio_tools()
        _patch_filesystem_rail()
        _patch_eager_tools()
        _patch_skill_toolkit()
        _PATCH_APPLIED = True
        logger.info("[SlimTools] switch ARMED: tool surface slimming enabled")
    except Exception as exc:
        logger.warning("[SlimTools] setup failed: %s", exc)

    return []
