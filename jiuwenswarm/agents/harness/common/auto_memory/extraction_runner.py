# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Auto Memory Extraction Runner - Memory extraction subagent functions.

This module provides functions for extracting memories from conversations:
- _MemoryExtractionContext: Context dataclass for extraction resources
- _create_memory_extraction_context: Workspace/SysOperation/ToolContext creation
- _execute_auto_memory_extraction: Main entry point for extraction
- _run_simple_memory_extraction: Standalone subagent extraction
- _run_memory_extraction_with_cache_sharing: Cache-sharing extraction mode
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openjiuwen.core.context_engine.engine import ContextEngine
    from jiuwenswarm.server.runtime.agent_adapter.agent_adapters import AgentAdapter
    from jiuwenswarm.common.schema.agent import AgentRequest

logger = logging.getLogger(__name__)


@dataclass
class _MemoryExtractionContext:
    """Context object for memory extraction subagents.

    Contains all shared resources needed for memory extraction:
    - Workspace for file operations
    - SysOperation for sandbox restrictions
    - CodingMemoryToolContext for memory tools
    - Memory tools for write/edit/read operations
    """

    workspace: Any  # Workspace
    sys_operation: Any  # SysOperation
    sysop_id: str
    tool_ctx: Any  # CodingMemoryToolContext
    memory_tools: list[Any]

    def cleanup(self) -> None:
        """Remove SysOperation to free resources."""
        from openjiuwen.core.runner.runner import Runner
        try:
            Runner.resource_mgr.remove_sys_operation(self.sysop_id)
        except Exception as cleanup_err:
            logger.warning("[auto_memory] Cleanup failed: %s", cleanup_err)


async def _create_memory_extraction_context(
    memory_dir: Path,
    agent_id: str = "auto_memory_subagent",
    sysop_id_prefix: str = "extract_memories_",
) -> _MemoryExtractionContext | None:
    """Create shared context for memory extraction subagents.

    This function creates all shared resources needed for memory extraction:
    - Workspace pointing to memory_dir
    - SysOperation restricted to memory_dir
    - CodingMemoryToolContext for vector-indexed memory tools
    - Coding memory tools (write/edit/read)

    Args:
        memory_dir: Path to the coding memory directory.
        agent_id: Agent ID for the tool context.
        sysop_id_prefix: Prefix for SysOperation ID (default: "extract_memories_").

    Returns:
        _MemoryExtractionContext if successful, None if SysOperation creation fails.
    """
    from openjiuwen.core.runner.runner import Runner
    from openjiuwen.core.sys_operation import SysOperation, SysOperationCard, OperationMode
    from openjiuwen.core.sys_operation.config import LocalWorkConfig
    from openjiuwen.harness.workspace.workspace import Workspace
    from openjiuwen.core.memory.lite.coding_memory_tool_context import CodingMemoryToolContext
    from openjiuwen.core.memory.lite.config import create_memory_settings
    from openjiuwen.harness.tools.coding_memory import create_coding_memory_tools

    # Ensure memory directory exists
    memory_dir.mkdir(parents=True, exist_ok=True)

    # Create Workspace
    workspace = Workspace(root_path=str(memory_dir), language="cn")
    workspace.set_directory({
        "name": "coding_memory",
        "path": ".",  # Points to root_path (memory_dir), not a subdirectory
        "type": "directory",
    })

    # Create SysOperation restricted to memory directory
    sysop_id = f"{sysop_id_prefix}{int(time.time())}"
    sysop_card = SysOperationCard(
        id=sysop_id,
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(
            sandbox_root=[str(memory_dir)],
            restrict_to_sandbox=True,
        ),
    )

    # Register SysOperation
    add_result = Runner.resource_mgr.add_sys_operation(sysop_card)
    if add_result.is_err():
        logger.error("[auto_memory] Failed to add SysOperation: %s", add_result.msg())
        return None

    sys_operation = Runner.resource_mgr.get_sys_operation(sysop_id)
    if sys_operation is None:
        logger.error("[auto_memory] Failed to get SysOperation")
        return None

    # Create CodingMemoryToolContext
    settings = create_memory_settings(str(memory_dir))
    tool_ctx = CodingMemoryToolContext(
        workspace=workspace,
        settings=settings,
        agent_id=agent_id,
        embedding_config=None,
        sys_operation=sys_operation,
        coding_memory_dir=str(memory_dir),
        manager=None,
        node_name="",  # Empty to write directly to memory_dir, not nested subdir
    )

    # Create coding memory tools
    memory_tools = create_coding_memory_tools(tool_ctx, language="cn")
    tool_ctx.coding_memory_dir = str(memory_dir)

    # Create .workspace marker file
    workspace_marker = memory_dir / ".workspace"
    if not workspace_marker.exists():
        workspace_marker.write_text("", encoding="utf-8")

    return _MemoryExtractionContext(
        workspace=workspace,
        sys_operation=sys_operation,
        sysop_id=sysop_id,
        tool_ctx=tool_ctx,
        memory_tools=memory_tools,
    )


