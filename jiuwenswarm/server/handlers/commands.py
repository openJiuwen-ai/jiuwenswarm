# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""命令域 handler"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import sys
from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist import (
    persist_cli_trusted_directory,
)
from jiuwenswarm.common.config import (
    get_config,
    get_default_models,
    get_mcp_server_config,
    get_mcp_servers,
    get_sandbox_endpoint,
    get_sandbox_runtime,
    remove_mcp_server_in_config,
    set_mcp_server_enabled_in_config,
    upsert_mcp_server_in_config,
)
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.model_config_validation import is_placeholder_api_base
from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.utils import get_config_file, mask_sensitive
from jiuwenswarm.common.version import __version__
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers._shared import (
    _agent_workspace_dir_for_request,
    _sessions_dir_for_request,
    resolve_agent_request_mode,
    resolve_request_project_dir,
)
from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import (
    build_yuanrong_sandbox_status_view,
    validate_sandbox_files_runtime,
)
from jiuwenswarm.server.runtime.session.session_history import append_compact_history_records
from jiuwenswarm.server.runtime.session.session_metadata import get_all_sessions_metadata
from jiuwenswarm.server.wire_truncate import (
    _build_workflow_detail_payload,
    _build_workflow_human_prompt_payload,
    _build_workflow_list_payload,
    _json_wire_size,
)

logger = logging.getLogger(__name__)

# /simplify prompt template — adapted /simplify skill for jiuwenswarm.
# Guides the agent through three phases: identify changes → three-dimension review
# (reuse/quality/efficiency) → aggregate and fix.
# Note: jiuwenswarm's sub-agents (task_tool / Agent tool) can only be dispatched to registered
# types (explore/plan/code, etc.) and cannot create custom reviewer roles on the fly. The prompt
# therefore presents parallel sub-agent review as an optional optimization — the agent may also
# perform all three reviews itself directly.
_SIMPLIFY_PROMPT_TEMPLATE = """\
# Simplify: Code Review and Cleanup

Review all changed files for reuse, quality, and efficiency. Fix any issues found.

## Scope

This review covers **reuse, quality, and efficiency only** — the three dimensions below. It is NOT a security review.

- Do NOT flag, fix, or report security vulnerabilities (injection, XSS, hard-coded secrets, auth flaws, etc.). Those are out of scope here and are handled by `/security-review`, which reports findings without modifying code.
- If you happen to notice a likely security issue while reviewing, do not fix it — at most note it in one line at the end ("possible security concern in <file>:<line>, run /security-review") and continue with the reuse/quality/efficiency review.

## Phase 1: Identify Changes

Run `git diff` (or `git diff HEAD` if there are staged changes) to see what changed. If there are no git changes, review the most recently modified files that the user mentioned or that you edited earlier in this conversation.

## Phase 2: Launch Three Review Agents in Parallel

If sub-agent tools are available (e.g. task_tool / Agent tool), launch all three agents concurrently in a single message. Pass each agent the full diff so it has the complete context. Otherwise, perform all three reviews yourself directly.

### Agent 1: Code Reuse Review

For each change:

1. **Search for existing utilities and helpers** that could replace newly written code. Look for similar patterns elsewhere in the codebase — common locations are utility directories, shared modules, and files adjacent to the changed ones.
2. **Flag any new function that duplicates existing functionality.** Suggest the existing function to use instead.
3. **Flag any inline logic that could use an existing utility** — hand-rolled string manipulation, manual path handling, custom environment checks, ad-hoc type guards, and similar patterns are common candidates.

### Agent 2: Code Quality Review

Review the same changes for hacky patterns:

1. **Redundant state**: state that duplicates existing state, cached values that could be derived, observers/effects that could be direct calls
2. **Parameter sprawl**: adding new parameters to a function instead of generalizing or restructuring existing ones
3. **Copy-paste with slight variation**: near-duplicate code blocks that should be unified with a shared abstraction
4. **Leaky abstractions**: exposing internal details that should be encapsulated, or breaking existing abstraction boundaries
5. **Stringly-typed code**: using raw strings where constants, enums (string unions), or branded types already exist in the codebase
6. **Unnecessary JSX nesting**: wrapper Boxes/elements that add no layout value — check if inner component props (flexShrink, alignItems, etc.) already provide the needed behavior
7. **Unnecessary comments**: comments explaining WHAT the code does (well-named identifiers already do that), narrating the change, or referencing the task/caller — delete; keep only non-obvious WHY (hidden constraints, subtle invariants, workarounds)

### Agent 3: Efficiency Review

Review the same changes for efficiency:

1. **Unnecessary work**: redundant computations, repeated file reads, duplicate network/API calls, N+1 patterns
2. **Missed concurrency**: independent operations run sequentially when they could run in parallel
3. **Hot-path bloat**: new blocking work added to startup or per-request/per-render hot paths
4. **Recurring no-op updates**: state/store updates inside polling loops, intervals, or event handlers that fire unconditionally — add a change-detection guard so downstream consumers aren't notified when nothing changed. Also: if a wrapper function takes an updater/reducer callback, verify it honors same-reference returns (or whatever the "no change" signal is) — otherwise callers' early-return no-ops are silently defeated
5. **Unnecessary existence checks**: pre-checking file/resource existence before operating (TOCTOU anti-pattern) — operate directly and handle the error
6. **Memory**: unbounded data structures, missing cleanup, event listener leaks
7. **Overly broad operations**: reading entire files when only a portion is needed, loading all items when filtering for one

## Phase 3: Fix Issues

Wait for all reviewers to complete. Aggregate their findings and fix each issue directly. If a finding is a false positive or not worth addressing, note it and move on — do not argue with the finding, just skip it.

When done, briefly summarize what was fixed (or confirm the code was already clean).
"""


