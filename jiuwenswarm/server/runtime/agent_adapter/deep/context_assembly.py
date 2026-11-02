"""ContextAssemble / ContextProcessor 的装配辅助。

职责：
- 构建 ContextAssembleRail（含旧构造签名兼容与 allowlist 同步）
- 解析 session_memory 配置并构建 ContextProcessorRail

只做配置到实例的转换，不是新 rail 实现；Code 模式方法内部仍调用这里。
"""

from __future__ import annotations

import logging
from typing import Any, List, Tuple

from openjiuwen.core.context_engine.processor.compressor import FullCompactProcessorConfig
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.rails.context_engineer.context_assemble_rail import ContextAssembleRail
from openjiuwen.harness.rails.context_engineer.context_processor_rail import ContextProcessorRail

logger = logging.getLogger(__name__)


def _build_context_assemble_rail(
    disabled_tools: list[str] | None = None,
    tool_name_allowlist: list[str] | None = None,
) -> ContextAssembleRail | None:
    """Build ContextAssembleRail.

    ``disabled_tools`` seeds the tools prompt hide-list. Product adapters own
    the blacklist data flow at construction time (and optional later
    ``update_disabled_tools`` calls).

    ``tool_name_allowlist`` (typically ProgressiveToolRail.eager_tools) keeps the
    system ``# 可用工具`` section aligned with the fixed eager schema so deferred
    / OfficeClaw MCP registrations do not rewrite the prompt prefix mid-task.
    Compatible with older openjiuwen / test fakes whose constructor does not
    accept the newer kwargs.

    Merge order: land agent-core !2840 (``tool_name_allowlist`` + tools-section
    fingerprint ``has_section`` guard) before this change. Without !2840 the
    modern kwargs raise ``TypeError`` and we silently fall back to the legacy
    constructor / optional setter path.
    """
    try:
        ctor_path = "modern"
        effective_disabled: list[str] | None = disabled_tools
        effective_allowlist: list[str] | None = tool_name_allowlist
        try:
            context_assemble_rail = ContextAssembleRail(
                disabled_tools=disabled_tools,
                tool_name_allowlist=tool_name_allowlist,
            )
        except TypeError:
            # Requested allowlist is not in effect until a setter applies it.
            effective_allowlist = None
            try:
                context_assemble_rail = ContextAssembleRail(disabled_tools=disabled_tools)
                ctor_path = "legacy_disabled_kw"
            except TypeError:
                context_assemble_rail = ContextAssembleRail()
                ctor_path = "legacy_no_args"
                effective_disabled = None
                update = getattr(context_assemble_rail, "update_disabled_tools", None)
                if callable(update) and disabled_tools:
                    update(disabled_tools)
                    effective_disabled = list(disabled_tools)
            setter = getattr(context_assemble_rail, "set_tool_name_allowlist", None)
            if callable(setter) and tool_name_allowlist is not None:
                setter(tool_name_allowlist)
                effective_allowlist = list(tool_name_allowlist)
        # Prefer instance state when the rail exposes what actually stuck.
        for attr in ("tool_name_allowlist", "_tool_name_allowlist"):
            if hasattr(context_assemble_rail, attr):
                effective_allowlist = getattr(context_assemble_rail, attr)
                break
        for attr in ("disabled_tools", "_disabled_tools"):
            if hasattr(context_assemble_rail, attr):
                value = getattr(context_assemble_rail, attr)
                if isinstance(value, (set, frozenset)):
                    effective_disabled = sorted(str(item) for item in value)
                elif value is not None:
                    effective_disabled = list(value)
                else:
                    effective_disabled = None
                break
        logger.info(
            "[JiuWenSwarmDeepAdapter] ContextAssembleRail create success "
            "(path=%s disabled_tools=%s allowlist=%s)",
            ctor_path,
            list(effective_disabled) if effective_disabled else None,
            list(effective_allowlist) if effective_allowlist else None,
        )
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] ContextAssembleRail create failed: %s", exc)
        context_assemble_rail = None
    return context_assemble_rail