async def _execute_auto_memory_extraction(
    project_dir: str,
    session_id: str,
    messages: list | None = None,
    parent_agent: "AgentAdapter | None" = None,
) -> None:
    """Execute auto memory extraction in background.

    This is a fire-and-forget helper that wraps execute_extract_memories.

    Args:
        project_dir: The project root path.
        session_id: The session ID.
        messages: Pre-retrieved messages from session history.
        parent_agent: The parent AgentAdapter instance (for cache sharing, optional).
    """
    # Import utility functions from extract_memories module
    from jiuwenswarm.agents.harness.common.auto_memory.extract_memories import (
        _check_coding_memory_write_in_history,
        scan_memory_files,
    )

    try:
        logger.debug("[auto_memory] Extraction task started")

        # Validate project_dir: must be an existing directory
        if not project_dir:
            logger.warning("[auto_memory] Skipped: project_dir is empty")
            return

        project_path = Path(project_dir)
        if not project_path.exists():
            logger.warning("[auto_memory] Skipped: project_dir does not exist")
            return

        if not project_path.is_dir():
            logger.warning("[auto_memory] Skipped: project_dir is not a directory")
            return

        # Use pre-retrieved messages from session history
        history = []
        if messages is not None:
            logger.info("[auto_memory] Using pre-retrieved messages (%d messages)", len(messages))
            history = messages
        else:
            logger.warning("[auto_memory] No messages provided, cannot extract memories")
            return

        if not history:
            logger.debug("[auto_memory] Skipped: no conversation messages found")
            return

        # Mutex detection: check if main agent used coding_memory_write
        # If so, skip auto memory subagent (main agent already handled memory)
        if _check_coding_memory_write_in_history(history):
            logger.info(
                "[auto_memory] Skipped: main agent used coding_memory_write (mutex detection)"
            )
            return

        # Count new messages (this turn's exchange)
        # Typically 1 user message + 1 assistant message per turn
        new_messages_count = 2  # Simplified count

        # Get coding memory directory (unified with Auto Memory)
        from jiuwenswarm.common.utils import get_agent_workspace_dir
        from jiuwenswarm.common.coding_memory_paths import resolve_project_coding_memory_dir
        memory_dir = Path(resolve_project_coding_memory_dir(
            agent_workspace_dir=get_agent_workspace_dir(),
            project_dir=project_dir,
        ))

        # Ensure memory directory exists
        memory_dir.mkdir(parents=True, exist_ok=True)
        memory_md = memory_dir / "MEMORY.md"
        if not memory_md.exists():
            memory_md.write_text(
                "# Project Auto Memory Index\n\n"
                "# This file is automatically updated by extractMemories.\n"
                "# Each entry links to a memory file.\n\n",
                encoding="utf-8",
            )

        # Scan existing memories
        existing_memories = scan_memory_files(memory_dir)
        logger.debug("[auto_memory] Existing memories: %d files", len(existing_memories))

        # Build extraction prompt
        from jiuwenswarm.agents.harness.common.auto_memory.prompts import build_extract_memories_prompt
        prompt = build_extract_memories_prompt(
            new_messages_count=new_messages_count,
            existing_memories=existing_memories,
            language="zh",
        )

        # Run memory extraction with cache sharing mode
        # Cache Sharing: inherit parent's model, tools, message prefix for API cache hit
        await _run_memory_extraction_with_cache_sharing(
            memory_dir=memory_dir,
            prompt=prompt,
            history=history,
            existing_memories=existing_memories,
            parent_agent=parent_agent,
        )

        logger.debug("[auto_memory] Extraction task completed")

    except Exception as e:
        logger.error("[auto_memory] Extraction failed: %s", e, exc_info=True)