def _build_simplify_prompt(target: str = "") -> str:
    """Build the prompt for the /simplify command.

    Args:
        target: Optional additional focus (e.g. file path, module name, specific dimension
            to emphasize), appended to the end of the prompt.
    """
    prompt = _SIMPLIFY_PROMPT_TEMPLATE
    if target:
        prompt += f"\n\n## Additional Focus\n\n{target}"
    return prompt


def _extract_compact_summary_processor(summary: str) -> str:
    for line in str(summary or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "processor":
            return value.strip()
    return ""


def _is_env_api_base_placeholder(env_updates: dict) -> bool:
    """检查 env_updates 中的 API_BASE 是否指向 example.* 等占位域名。"""
    return is_placeholder_api_base(str(env_updates.get("API_BASE", "") or "").strip())


async def handle_command_workflows(ctx: RequestContext) -> None:
    """Handle command.workflows RPC — list summaries or get one workflow detail."""
    request = ctx.request
    from jiuwenswarm.agents.harness.team import get_team_manager

    session_id = request.session_id or ""
    channel_id = request.channel_id or "web"
    params = request.params if isinstance(request.params, dict) else {}
    action = str(params.get("action") or "list").strip().lower()
    workflow_id = params.get("workflow_id") or params.get("workflow_run_id")
    wf_id_log = workflow_id.strip() if isinstance(workflow_id, str) else workflow_id

    logger.info(
        "[WF_DBG] command.workflows req channel_id=%s session_id=%s request_id=%s action=%s workflow_id=%s",
        channel_id,
        session_id,
        request.request_id,
        action,
        wf_id_log,
    )

    team_manager = get_team_manager(channel_id)
    workflow_handler = team_manager.get_workflow_handler(session_id)
    source = "live" if workflow_handler is not None else "checkpoint"
    detail_raw_bytes: int | None = None

    if workflow_handler is None:
        # No live handler (runtime not active / torn down by cancel-stop).
        # The snapshot is a read-only pull and must not depend on runtime
        # liveness — fall back to the persisted checkpoint so historical /
        # terminal workflow runs remain queryable after the team session
        # is cancelled or stopped.
        try:
            from jiuwenswarm.server.handlers._shared import _sessions_dir_for_request
            from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
                restore_workflow_runs,
            )

            restored = restore_workflow_runs(
                session_id,
                sessions_root=_sessions_dir_for_request(request),
            )
            workflows = (
                [run.to_workflow_run_dict() for run in restored.values()]
                if restored
                else []
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[WF_DBG] command.workflows checkpoint_restore_failed session_id=%s error=%s",
                session_id,
                exc,
            )
            workflows = []
    else:
        try:
            workflows = workflow_handler.get_workflow_snapshot()
        except Exception as e:
            logger.warning(
                "[WF_DBG] command.workflows snapshot_failed session_id=%s error=%s",
                session_id,
                e,
            )
            workflows = []

    source_count = len(workflows)
    source_bytes = sum(_json_wire_size(item) for item in workflows if isinstance(item, dict))

    if action == "get":
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=False,
                payload={"error": "workflow_id is required for action=get"},
            )
        else:
            target_id = workflow_id.strip()
            match = next(
                (item for item in workflows if isinstance(item, dict) and item.get("id") == target_id),
                None,
            )
            if match is None:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload={"error": f"workflow not found: {target_id}"},
                )
            else:
                detail_raw_bytes = _json_wire_size(match)
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=True,
                    payload=_build_workflow_detail_payload(match, session_id=session_id),
                )
    elif action == "get_human_prompt":
        agent_id = params.get("agent_id")
        correlation_id = params.get("correlation_id")
        agent_id_str = agent_id.strip() if isinstance(agent_id, str) and agent_id.strip() else None
        corr_id_str = (
            correlation_id.strip()
            if isinstance(correlation_id, str) and correlation_id.strip()
            else None
        )
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=False,
                payload={"error": "workflow_id is required for action=get_human_prompt"},
            )
        elif not agent_id_str and not corr_id_str:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=False,
                payload={"error": "agent_id or correlation_id is required for action=get_human_prompt"},
            )
        else:
            target_id = workflow_id.strip()
            match = next(
                (item for item in workflows if isinstance(item, dict) and item.get("id") == target_id),
                None,
            )
            if match is None:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok=False,
                    payload={"error": f"workflow not found: {target_id}"},
                )
            else:
                prompt_payload = _build_workflow_human_prompt_payload(
                    match,
                    session_id=session_id,
                    agent_id=agent_id_str,
                    correlation_id=corr_id_str,
                )
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=channel_id,
                    ok="error" not in prompt_payload,
                    payload=prompt_payload,
                )
    else:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=True,
            payload=_build_workflow_list_payload(workflows, session_id=session_id),
        )

    payload = resp.payload if isinstance(resp.payload, dict) else {}
    payload_bytes = _json_wire_size(payload)
    truncated = bool(payload.get("truncated")) if isinstance(payload, dict) else False
    included = len(payload.get("workflows", [])) if payload.get("action") == "list" else None
    error = payload.get("error") if isinstance(payload, dict) and not resp.ok else None
    log_level = logging.WARNING if (not resp.ok or truncated) else logging.INFO
    if action == "list":
        logger.log(
            log_level,
            "[WF_DBG] command.workflows res ok=%s action=list source=%s count=%d source_bytes=%d "
            "payload_bytes=%d included=%d/%d truncated=%s error=%s",
            resp.ok,
            source,
            source_count,
            source_bytes,
            payload_bytes,
            included or 0,
            source_count,
            truncated,
            error,
        )
    else:
        prompt_len = None
        if action == "get_human_prompt" and isinstance(payload, dict):
            human_prompt = payload.get("human_prompt")
            if isinstance(human_prompt, str):
                prompt_len = len(human_prompt.encode("utf-8"))
        logger.log(
            log_level,
            "[WF_DBG] command.workflows res ok=%s action=%s source=%s workflow_id=%s "
            "raw_bytes=%s payload_bytes=%d truncated=%s prompt_len=%s error=%s",
            resp.ok,
            action,
            source,
            wf_id_log,
            detail_raw_bytes,
            payload_bytes,
            truncated,
            prompt_len,
            error,
        )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_command_add_dir(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        params = request.params or {}
        directory_path = params.get("path")
        remember = params.get("remember", False)
        persist: dict[str, Any]
        if directory_path is None or (
                isinstance(directory_path, str) and not directory_path.strip()
        ):
            persist = {"ok": False, "error": "path is required"}
        else:
            persist = persist_cli_trusted_directory(str(directory_path))
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=bool(persist.get("ok", False)),
            payload={
                "path": directory_path,
                "remember": remember,
                "persist": persist,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.add_dir failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "error": str(e),
                "code": "BAD_REQUEST" if isinstance(e, ValueError) else "SESSION_CREATE_FAILED",
            },
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_command_chrome(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={},
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.chrome failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_command_compact(ctx: RequestContext) -> None:
    from openjiuwen.harness.observability import close_agent_run_span, open_agent_run_span
    from jiuwenswarm.agents.harness.agent_observability import sync_agent_observability

    request = ctx.request
    run_span = None
    try:
        session_id = request.session_id or "default"
        params = request.params or {}

        channel_id = request.channel_id or "default"
        mode, sub_mode, canonical_mode = resolve_agent_request_mode(params.get("mode", "agent"))
        agent_mode = "agent" if mode == "auto_harness" else mode
        agent = ctx.services.agent_manager.get_agent_for_session_nowait(
            channel_id, session_id
        )
        if agent is None:
            agent = await ctx.services.agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode,
            )

        if agent is None:
            raise ValueError("Failed to get agent")

        await agent.ensure_instance()
        await asyncio.to_thread(sync_agent_observability)
        execution_subject = None
        if is_team_mode(canonical_mode):
            from jiuwenswarm.agents.harness.team import get_team_manager

            team_agent = get_team_manager(channel_id).get_team_agent(session_id)
            if team_agent is not None:
                execution_subject = team_agent.observability_execution_subject(session_id)
        run_span = open_agent_run_span(
            session_id=session_id,
            mode=params.get("mode", "agent"),
            request_id=request.request_id,
            run_id=request.request_id,
            execution_subject=execution_subject,
        )
        result_data = await agent.compress_context(session_id=session_id, return_state=True)

        result = result_data.get("result")
        stats = result_data.get("stats")
        state = result_data.get("state") if isinstance(result_data.get("state"), dict) else {}
        summary = str(
            result_data.get("compact_summary")
            or state.get("compact_summary")
            or result_data.get("summary")
            or ""
        ).strip()

        if result == "compressed" and stats:
            before_tokens = stats.get("raw_total_tokens", 0)
            after_tokens = stats.get("total_tokens", 0)
            if before_tokens > 0:
                rate = round((before_tokens - after_tokens) / before_tokens * 100, 1)
            else:
                rate = 0
            stats_summary = (
                f"\u2713 Context compacted: {after_tokens / 1000:.1f}K/"
                f"{before_tokens / 1000:.1f}K tokens ({rate:.1f}% saved)"
            )

            await ctx.services.send_push({
                "channel_id": channel_id,
                "session_id": session_id,
                "payload": {
                    "event_type": "context.compressed",
                    "rate": rate,
                    "beforeCompressed": before_tokens,
                    "afterCompressed": after_tokens,
                },
            })
            if summary:
                append_compact_history_records(
                    session_id=session_id,
                    request_id=request.request_id,
                    channel_id=channel_id,
                    summary=summary,
                    timestamp=_dt.datetime.now().timestamp(),
                    trigger="manual",
                    stats=stats,
                    mode=params.get("mode", "agent"),
                )
                compression_state_payload: dict[str, Any] = {
                    **state,
                    "event_type": "context.compression_state",
                    "status": state.get("status") or "compressed",
                    "phase": state.get("phase") or "active_compress",
                    "processor": state.get("processor") or _extract_compact_summary_processor(summary),
                    "before": state.get("before") or {"tokens": before_tokens},
                    "after": state.get("after") or {"tokens": after_tokens},
                    "saved": state.get("saved") or {
                        "tokens": before_tokens - after_tokens,
                        "percent": rate,
                    },
                    "summary": stats_summary,
                    "compact_summary": summary,
                }
                await ctx.services.send_push({
                    "channel_id": channel_id,
                    "session_id": session_id,
                    "payload": compression_state_payload,
                })

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "result": result,
                "stats": stats,
                **({"summary": summary} if summary else {}),
                **({"compact_summary": summary} if summary else {}),
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.compact failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    finally:
        close_agent_run_span(run_span, session_id=request.session_id or "default")
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_command_compact_partial(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        session_id = request.session_id or "default"
        params = request.params or {}
        turn_index = int(params.get("turn_index", 0))
        direction = str(params.get("direction") or "from").strip()

        channel_id = request.channel_id or "default"
        mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
        agent_mode = "agent" if mode == "auto_harness" else mode
        agent = await ctx.services.agent_manager.get_agent(
            channel_id=channel_id,
            mode=agent_mode,
            project_dir=resolve_request_project_dir(request),
            sub_mode=sub_mode,
        )

        if agent is None:
            raise ValueError("Failed to get agent")

        result_data = await agent.compact_partial(
            session_id=session_id,
            turn_index=turn_index,
            direction=direction,
        )

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=result_data,
        )
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        logger.exception("[AgentWebSocketServer] command.compact_partial failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "status": "failed",
                "error": str(e),
            },
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_command_context(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        session_id = request.session_id or "default"
        params = request.params or {}

        channel_id = request.channel_id or "default"
        mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
        agent_mode = "agent" if mode == "auto_harness" else mode
        agent = await ctx.services.agent_manager.get_agent(
            channel_id=channel_id,
            mode=agent_mode,
            project_dir=resolve_request_project_dir(request),
            sub_mode=sub_mode,
        )

        if agent is None:
            raise ValueError("Failed to get agent")

        result_data = await agent.get_context_usage(session_id=session_id)

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=result_data,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.context failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_command_recap(ctx: RequestContext) -> None:
    """处理 /recap 命令：生成会话快速回顾（read-only，不修改历史）"""
    request = ctx.request
    try:
        session_id = request.session_id or "default"
        params = request.params or {}
        channel_id = request.channel_id or "default"
        mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
        agent_mode = "agent" if mode == "auto_harness" else mode

        agent = await ctx.services.agent_manager.get_agent(
            channel_id=channel_id,
            mode=agent_mode,
            project_dir=resolve_request_project_dir(request),
            sub_mode=sub_mode,
        )

        if agent is None:
            raise ValueError("Failed to get agent")

        result_data = await agent.generate_recap(session_id=session_id)

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=result_data,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.recap failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "status": "failed",
                "error": str(e),
            },
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_command_btw(ctx: RequestContext) -> None:
    """处理 /btw 命令：独立、无工具、单轮 LLM 侧问题查询。

    - 获取当前会话上下文（最近消息）
    - 用隔离的 LLM 查询回答问题
    - 不修改对话历史
    - 不使用任何工具（纯文本回答）
    - 仅单轮（无后续 token 消耗）
    """
    request = ctx.request
    try:
        session_id = request.session_id or "default"
        params = request.params or {}
        channel_id = request.channel_id or "default"
        question = (params.get("question") or "").strip()

        logger.info(
            "[AgentWebSocketServer] command.btw received: session_id=%s question=%s",
            session_id,
            question[:100] if question else "",
        )

        if not question:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"status": "failed", "error": "Question is required"},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            await ctx.sink.send_wire(wire)
            return

        mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
        agent_mode = "agent" if mode == "auto_harness" else mode

        agent = await ctx.services.agent_manager.get_agent(
            channel_id=channel_id,
            mode=agent_mode,
            project_dir=resolve_request_project_dir(request),
            sub_mode=sub_mode,
        )

        if agent is None:
            raise ValueError("Failed to get agent")

        result_data = await agent.generate_btw_answer(
            session_id=session_id,
            question=question,
        )

        logger.info(
            "[AgentWebSocketServer] command.btw result: status=%s",
            result_data.get("status"),
        )

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=result_data,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.btw failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "status": "failed",
                "error": str(e),
            },
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_command_diff(ctx: RequestContext) -> None:
    request = ctx.request
    from jiuwenswarm.server.runtime.session.git_diff_status import get_session_extra_history_roots
    from jiuwenswarm.server.utils.diff_service import get_diff_service

    try:
        session_id = request.session_id or "default"
        project_dir = resolve_request_project_dir(request)
        extra_history_roots = get_session_extra_history_roots(
            session_id,
            sessions_root=_sessions_dir_for_request(request),
            agent_workspace_root=_agent_workspace_dir_for_request(request),
        )
        diff_service = get_diff_service()
        turns, git_diff = await asyncio.gather(
            asyncio.to_thread(
                diff_service.get_turn_diffs,
                session_id,
                project_dir,
                extra_history_roots=extra_history_roots,
            ),
            asyncio.to_thread(diff_service.get_git_diff, project_dir),
        )

        logger.info(
            "[AgentWebSocketServer] command.diff response: session_id=%s turns=%s git_diff=%s project_dir=%s",
            session_id,
            len(turns),
            git_diff is not None,
            project_dir,
        )

        payload: dict[str, Any] = {
            "type": "list",
            "turns": turns,
        }
        if git_diff is not None:
            payload["gitDiff"] = git_diff

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
    except Exception as e:
        logger.exception("[AgentWebSocketServer] command.diff failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)




async def handle_command_simplify(ctx: RequestContext) -> None:
    """处理 /simplify 命令：组装代码精简审查 prompt 并返回（由前端作为消息发送给 Agent）。

    prompt 指导 Agent 分三阶段完成
    1) 识别改动（git diff）
    2) 三维度审查（复用 / 质量 / 效率）—— 子 Agent 并行审查为可选优化手段
    3) 聚合发现并直接修复
    """
    request = ctx.request
    try:
        params = request.params or {}
        target = str(params.get("target", "")).strip()

        prompt = _build_simplify_prompt(target)

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"prompt": prompt},
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.simplify failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_command_model(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        params = request.params or {}
        action = params.get("action")

        if action == "add_model":
            target = str(params.get("target", "")).strip()
            logger.info("[command.model] add_model: target=%s", target)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"type": "model_added", "name": target},
            )

        elif action == "switch_model":
            target = str(params.get("model", "")).strip()
            env_updates = params.get("env_updates", {})
            logger.info(
                "[command.model] switch_model: target=%s, env_updates=%s",
                target,
                mask_sensitive(env_updates),
            )

            if not env_updates:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "No env_updates provided"},
                )
            elif _is_env_api_base_placeholder(env_updates):
                api_base_val = str(env_updates.get("API_BASE", ""))
                logger.warning(
                    "[command.model] switch_model rejected: API_BASE is a placeholder domain: %s",
                    api_base_val,
                )
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={
                        "error": f"API_BASE '{api_base_val}' 指向占位域名，无法实际提供服务，请配置有效的 API 地址",
                    },
                )
            else:
                for k, v in env_updates.items():
                    os.environ[k] = v
                logger.info("[command.model] os.environ 已更新, MODEL_NAME=%s", os.getenv("MODEL_NAME", "unknown"))

                try:
                    from jiuwenswarm.agents.harness.common.memory.config import (
                        clear_config_cache,
                        clear_embed_config_db_cache,
                    )
                    clear_config_cache()
                    clear_embed_config_db_cache()
                    logger.info("[command.model] config cache 已清除")
                except Exception as e:
                    logger.debug("[command.model] clear_config_cache skipped: %s", e)

                try:
                    await ctx.services.agent_manager.reload_agents_config(None, env_updates)
                    logger.info("[command.model] agent config 已重载")
                except Exception as e:
                    logger.debug("[command.model] reload_agents_config skipped: %s", e)

                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "current": os.getenv("MODEL_NAME", "unknown"),
                        "requested": target,
                        "type": "switched",
                        "applied": True,
                    },
                )
                logger.info("[command.model] 切换完成: current=%s", os.getenv("MODEL_NAME", "unknown"))

        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"current": os.getenv("MODEL_NAME", "unknown"), "available": ["default-model"]},
            )

    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.model failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_command_resume(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        params = request.params or {}
        query = params.get("query")
        session_id = query if isinstance(query, str) and query.strip() else "sess_mock_resume"
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "session_id": session_id,
                "query": query if isinstance(query, str) else "",
                "resumed": True,
                "preview": "Mock resumed conversation",
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.resume failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_command_session(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        session_id = request.session_id or "sess_mock"
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "session_id": session_id,
                "remote_url": f"https://example.com/session/{session_id}",
                "qr_text": f"session:{session_id}",
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.session failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_command_status(ctx: RequestContext) -> None:
    request = ctx.request
    try:
        params = request.params or {}
        action = str(params.get("action", "overview")).strip().lower()

        if action == "usage":
            sessions, total = get_all_sessions_metadata(limit=500, offset=0)
            messages_total = sum(s.get("message_count", 0) for s in sessions)
            model_counts: dict[str, int] = {}
            for s in sessions:
                mode = str(s.get("mode", "unknown"))
                model_counts[mode] = model_counts.get(mode, 0) + 1
            active_days_set: set[str] = set()
            longest_hours = 0.0
            for s in sessions:
                created = s.get("created_at", 0)
                last = s.get("last_message_at", 0)
                if created:
                    try:
                        day_str = _dt.datetime.fromtimestamp(
                            created, tz=_dt.timezone.utc
                        ).strftime("%Y-%m-%d")
                        active_days_set.add(day_str)
                    except Exception:  # noqa: BLE001
                        pass
                if created and last:
                    longest_hours = max(longest_hours, (last - created) / 3600)

            models_used = [{"name": k, "count": v} for k, v in sorted(model_counts.items(), key=lambda x: -x[1])]
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "sessions_total": total,
                    "messages_total": messages_total,
                    "models_used": models_used,
                    "active_days": len(active_days_set),
                    "longest_session_hours": round(longest_hours, 1),
                },
            )
        elif action == "config":
            config_path = str(get_config_file())
            settings_sources: list[str] = []
            config_dir = os.getenv("JIUWENSWARM_CONFIG_DIR")
            if config_dir:
                settings_sources.append(f"env:JIUWENSWARM_CONFIG_DIR={config_dir}")
            settings_sources.append(config_path)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "config_path": config_path,
                    "settings_sources": settings_sources,
                },
            )
        else:
            # overview (default)
            config = get_config()
            session_id = request.session_id or ""
            default_models = get_default_models(config)
            active_entry = default_models[0] if default_models else {}
            mcc = active_entry.get("model_client_config", {})
            model_name = str(mcc.get("model_name", "") or config.get("model", ""))
            provider = str(mcc.get("client_provider", "") or config.get("model_provider", ""))
            api_base = str(mcc.get("api_base", "") or config.get("api_base", ""))

            mcp_servers = get_mcp_servers()
            mcp_summary = [
                {
                    "name": str(s.get("name", "unknown")),
                    "enabled": bool(s.get("enabled", True)),
                    "transport": str(s.get("transport", "unknown")),
                }
                for s in mcp_servers
                if isinstance(s, dict)
            ]

            config_path = str(get_config_file())
            settings_sources: list[str] = []
            config_dir = os.getenv("JIUWENSWARM_CONFIG_DIR")
            if config_dir:
                settings_sources.append(f"env:JIUWENSWARM_CONFIG_DIR={config_dir}")
            settings_sources.append(config_path)

            # Memory diagnostics — use the actual workspace dir (trusted_dir or cwd),
            # same as ProjectMemoryRail, so we detect JIUWESWARM.md where /init creates it.
            params = request.params or {}
            workspace_dir = str(params.get("cwd", "") or os.getcwd())
            trusted_dirs = params.get("trusted_dirs")
            if isinstance(trusted_dirs, list) and trusted_dirs:
                workspace_dir = str(trusted_dirs[0])
            try:
                from jiuwenswarm.agents.harness.common.rails.project_memory import (
                    clear_project_memory_cache,
                    discover_and_load_memory_files,
                    get_large_memory_files,
                )
                clear_project_memory_cache(workspace_dir)
                project_files = discover_and_load_memory_files(
                    workspace=workspace_dir, target_path=workspace_dir,
                )
                memory_warnings = get_large_memory_files(project_files)
                logger.info(
                    "[AgentWebSocketServer] memory diagnostics: "
                    "workspace_dir=%s, files=%d, warnings=%d",
                    workspace_dir, len(project_files), len(memory_warnings),
                )
            except Exception as exc:
                logger.warning(
                    "[AgentWebSocketServer] memory diagnostics failed: "
                    "workspace_dir=%s, error=%s",
                    workspace_dir, exc,
                )
                memory_warnings = []

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "version": __version__,
                    "session_id": session_id,
                    "cwd": str(params.get("cwd", "") or os.getcwd()),
                    "model": model_name,
                    "provider": provider,
                    "api_base": api_base,
                    "connection_status": "connected",
                    "mcp_servers": mcp_summary,
                    "config_path": config_path,
                    "settings_sources": settings_sources,
                    "memory_warnings": memory_warnings,
                },
            )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] command.status failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)