def _sync_context_assemble_tool_allowlist(
    assemble: ContextAssembleRail | None,
    progressive: Any | None,
) -> None:
    """Keep ContextAssemble tools-section allowlist aligned with eager schema."""
    if assemble is None:
        return
    setter = getattr(assemble, "set_tool_name_allowlist", None)
    if not callable(setter):
        return
    if progressive is None:
        setter(None)
        return
    eager = getattr(progressive, "eager_tools", None) or []
    setter(list(eager))


def _resolve_session_memory_config(context_engine_cfg: dict[str, Any]) -> dict[str, Any] | None:
    raw_config = (
        context_engine_cfg.get("session_memory_config")
        or context_engine_cfg.get("session_memory")
    )
    if raw_config is True:
        return {}
    if isinstance(raw_config, dict):
        return raw_config
    return None


def _build_context_processor_rail(config: dict[str, Any]) -> ContextProcessorRail | None:
    """Build ContextProcessorRail with user config.

    从配置中读取 processor 配置，传递给 ContextProcessorRail。

    Args:
        config: 配置字典
    """
    try:
        user_processors: List[Tuple[str, dict]] = []
        raw_context_engine_cfg = config.get("context_engine_config", {})
        context_engine_cfg = raw_context_engine_cfg if isinstance(raw_context_engine_cfg, dict) else {}
        session_memory_cfg = _resolve_session_memory_config(context_engine_cfg)

        offloader_cfg = context_engine_cfg.get("message_summary_offloader_config", {})
        if isinstance(offloader_cfg, dict) and offloader_cfg:
            user_processors.append(("MessageSummaryOffloader", offloader_cfg))

        # 会话记忆：preset 链中默认为禁用，此处用配置覆盖启用
        session_memory_compressor_cfg = context_engine_cfg.get("session_memory_compressor_config", {})
        if isinstance(session_memory_compressor_cfg, dict) and session_memory_compressor_cfg:
            user_processors.append(("SessionMemoryCompressor", session_memory_compressor_cfg))

        compressor_cfg = context_engine_cfg.get("dialogue_compressor_config", {})
        if isinstance(compressor_cfg, dict) and compressor_cfg:
            user_processors.append(("DialogueCompressor", compressor_cfg))

        current_round_cfg = context_engine_cfg.get("current_round_compressor_config", {})
        if isinstance(current_round_cfg, dict) and current_round_cfg:
            user_processors.append(("CurrentRoundCompressor", current_round_cfg))

        round_level_cfg = context_engine_cfg.get("round_level_compressor_config", {})
        if isinstance(round_level_cfg, dict) and round_level_cfg:
            user_processors.append(("RoundLevelCompressor", round_level_cfg))

        # 兜底压缩：整窗口 token 超阈值时调一次 LLM 把历史压成 summary。
        # ContextOverflowRecoveryRail 在 413/上下文溢出时靠 set_force_compact 触发。
        # 注意：FullCompactProcessor 不在 0.1.16 preset 链中，_merge_processors 对非 preset
        # processor 不接受 dict（无 base_cfg 可合并），必须传完整 BaseModel 实例。
        full_compact_cfg = context_engine_cfg.get("full_compact_processor_config", {})
        if isinstance(full_compact_cfg, dict) and full_compact_cfg:
            user_processors.append(
                ("FullCompactProcessor", FullCompactProcessorConfig(**full_compact_cfg))
            )

        reasoning_loop_cfg = context_engine_cfg.get("reasoning_tool_loop_compact_config", {})
        if isinstance(reasoning_loop_cfg, dict) and reasoning_loop_cfg:
            reasoning_loop_cfg = {
                **reasoning_loop_cfg,
                "language": resolve_language(
                    str(config.get("preferred_language", "zh")).strip().lower()
                ),
            }
            user_processors.append(("ReasoningToolLoopCompactProcessor", reasoning_loop_cfg))

        context_rail = ContextProcessorRail(
            processors=user_processors if user_processors else None,
            preset=True,
            session_memory=session_memory_cfg,
        )
        logger.info(
            "[JiuWenSwarmDeepAdapter] ContextProcessorRail create success for agent mode, "
            "user_processors=%s session_memory=%s",
            [p[0] for p in user_processors] if user_processors else "none",
            "enabled" if isinstance(session_memory_cfg, dict) else "disabled",
        )
        return context_rail
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] ContextProcessorRail create failed: %s", exc)
        return None