async def _run_memory_extraction_with_cache_sharing(
    memory_dir: Path,
    prompt: str,
    history: list[dict[str, Any]],
    existing_memories: list[dict[str, Any]],
    parent_agent: "AgentAdapter",
) -> None:
    """Run memory extraction with API cache sharing.

    Similar to Claude Code's ForkedAgent pattern:
    - Inherits parent agent's model, system_prompt, and tools
    - Inherits parent agent's message prefix (via create_new_context_engine)
    - Runtime tool restriction via AutoMemoryToolRestrictionRail

    This enables API cache hit because all cache key components match:
    - system_prompt (inherited)
    - tools (inherited, runtime restriction doesn't change tool definitions)
    - model (inherited)
    - messages prefix (inherited)

    Args:
        memory_dir: Path to the coding memory directory.
        prompt: The extraction prompt.
        history: The conversation history.
        existing_memories: List of existing memory files.
        parent_agent: The parent DeepAgent instance to inherit from.
    """
    # Import utility functions from extract_memories module
    from jiuwenswarm.agents.harness.common.auto_memory.extract_memories import (
        _convert_messages_to_base_messages,
    )

    try:
        from jiuwenswarm.agents.harness.common.auto_memory.tool_restriction_rail import (
            AutoMemoryToolRestrictionRail,
        )
        from openjiuwen.core.single_agent.schema.agent_card import AgentCard
        from openjiuwen.harness.factory import create_deep_agent
        from openjiuwen.core.session.agent import Session

        logger.debug("[auto_memory] Cache sharing mode start")

        # Get parent agent's DeepAgent instance (adapter._instance)
        parent_instance = getattr(parent_agent, "_instance", None)
        if parent_instance is None:
            logger.error("[auto_memory] Parent adapter has no _instance, cannot proceed")
            return

        # 1. Get parent agent's model
        parent_model = getattr(parent_instance, "deep_config", None)
        if parent_model is not None:
            parent_model = getattr(parent_model, "model", None)

        if parent_model is None:
            logger.error("[auto_memory] Parent agent has no model, cannot proceed")
            return

        # 2. Get parent agent's actual tools sent to LLM
        # IMPORTANT: ability_manager.list() returns all registered tools
        # These are the actual tools sent to the LLM (after duplicate filtering in DeepAgent)
        parent_tools = []
        if hasattr(parent_instance, "ability_manager"):
            parent_tools = list(parent_instance.ability_manager.list() or [])

        # 2.5. Get parent agent's system_prompt for cache key matching
        parent_system_prompt = None
        if hasattr(parent_instance, "deep_config") and parent_instance.deep_config is not None:
            parent_system_prompt = getattr(parent_instance.deep_config, "system_prompt", None)
        if parent_system_prompt is None:
            # Fallback: try to get from system_prompt_builder
            if hasattr(parent_instance, "system_prompt_builder") and parent_instance.system_prompt_builder is not None:
                # Build prompt dynamically - but this may differ from actual cached prompt
                logger.warning(
                    "[auto_memory] Parent system_prompt is None, will use extraction prompt (may affect cache)"
                )
        # 2.6. Create context_engine_config with no limit for sub-agent
        # auto-memory needs full history, so set max_context_message_num=None (unlimited)
        from openjiuwen.core.context_engine import ContextEngineConfig
        subagent_context_config = ContextEngineConfig(
            max_context_message_num=None,  # No message limit
            default_window_round_num=None,  # No round limit
        )
        # 3. Get parent agent's rails for system prompt consistency
        # Inherit RuntimePromptRail and ProjectMemoryRail to ensure dynamic content injection
        # Rails are stored directly on the adapter (not on _instance)
        parent_rails = []
        # RuntimePromptRail (always present in code mode)
        _runtime_rail = getattr(parent_agent, "_runtime_prompt_rail", None)
        if _runtime_rail is not None:
            parent_rails.append(_runtime_rail)
        # ProjectMemoryRail (code mode only)
        _project_rail = getattr(parent_agent, "_project_memory_rail", None)
        if _project_rail is not None:
            parent_rails.append(_project_rail)
        # CodingMemoryRail (code mode only)
        _coding_rail = getattr(parent_agent, "_coding_memory_rail", None)
        if _coding_rail is not None:
            parent_rails.append(_coding_rail)

        # 4. Create AutoMemoryToolRestrictionRail for runtime tool restriction
        restriction_rail = AutoMemoryToolRestrictionRail(memory_dir=str(memory_dir))

        # 5. Create shared extraction context (Workspace, SysOperation, ToolContext, MemoryTools)
        ctx = await _create_memory_extraction_context(
            memory_dir,
            agent_id="auto_memory_cache_subagent",
            sysop_id_prefix="extract_memories_cache_",
        )
        if ctx is None:
            logger.error("[auto_memory] Failed to create extraction context")
            return

        # 6. Combine parent tools with memory tools
        # Note: For cache sharing, parent_tools should match parent's tool definitions
        # memory_tools are added for memory writing capability
        combined_tools = list(parent_tools) + list(ctx.memory_tools)

        # 7. Create agent card for extract memories subagent
        agent_card = AgentCard(
            name="extractMemoriesCache",
            description="Extract memories with cache sharing",
        )

        # 8. Build extraction prompt (will be sent as user message for cache key matching)
        # IMPORTANT: For cache sharing, we must use parent's system_prompt
        # and send extraction instructions via user message
        extraction_query = f"{prompt}\n\n请分析对话内容并提取记忆，使用 coding_memory_write 工具更新记忆文件。"

        # Use parent's system_prompt if available (for cache key matching)
        # Otherwise fallback to extraction prompt
        subagent_system_prompt = parent_system_prompt if parent_system_prompt else extraction_query

        # 9. Create subagent with parent's model, tools, and rails for cache sharing
        subagent_rails = parent_rails + [restriction_rail]

        sub_agent = create_deep_agent(
            model=parent_model,  # Inherit parent's model for cache sharing
            card=agent_card,
            system_prompt=subagent_system_prompt,  # Inherit parent's system_prompt for cache
            tools=combined_tools,  # Combined tools (parent + memory)
            workspace=ctx.workspace,
            sys_operation=ctx.sys_operation,
            restrict_to_work_dir=True,
            enable_task_loop=False,
            max_iterations=10,  # Keep loose upper limit, actual rounds should be low
            rails=subagent_rails,  # Inherit parent's rails + restriction_rail
            context_engine_config=subagent_context_config,  # No limit for full history
        )

        logger.debug("[auto_memory] Subagent created (cache sharing mode)")

        # 10. Create new context engine inheriting parent's message prefix
        # forkContextMessages pattern: pass full history for cache sharing (like Claude Code)
        fork_prefix_messages_raw = history

        # Convert dict messages to BaseMessage objects
        fork_prefix_messages = _convert_messages_to_base_messages(fork_prefix_messages_raw)

        try:
            # IMPORTANT: Must use sub_agent's context_engine, not parent_agent's!
            # sub_agent has its own independent context_engine._context_pool
            # Creating context on parent_agent won't transfer to sub_agent
            new_session_id = await sub_agent.create_new_context_engine(
                messages=fork_prefix_messages,
            )
        except Exception as ctx_err:
            logger.error("[auto_memory] Failed to create context engine: %s", ctx_err)
            return

        # 11. Invoke subagent with inherited session
        new_session = Session(session_id=new_session_id, card=sub_agent.card)

        # Send extraction instructions via user message
        extraction_input = extraction_query if parent_system_prompt else "请提取记忆"

        logger.debug("[auto_memory] === INVOKING SUBAGENT ===")
        try:
            invoke_start_time = asyncio.get_event_loop().time()

            # Use stream mode to match parent agent's streaming mode for cache sharing
            # IMPORTANT: Parent agent uses stream mode, so subagent must also use stream
            # to match cache key (is_stream parameter affects cache key)
            result_chunks = []
            async for chunk in sub_agent.stream(
                {"query": extraction_input},
                session=new_session,
            ):
                result_chunks.append(chunk)

            # Collect final result from stream
            # The last chunk or accumulated chunks should contain the final result
            result = result_chunks[-1] if result_chunks else None

            # Add timestamp after invoke
            invoke_end_time = asyncio.get_event_loop().time()
            invoke_duration = invoke_end_time - invoke_start_time
            logger.debug("[auto_memory] === SUBAGENT INVOKE COMPLETED === (duration: %.2fs)", invoke_duration)
        except Exception as invoke_err:
            logger.error("[auto_memory] === SUBAGENT INVOKE FAILED ===")
            logger.error("[auto_memory] Invoke error: %s", invoke_err, exc_info=True)
            raise

        # 12. Check result
        if result is None:
            logger.warning("[auto_memory] Subagent returned no result")

        # 13. Check created files
        created_files = list(memory_dir.glob("*.md"))
        logger.info("[auto_memory] Memory files: %d - %s",
                    len(created_files), [f.name for f in created_files])

        # 14. Cleanup
        ctx.cleanup()

        logger.info("[auto_memory] === CACHE SHARING MODE END ===")

    except Exception as e:
        logger.error("[auto_memory] Cache sharing extraction failed: %s", e, exc_info=True)


