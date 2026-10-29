# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Commands management; resources remain owned by its injected host."""

from __future__ import annotations

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from typing import Any
import datetime as _dt
from jiuwenswarm.server.runtime.session.session_history import (
    append_compact_history_records,
)
from jiuwenswarm.server.runtime.session.session_history import append_history_record
import asyncio
from gateway.common.mode_matrix import deprecate_mode
from jiuwenswarm.server.runtime.session.session_metadata import (
    get_all_sessions_metadata,
)
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import get_config_file
from jiuwenswarm.common.config import get_default_models
from jiuwenswarm.common.config import get_mcp_servers
from jiuwenswarm.common.version import get_runtime_version
from jiuwenswarm.common.model_config_validation import is_placeholder_api_base
from gateway.common.mode_matrix import is_team_mode
from jiuwenswarm.common.utils import mask_sensitive
import os
from jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist import (
    persist_cli_trusted_directory,
)
from jiuwenswarm.runtime.request import resolve_agent_request_mode
from jiuwenswarm.runtime.request import resolve_request_project_dir

logger = logging.getLogger(__name__)


_MANUAL_COMPACT_PROCESSOR_TYPES = ["MessageSummaryOffloader", "RoundLevelCompressor"]


_SIMPLIFY_PROMPT_TEMPLATE = '# Simplify: Code Review and Cleanup\n\nReview all changed files for reuse, quality, and efficiency. Fix any issues found.\n\n## Scope\n\nThis review covers **reuse, quality, and efficiency only** — the three dimensions below. It is NOT a security review.\n\n- Do NOT flag, fix, or report security vulnerabilities (injection, XSS, hard-coded secrets, auth flaws, etc.). Those are out of scope here and are handled by `/security-review`, which reports findings without modifying code.\n- If you happen to notice a likely security issue while reviewing, do not fix it — at most note it in one line at the end ("possible security concern in <file>:<line>, run /security-review") and continue with the reuse/quality/efficiency review.\n\n## Phase 1: Identify Changes\n\nRun `git diff` (or `git diff HEAD` if there are staged changes) to see what changed. If there are no git changes, review the most recently modified files that the user mentioned or that you edited earlier in this conversation.\n\n## Phase 2: Launch Three Review Agents in Parallel\n\nIf sub-agent tools are available (e.g. task_tool / Agent tool), launch all three agents concurrently in a single message. Pass each agent the full diff so it has the complete context. Otherwise, perform all three reviews yourself directly.\n\n### Agent 1: Code Reuse Review\n\nFor each change:\n\n1. **Search for existing utilities and helpers** that could replace newly written code. Look for similar patterns elsewhere in the codebase — common locations are utility directories, shared modules, and files adjacent to the changed ones.\n2. **Flag any new function that duplicates existing functionality.** Suggest the existing function to use instead.\n3. **Flag any inline logic that could use an existing utility** — hand-rolled string manipulation, manual path handling, custom environment checks, ad-hoc type guards, and similar patterns are common candidates.\n\n### Agent 2: Code Quality Review\n\nReview the same changes for hacky patterns:\n\n1. **Redundant state**: state that duplicates existing state, cached values that could be derived, observers/effects that could be direct calls\n2. **Parameter sprawl**: adding new parameters to a function instead of generalizing or restructuring existing ones\n3. **Copy-paste with slight variation**: near-duplicate code blocks that should be unified with a shared abstraction\n4. **Leaky abstractions**: exposing internal details that should be encapsulated, or breaking existing abstraction boundaries\n5. **Stringly-typed code**: using raw strings where constants, enums (string unions), or branded types already exist in the codebase\n6. **Unnecessary JSX nesting**: wrapper Boxes/elements that add no layout value — check if inner component props (flexShrink, alignItems, etc.) already provide the needed behavior\n7. **Unnecessary comments**: comments explaining WHAT the code does (well-named identifiers already do that), narrating the change, or referencing the task/caller — delete; keep only non-obvious WHY (hidden constraints, subtle invariants, workarounds)\n\n### Agent 3: Efficiency Review\n\nReview the same changes for efficiency:\n\n1. **Unnecessary work**: redundant computations, repeated file reads, duplicate network/API calls, N+1 patterns\n2. **Missed concurrency**: independent operations run sequentially when they could run in parallel\n3. **Hot-path bloat**: new blocking work added to startup or per-request/per-render hot paths\n4. **Recurring no-op updates**: state/store updates inside polling loops, intervals, or event handlers that fire unconditionally — add a change-detection guard so downstream consumers aren\'t notified when nothing changed. Also: if a wrapper function takes an updater/reducer callback, verify it honors same-reference returns (or whatever the "no change" signal is) — otherwise callers\' early-return no-ops are silently defeated\n5. **Unnecessary existence checks**: pre-checking file/resource existence before operating (TOCTOU anti-pattern) — operate directly and handle the error\n6. **Memory**: unbounded data structures, missing cleanup, event listener leaks\n7. **Overly broad operations**: reading entire files when only a portion is needed, loading all items when filtering for one\n\n## Phase 3: Fix Issues\n\nWait for all reviewers to complete. Aggregate their findings and fix each issue directly. If a finding is a false positive or not worth addressing, note it and move on — do not argue with the finding, just skip it.\n\nWhen done, briefly summarize what was fixed (or confirm the code was already clean).\n'


