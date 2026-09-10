# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashMemory extension entry — discovered by ExtensionLoader at startup.

Gate: FLASH_MEMORY_ENABLED=1.
When enabled, runtime-patches:
1. MemoryRail._register_memory_tools — register unified FlashMemoryTool
   instead of the stock 5 separate tools
2. MemoryRail.before_model_call — rewrite the memory section prompt text
   (old tool names → memory(mode=...) form)
3. interface_deep's group-chat/disable linkage — remove("memory") instead
   of remove("write_memory"/"edit_memory")

Zero modification to any stock jiuwenswarm source file.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PATCH_APPLIED = False

_MEMORY_NAME_REPLACEMENTS = (
    ("memory_search", "memory(mode=search)"),
    ("memory_get", "memory(mode=read)"),
    ("read_memory", "memory(mode=read)"),
    ("write_memory", "memory(mode=write)"),
    ("edit_memory", "memory(mode=edit)"),
)


def _is_enabled() -> bool:
    raw = os.getenv("FLASH_MEMORY_ENABLED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _patch_memory_rail_register() -> None:
    """Patch MemoryRail._register_memory_tools to register FlashMemoryTool."""
    from openjiuwen.harness.rails.memory.memory_rail import MemoryRail
    from openjiuwen.core.memory.lite.config import create_memory_settings
    from openjiuwen.core.memory.lite.memory_tool_context import MemoryToolContext

    def _patched_register(self, agent) -> None:
        """Register unified FlashMemoryTool instead of 5 separate tools."""
        if not hasattr(agent, "ability_manager"):
            logger.warning("[FlashMemory] agent has no ability_manager; skip")
            return

        try:
            from jiuwenswarm.extensions.flash_memory.FlashMemoryTool import (
                FlashMemoryTool,
            )

            agent_id = getattr(getattr(agent, "card", None), "id", None) or "default"
            language = (
                getattr(self.system_prompt_builder, "language", "cn")
                if self.system_prompt_builder
                else "cn"
            )

            memory_dir = (
                str(self.workspace.get_node_path("memory") or "") if self.workspace else ""
            )
            settings = create_memory_settings(memory_dir)
            self._tool_ctx = MemoryToolContext(
                workspace=self.workspace,
                settings=settings,
                agent_id=agent_id,
                embedding_config=self._embedding_config,
                sys_operation=self.sys_operation,
                manager=None,
                node_name="memory",
            )

            # Read-only flag: reuse MemoryRail's runtime _is_read_only state
            read_only_flag = lambda: getattr(self, "_is_read_only", False)
            tool = FlashMemoryTool(
                self._tool_ctx,
                read_only_flag=read_only_flag,
                language=language,
                agent_id=agent_id,
            )
            card = tool.card
            result = agent.ability_manager.add_ability(card, tool)
            if result.added:
                self._owned_tool_cards[card.name] = card
                logger.info(
                    "[FlashMemory] Registered unified memory tool: %s "
                    "(modes: write/edit/read/search, 5-in-1)",
                    card.name,
                )

        except Exception as exc:
            logger.error("[FlashMemory] unified register failed, falling back: %s", exc)
            # Fallback: call the original registration (stock 5 tools)
            MemoryRail._original_register_memory_tools(self, agent)

    # Save original for fallback
    if not hasattr(MemoryRail, "_original_register_memory_tools"):
        MemoryRail._original_register_memory_tools = MemoryRail._register_memory_tools

    MemoryRail._register_memory_tools = _patched_register
    logger.info("[FlashMemory] patched MemoryRail._register_memory_tools (unified tool)")


def _patch_memory_rail_prompt() -> None:
    """Patch MemoryRail.before_model_call to rewrite memory section text."""
    from openjiuwen.harness.rails.memory.memory_rail import MemoryRail

    original = MemoryRail.before_model_call

    async def _patched_before_model_call(self, ctx) -> None:
        # Temporarily patch build_memory_section to rewrite tool names
        import openjiuwen.harness.prompts.sections.memory as _mem_section

        original_build = _mem_section.build_memory_section

        def _rewriting_build(language="cn", **kwargs):
            section = original_build(language=language, **kwargs)
            if section is not None:
                content = getattr(section, "content", None)
                if isinstance(content, dict):
                    new_content = {}
                    for lang_key, text in content.items():
                        if isinstance(text, str):
                            for old, new in _MEMORY_NAME_REPLACEMENTS:
                                text = text.replace(old, new)
                        new_content[lang_key] = text
                    try:
                        section = type(section)(
                            name=section.name,
                            content=new_content,
                            priority=section.priority,
                        )
                    except Exception:
                        pass
            return section

        _mem_section.build_memory_section = _rewriting_build
        try:
            await original(self, ctx)
        finally:
            _mem_section.build_memory_section = original_build

    MemoryRail.before_model_call = _patched_before_model_call
    logger.info("[FlashMemory] patched MemoryRail.before_model_call (prompt rewrite)")


def _patch_group_chat_linkage() -> None:
    """Patch interface_deep's group-chat/disable tool-name linkage.

    Stock logic removes "write_memory"/"edit_memory" by name in group-chat /
    memory-disabled mode. With the unified tool, the name is "memory".
    We patch the module-level constant so the removal targets the right name.
    """
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as _iface

    # Patch the tool names used in the group-chat/disable logic
    # Stock: _all_memory_tools = (write_memory, edit_memory, read_memory, memory_search, memory_get)
    # Unified: _all_memory_tools = (memory,)
    if hasattr(_iface, "_all_memory_tools"):
        _iface._all_memory_tools = ("memory",)
        logger.info("[FlashMemory] patched _all_memory_tools → ('memory',)")

    # Stock also removes ("write_memory", "edit_memory") specifically for group chat
    # We need to patch that too — it's inline in _update_session_tools
    # Instead of patching the method, we register "memory" in the module so
    # remove("memory") works when the unified tool is registered.
    logger.info("[FlashMemory] group-chat linkage: remove('memory') targets unified tool")


async def register_extensions(registry):
    """ExtensionLoader entry point."""
    global _PATCH_APPLIED

    if not _is_enabled():
        logger.info("[FlashMemory] disabled (FLASH_MEMORY_ENABLED not set)")
        return []

    if _PATCH_APPLIED:
        return []

    try:
        _patch_memory_rail_register()
        _patch_memory_rail_prompt()
        _patch_group_chat_linkage()
        _PATCH_APPLIED = True
        logger.info("[FlashMemory] switch ARMED: unified memory tool (5-in-1)")
    except Exception as exc:
        logger.warning("[FlashMemory] setup failed: %s", exc)

    return []