def _is_env_api_base_placeholder(env_updates: dict) -> bool:
    """检查 env_updates 中的 API_BASE 是否指向 example.* 等占位域名。"""
    return is_placeholder_api_base(str(env_updates.get("API_BASE", "") or "").strip())


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


def resolve_status_code_graph(config: dict | None, workspace: str) -> dict[str, object]:
    """Port agent_os's SDK-backed status; do not create an Agent to inspect it."""
    from jiuwenswarm.server.runtime.agent_adapter.code_graph_flags import (
        product_code_graph_config,
        resolve_code_graph_flags,
        rewrite_code_graph_limit_message,
    )

    if not resolve_code_graph_flags(config).enabled:
        return {"present": False, "state": "absent"}
    try:
        from openjiuwen.core.retrieval.code_graph.manager import get_code_graph_manager

        cfg = product_code_graph_config(config)
        stats = get_code_graph_manager(cfg).stats(workspace, config=cfg)
        if not isinstance(stats, dict):
            return {"present": False, "state": "absent"}
        payload = dict(stats)
        if payload.get("limit_exceeded"):
            payload["state"] = "unavailable"
        if payload.get("message"):
            payload["message"] = rewrite_code_graph_limit_message(payload["message"])
        return payload
    except Exception as exc:
        return {"present": False, "state": "unavailable", "message": str(exc)}


class CommandsManagement:
    def __init__(self, host):
        self.host = host

    async def _handle_command_add_dir(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        try:
            params = request.params or {}
            directory_path = params.get("path")
            remember = params.get("remember", False)
            persist: dict[str, Any]
            if directory_path is None or (
                isinstance(directory_path, str) and (not directory_path.strip())
            ):
                persist = {"ok": False, "error": "path is required"}
            else:
                persist = persist_cli_trusted_directory(str(directory_path))
            if persist.get(
                "ok"
            ) is True and host._agent_manager.has_smart_permission_lifecycle(
                get_config()
            ):
                host._agent_manager.schedule_permissions_reload()
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
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.add_dir failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": str(e),
                    "code": "BAD_REQUEST"
                    if isinstance(e, ValueError)
                    else "SESSION_CREATE_FAILED",
                },
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_command_chrome(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        try:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={},
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.chrome failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_command_compact(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from openjiuwen.harness.observability import (
            close_agent_run_span,
            open_agent_run_span,
        )
        from jiuwenswarm.agents.harness.agent_observability import (
            sync_agent_observability,
        )

        _run_span: Any = None
        summary = ""
        try:
            session_id = request.session_id or "default"
            params = request.params or {}
            channel_id = request.channel_id or "default"
            mode, sub_mode, canonical_mode = resolve_agent_request_mode(
                params.get("mode", "agent")
            )
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = host._agent_manager.get_agent_for_session_nowait(
                channel_id=channel_id, session_id=session_id
            )
            if agent is None:
                agent = await host._agent_manager.get_agent(
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
                    execution_subject = team_agent.observability_execution_subject(
                        session_id
                    )
            _trajectory_mode = deprecate_mode(canonical_mode)
            _run_span = open_agent_run_span(
                session_id=session_id,
                mode=_trajectory_mode,
                request_id=request.request_id,
                run_id=request.request_id,
                execution_subject=execution_subject,
            )
            try:
                result_data = await agent.compress_context(
                    session_id=session_id,
                    return_state=True,
                    processor_types=_MANUAL_COMPACT_PROCESSOR_TYPES,
                )
                result = result_data.get("result")
                stats = result_data.get("stats")
                state = (
                    result_data.get("state")
                    if isinstance(result_data.get("state"), dict)
                    else {}
                )
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
                        rate = round(
                            (before_tokens - after_tokens) / before_tokens * 100, 1
                        )
                    else:
                        rate = 0
                    stats_summary = f"✓ Context compacted: {after_tokens / 1000:.1f}K/{before_tokens / 1000:.1f}K tokens ({rate:.1f}% saved)"
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
                            "status": state.get("status") or "completed",
                            "phase": state.get("phase") or "active_compress",
                            "processor": state.get("processor")
                            or _extract_compact_summary_processor(summary),
                            "before": state.get("before") or {"tokens": before_tokens},
                            "after": state.get("after") or {"tokens": after_tokens},
                            "saved": state.get("saved")
                            or {
                                "tokens": before_tokens - after_tokens,
                                "percent": rate,
                            },
                            "summary": stats_summary,
                            "compact_summary": summary,
                        }
                        await host.send_push(
                            {
                                "channel_id": channel_id,
                                "session_id": session_id,
                                "payload": compression_state_payload,
                            }
                        )
                if result in {"compressed", "noop"}:
                    build_usage_event = getattr(agent, "get_context_usage_event", None)
                    if callable(build_usage_event):
                        try:
                            usage_payload = await build_usage_event(
                                session_id=session_id, request_id=request.request_id
                            )
                        except Exception:
                            logger.warning(
                                "[AgentWebSocketServer] manual context usage event failed",
                                exc_info=True,
                            )
                        else:
                            if isinstance(usage_payload, dict):
                                append_history_record(
                                    session_id=session_id,
                                    request_id=request.request_id,
                                    channel_id=channel_id,
                                    role="assistant",
                                    event_type="context.usage",
                                    content="",
                                    timestamp=_dt.datetime.now().timestamp(),
                                    extra={
                                        key: value
                                        for key, value in usage_payload.items()
                                        if key != "event_type"
                                    },
                                    mode=params.get("mode", "unknown"),
                                )
                                await host.send_push(
                                    {
                                        "channel_id": channel_id,
                                        "session_id": session_id,
                                        "payload": usage_payload,
                                    }
                                )
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
            finally:
                close_agent_run_span(_run_span, session_id=session_id, output=summary)
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.compact failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_command_compact_partial(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        try:
            session_id = request.session_id or "default"
            params = request.params or {}
            turn_index = int(params.get("turn_index", 0))
            direction = str(params.get("direction") or "from").strip()
            channel_id = request.channel_id or "default"
            mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await host._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode,
            )
            if agent is None:
                raise ValueError("Failed to get agent")
            result_data = await agent.compact_partial(
                session_id=session_id, turn_index=turn_index, direction=direction
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
            logger.exception(
                "[AgentWebSocketServer] command.compact_partial failed: %s", e
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"status": "failed", "error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_command_context(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        try:
            session_id = request.session_id or "default"
            params = request.params or {}
            channel_id = request.channel_id or "default"
            mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await host._agent_manager.get_agent(
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
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.context failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_command_recap(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 /recap 命令：生成会话快速回顾（read-only，不修改历史）"""
        host = self.host
        try:
            session_id = request.session_id or "default"
            params = request.params or {}
            channel_id = request.channel_id or "default"
            mode, sub_mode, canonical_mode = resolve_agent_request_mode(
                params.get("mode", "agent")
            )
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = await host._agent_manager.get_agent(
                channel_id=channel_id,
                mode=agent_mode,
                project_dir=resolve_request_project_dir(request),
                sub_mode=sub_mode,
            )
            if agent is None:
                raise ValueError("Failed to get agent")
            result_data = await agent.generate_recap(
                session_id=session_id, current_mode=canonical_mode
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result_data,
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.recap failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"status": "failed", "error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_command_btw(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 /btw 命令：独立、无工具、单轮 LLM 侧问题查询。

        - 获取当前会话上下文（最近消息）
        - 用隔离的 LLM 查询回答问题
        - 不修改对话历史
        - 不使用任何工具（纯文本回答）
        - 仅单轮（无后续 token 消耗）
        """
        host = self.host
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
                wire = host.services.encode_response(
                    resp, response_id=request.request_id
                )
                async with send_lock:
                    await host.services.send(ws, wire)
                return
            mode, sub_mode, _ = resolve_agent_request_mode(params.get("mode", "agent"))
            agent_mode = "agent" if mode == "auto_harness" else mode
            agent = host._agent_manager.get_agent_for_session_nowait(
                channel_id=channel_id, session_id=session_id
            )
            if agent is None:
                agent = await host._agent_manager.get_agent(
                    channel_id=channel_id,
                    mode=agent_mode,
                    project_dir=resolve_request_project_dir(request),
                    sub_mode=sub_mode,
                )
            if agent is None:
                raise ValueError("Failed to get agent")
            await agent.ensure_instance()
            result_data = await agent.generate_btw_answer(
                session_id=session_id, question=question
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
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.btw failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"status": "failed", "error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_command_diff(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        from jiuwenswarm.server.runtime.session.git_diff_status import (
            get_session_extra_history_roots,
        )
        from jiuwenswarm.server.utils.diff_service import get_diff_service

        try:
            session_id = request.session_id or "default"
            project_dir = resolve_request_project_dir(request)
            extra_history_roots = get_session_extra_history_roots(session_id)
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
            payload: dict[str, Any] = {"type": "list", "turns": turns}
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_command_simplify(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 /simplify 命令：组装代码精简审查 prompt 并返回（由前端作为消息发送给 Agent）。

        prompt 指导 Agent 分三阶段完成
        1) 识别改动（git diff）
        2) 三维度审查（复用 / 质量 / 效率）—— 子 Agent 并行审查为可选优化手段
        3) 聚合发现并直接修复
        """
        host = self.host
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
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.simplify failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_command_model(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
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
                            "error": f"API_BASE '{api_base_val}' 指向占位域名，无法实际提供服务，请配置有效的 API 地址"
                        },
                    )
                else:
                    for k, v in env_updates.items():
                        os.environ[k] = v
                    logger.info(
                        "[command.model] os.environ 已更新, MODEL_NAME=%s",
                        os.getenv("MODEL_NAME", "unknown"),
                    )
                    try:
                        from jiuwenswarm.agents.harness.common.memory.config import (
                            clear_config_cache,
                        )

                        clear_config_cache()
                        logger.info("[command.model] config cache 已清除")
                    except Exception as e:
                        logger.debug(
                            "[command.model] clear_config_cache skipped: %s", e
                        )
                    try:
                        await host._agent_manager.reload_agents_config(
                            None, env_updates
                        )
                        logger.info("[command.model] agent config 已重载")
                    except Exception as e:
                        logger.debug(
                            "[command.model] reload_agents_config skipped: %s", e
                        )
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
                    logger.info(
                        "[command.model] 切换完成: current=%s",
                        os.getenv("MODEL_NAME", "unknown"),
                    )
            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "current": os.getenv("MODEL_NAME", "unknown"),
                        "available": ["default-model"],
                    },
                )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.model failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_command_resume(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        try:
            params = request.params or {}
            query = params.get("query")
            session_id = (
                query
                if isinstance(query, str) and query.strip()
                else "sess_mock_resume"
            )
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
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.resume failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_command_session(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        try:
            session_id = request.session_id or "sess_mock"
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": session_id,
                    "remote_url": f"https://example.invalid/session/{session_id}",
                    "qr_text": f"session:{session_id}",
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.session failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_command_status(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        try:
            params = request.params or {}
            action = str(params.get("action", "overview")).strip().lower()
            if action == "usage":
                sessions, total = get_all_sessions_metadata(limit=500, offset=0)
                messages_total = sum((s.get("message_count", 0) for s in sessions))
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
                        except Exception:
                            pass
                    if created and last:
                        longest_hours = max(longest_hours, (last - created) / 3600)
                models_used = [
                    {"name": k, "count": v}
                    for k, v in sorted(model_counts.items(), key=lambda x: -x[1])
                ]
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
                config = get_config()
                session_id = request.session_id or ""
                default_models = get_default_models(config)
                active_entry = default_models[0] if default_models else {}
                mcc = active_entry.get("model_client_config", {})
                model_name = str(mcc.get("model_name", "") or config.get("model", ""))
                provider = str(
                    mcc.get("client_provider", "") or config.get("model_provider", "")
                )
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
                        workspace=workspace_dir, target_path=workspace_dir
                    )
                    memory_warnings = get_large_memory_files(project_files)
                    logger.info(
                        "[AgentWebSocketServer] memory diagnostics: workspace_dir=%s, files=%d, warnings=%d",
                        workspace_dir,
                        len(project_files),
                        len(memory_warnings),
                    )
                except Exception as exc:
                    logger.warning(
                        "[AgentWebSocketServer] memory diagnostics failed: workspace_dir=%s, error=%s",
                        workspace_dir,
                        exc,
                    )
                    memory_warnings = []
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "version": get_runtime_version(),
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
                from jiuwenswarm.common.agentos_runtime import is_agentos_runtime

                if is_agentos_runtime():
                    resp.payload["code_graph"] = resolve_status_code_graph(
                        config, workspace_dir
                    )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.status failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)
