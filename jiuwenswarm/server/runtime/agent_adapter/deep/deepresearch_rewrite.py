"""组装 deepresearch 执行 rail 与工具上下文，并完成改写快路径的会话事务。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.rails import DeepResearchExecutionRail
from jiuwenswarm.agents.harness.common.tools.deepresearch.deepresearch_rewrite_fast_path import (
    RewriteFastPathResult,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.deepresearch_rewrite_html_followup import (
    PENDING_HTML_EXPORT_STATE_KEY,
    RewriteHtmlFollowupResult,
    RewriteHtmlTarget,
    decode_html_tool_result,
    is_html_followup_request,
    target_from_commit_result,
    target_from_state,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import (
    _deepresearch_rewrite_publish_uncertain,
    _deepresearch_rewrite_replay_conflict,
    _deepresearch_rewrite_terminal_kind,
    _valid_deepresearch_rewrite_request_id,
)
from jiuwenswarm.server.runtime.runtime_scope import RuntimeScopeKey

logger = logging.getLogger(__name__)

_DEEPRESEARCH_REWRITE_REPLAY_STATE_KEY = "deepresearch_rewrite_fast_path_replays"
_DEEPRESEARCH_REWRITE_REPLAY_SCHEMA_VERSION = 1
_DEEPRESEARCH_REWRITE_REPLAY_MAX_ENTRIES = 32
_DEEPRESEARCH_REWRITE_REQUEST_ID_MAX_LENGTH = 256
_DEEPRESEARCH_REWRITE_REPLAY_MAX_BYTES = 256 * 1024
_DEEPRESEARCH_REWRITE_REPLAY_PATH_MAX_BYTES = 4096
_DEEPRESEARCH_REWRITE_REPLAY_TERMINAL_KINDS = frozenset(
    {"success", "report_delivery_failed", "publish_uncertain"}
)
_DEEPRESEARCH_REWRITE_SUCCESS_MESSAGE = (
    "本轮改写已完成。若报告已是最终版本，请回复‘生成 HTML’；"
    "如需继续改写，可直接选择下一处内容。"
)
_DEEPRESEARCH_REWRITE_DELIVERY_FAILURE_MESSAGE = (
    "改写版本已成功保留，但报告文件交付失败。"
)
_DEEPRESEARCH_REWRITE_PUBLISH_UNCERTAIN_MESSAGE = (
    "改写版本状态不确定，请刷新会话后重试。"
)


def _build_deepresearch_execution_rail(
    adapter,
) -> DeepResearchExecutionRail | None:
    """Build the native HITL bridge for deepresearch_execute."""
    try:
        rail = DeepResearchExecutionRail(model_provider=lambda: adapter._model,
                                         materials_context_provider=lambda: _get_deepresearch_material_context(adapter),
                                         mcp_context_provider=lambda: _get_deepresearch_mcp_context(adapter))
        logger.info(
            "[JiuWenSwarmDeepAdapter] DeepResearchExecutionRail create success"
        )
        return rail
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] DeepResearchExecutionRail create failed: %s",
            exc,
        )
        return None


async def _get_deepresearch_mcp_context(adapter) -> list[dict[str, Any]]:
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        get_effective_permissions_config,
        get_mcp_server_registry,
    )
    from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import get_search_mcp_context

    def request_state():
        return (getattr(adapter, "_deepresearch_mcp_snapshot", None),
                adapter._active_office_claw_mcp, adapter._current_request_route)

    def resolve_permission_config(**kwargs):
        return adapter._resolve_permission_config_for_agent(**kwargs)

    return await get_search_mcp_context(
        adapter, registry_provider=get_mcp_server_registry,
        owner_policy_provider=get_effective_permissions_config,
        request_state=request_state,
        resolve_permission_config=resolve_permission_config,
    )


def _get_deepresearch_material_context(adapter) -> dict[str, Any] | None:
    # Per-session adapter snapshot survives long-lived runner Task boundaries.
    context = copy.deepcopy(adapter._current_request_route.get("materials_context"))
    if context is None:
        return None
    if not context["files"]:
        return context
    try:
        permission_config = adapter._resolve_permission_config_for_agent(
            session_id=str(adapter._current_request_route.get("session_id") or "")
        )
    except Exception:
        # Unknown policy must not authorize material reads or expose config data.
        logger.warning("[DeepResearch] Material permission configuration unavailable")
        permission_config = {}
    enabled = permission_config.get("enabled") if isinstance(permission_config, dict) else None
    engine = getattr(getattr(adapter, "_permission_rail", None), "_engine", None)
    from openjiuwen.harness.security.models import PermissionLevel
    for item in context["files"]:
        if enabled is False:
            # A disabled permission system intentionally has no PermissionRail.
            item["read_allowed"] = True
        elif enabled is not True or engine is None:
            item["read_allowed"] = False
        else:
            level, _ = engine.check_tool_permission_directly("read_file", {"file_path": item.get("path", "")})
            item["read_allowed"] = level == PermissionLevel.ALLOW
    return context


def _get_deepresearch_tool_context(adapter) -> dict[str, str]:
    """Return the adapter-owned route that survives runner task boundaries.

    Prefers the per-session-adapter ``_current_request_route`` snapshot
    (captured from request.* at chat request entry) so deepresearch_stream
    progress pushes bind to the originating session/request even when
    concurrent requests leak module-level ``_CRON_TOOL_*`` bindings into
    background tool tasks. Falls back to ``_runtime_cron_tool_context``
    for cron-triggered paths that don't populate the snapshot.
    """
    route = adapter._current_request_route
    if route.get("session_id"):
        scope = RuntimeScopeKey.from_adapter(
            adapter, session_id=route["session_id"]
        )
        output_dir = str(route.get("output_dir") or "").strip()
        return {
            "request_id": str(route.get("request_id") or ""),
            "channel_id": str(route.get("channel_id") or ""),
            "session_id": scope.session_id,
            "service_id": scope.service_id,
            "agent_id": scope.agent_id,
            "workspace_key": scope.workspace_key,
            "output_dir": _deepresearch_artifact_output_dir(adapter,
                route.get("output_dir")
            ),
        }
    context = adapter._runtime_cron_tool_context
    metadata = context.metadata if isinstance(context.metadata, dict) else {}
    scope = RuntimeScopeKey.from_adapter(adapter, session_id=context.session_id)
    request_workspace = metadata.get("workspace_dir") or metadata.get("project_dir")
    return {
        "request_id": str(metadata.get("request_id") or ""),
        "channel_id": str(context.channel_id or ""),
        "session_id": scope.session_id,
        "service_id": scope.service_id,
        "agent_id": scope.agent_id,
        "workspace_key": scope.workspace_key,
        "output_dir": _deepresearch_artifact_output_dir(adapter,
            metadata.get("project_dir")
        ),
    }


def _deepresearch_artifact_output_dir(
    adapter, request_workspace: str | None = None
) -> str:
    """Return the current session workspace, or the legacy artifact root."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        get_agent_workspace_dir,
    )
    normalized_request_workspace = (
        request_workspace.strip()
        if isinstance(request_workspace, str)
        else ""
    )
    session_workspace = normalized_request_workspace or str(
        getattr(adapter, "_project_dir", None) or ""
    ).strip()
    if session_workspace:
        return str(Path(session_workspace).expanduser().resolve())
    workspace = Path(
        getattr(adapter, "_workspace_dir", None) or get_agent_workspace_dir()
    ).expanduser().resolve()
    return str(workspace / "projects")


def _register_deepresearch_tool_cards(adapter, tool_cards: list[Any]) -> None:
    """Register the formal DeepResearch surface as process-shared tools."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        get_deepresearch_tools,
    )
    registered_cards = [
        adapter._register_shared_tool(tool).card
        for tool in get_deepresearch_tools()
    ]
    tool_cards.extend(registered_cards)


def _active_deepresearch_session(adapter, session_id: str) -> Any | None:
    """Return the already-bound product session only for an exact id match."""
    if not isinstance(session_id, str) or not session_id:
        return None
    instance = getattr(adapter, "_instance", None)
    session = getattr(instance, "_interaction_session", None)
    get_session_id = getattr(session, "get_session_id", None)
    if session is None or not callable(get_session_id):
        return None
    try:
        active_session_id = get_session_id()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(active_session_id, str) or active_session_id != session_id:
        return None
    return session


async def _load_deepresearch_rewrite_html_target(
    adapter,
    session_id: str,
) -> RewriteHtmlTarget | None:
    """Restore the trusted HTML target from the active product session."""
    session = _active_deepresearch_session(adapter, session_id)
    if session is None:
        return None
    try:
        return target_from_state(
            session.get_state(PENDING_HTML_EXPORT_STATE_KEY)
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[DeepResearchRewriteHtmlFollowup] restore target failed "
            "session_id=%s error_type=%s",
            session_id,
            type(exc).__name__,
        )
        return None


async def _try_deepresearch_rewrite_html_followup(
    adapter,
    query: object,
    session_id: str,
) -> RewriteHtmlFollowupResult | None:
    """Handle an explicit HTML follow-up without routing through the model."""
    if not is_html_followup_request(query):
        return None

    target = await _load_deepresearch_rewrite_html_target(adapter, session_id)
    if target is None:
        return RewriteHtmlFollowupResult(
            status="error",
            error_code="TARGET_UNAVAILABLE",
            message=(
                "未找到可生成 HTML 的已完成改写版本。"
                "建议先继续改写并生成新的 Markdown 版本，再生成 HTML。"
            ),
        )

    from jiuwenswarm.agents.harness.common.tools.deepresearch import rewrite_tools

    try:
        raw_result = await rewrite_tools.deepresearch_generate_rewrite_html._func(  # pylint: disable=protected-access
            report_path=target.report_path,
            revision_id=target.revision_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[DeepResearchRewriteHtmlFollowup] HTML tool failed "
            "session_id=%s error_type=%s",
            session_id,
            type(exc).__name__,
        )
        raw_result = None
    return decode_html_tool_result(raw_result)


async def _run_deepresearch_rewrite_html_transaction(
    adapter,
    query: object,
    *,
    session_id: str,
) -> RewriteHtmlFollowupResult | None:
    """Linearize trusted-target loading and HTML generation with Core work."""
    if adapter._deepresearch_rewrite_tx_uncertain:
        return None
    instance = getattr(adapter, "_instance", None)
    send_lock = getattr(instance, "_interaction_send_lock", None)
    control_lock = getattr(instance, "_interaction_control_lock", None)
    keep_open = getattr(instance, "_should_keep_interaction_open_locked", None)
    if send_lock is None or control_lock is None or not callable(keep_open):
        return None
    async with send_lock:
        async with control_lock:
            if keep_open():
                return None
            return await _try_deepresearch_rewrite_html_followup(adapter,
                query,
                session_id,
            )


async def _try_deepresearch_rewrite_fast_path(
    adapter,
    query: object,
) -> RewriteFastPathResult | None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        run_rewrite_fast_path,
    )
    from jiuwenswarm.agents.harness.common.tools.deepresearch import rewrite_tools
    from jiuwenswarm.common.thinking.adapter import adapt_thinking
    from jiuwenswarm.common.thinking.types import thaw_llm_call_kwargs

    model_call_kwargs = thaw_llm_call_kwargs(
        adapt_thinking("off", model=adapter._model).llm_call_kwargs
    )
    return await run_rewrite_fast_path(
        query,
        model_invoke=adapter._model.invoke,
        prepare_invoke=rewrite_tools.deepresearch_prepare_rewrite._func,  # pylint: disable=protected-access
        commit_invoke=rewrite_tools.deepresearch_commit_rewrite._func,  # pylint: disable=protected-access
        model_call_kwargs=model_call_kwargs,
    )


def _decode_deepresearch_rewrite_replays(
    value: object,
) -> list[tuple[str, str, RewriteFastPathResult, dict[str, object]]]:
    try:
        encoded_state = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        return []
    invalid_state = (
        len(encoded_state) > _DEEPRESEARCH_REWRITE_REPLAY_MAX_BYTES
        or not isinstance(value, dict)
        or set(value) != {"schema_version", "entries"}
        or value.get("schema_version")
        != _DEEPRESEARCH_REWRITE_REPLAY_SCHEMA_VERSION
    )
    if invalid_state:
        return []
    raw_entries = value.get("entries")
    if (
        not isinstance(raw_entries, list)
        or len(raw_entries) > _DEEPRESEARCH_REWRITE_REPLAY_MAX_ENTRIES
    ):
        return []
    decoded = []
    seen_request_ids = set()
    for entry in raw_entries:
        if not isinstance(entry, dict) or set(entry) != {
            "request_id",
            "query_sha256",
            "terminal_kind",
            "action",
            "target",
        }:
            return []
        request_id = entry.get("request_id")
        query_sha256 = entry.get("query_sha256")
        terminal_kind = entry.get("terminal_kind")
        action = entry.get("action")
        raw_target = entry.get("target")
        target = target_from_state(raw_target)
        invalid_entry = (
            not _valid_deepresearch_rewrite_request_id(request_id)
            or not isinstance(query_sha256, str)
            or len(query_sha256) != 64
            or any(character not in "0123456789abcdef" for character in query_sha256)
            or request_id in seen_request_ids
            or terminal_kind not in _DEEPRESEARCH_REWRITE_REPLAY_TERMINAL_KINDS
            or (
                action not in {"polish", "expand", "shorten"}
                and not (terminal_kind == "publish_uncertain" and action is None)
            )
            or target is None
            or len(target.report_path.encode("utf-8"))
            > _DEEPRESEARCH_REWRITE_REPLAY_PATH_MAX_BYTES
        )
        if invalid_entry:
            return []
        commit_result = {
            "status": "completed",
            "report_delivered": terminal_kind == "success",
            "report_path": target.report_path,
            "revision_id": target.revision_id,
        }
        delivery_failed = terminal_kind == "report_delivery_failed"
        publish_uncertain = terminal_kind == "publish_uncertain"
        decoded.append(
            (
                request_id,
                query_sha256,
                RewriteFastPathResult(
                    recognized=True,
                    status="error" if publish_uncertain else "completed",
                    action=action,
                    error_code=(
                        "PUBLISH_STATE_UNCERTAIN"
                        if publish_uncertain
                        else (
                            "REPORT_DELIVERY_FAILED" if delivery_failed else None
                        )
                    ),
                    message=(
                        _DEEPRESEARCH_REWRITE_PUBLISH_UNCERTAIN_MESSAGE
                        if publish_uncertain
                        else (
                            _DEEPRESEARCH_REWRITE_DELIVERY_FAILURE_MESSAGE
                            if delivery_failed
                            else _DEEPRESEARCH_REWRITE_SUCCESS_MESSAGE
                        )
                    ),
                    usage_metadata=None,
                    prepare_ms=0.0,
                    model_ms=0.0,
                    commit_ms=0.0,
                    total_ms=0.0,
                    model_calls=0,
                    model_output_adjustments=(),
                    model_output_error_reason=None,
                    commit_result=commit_result,
                ),
                target.to_state(),
            )
        )
        seen_request_ids.add(request_id)
    return decoded


def _encode_deepresearch_rewrite_replay_entry(
    request_id: str,
    query_sha256: str,
    result: RewriteFastPathResult,
    target: RewriteHtmlTarget,
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "query_sha256": query_sha256,
        "terminal_kind": _deepresearch_rewrite_terminal_kind(result),
        "action": result.action,
        "target": target.to_state(),
    }


def _load_deepresearch_rewrite_replay(
    adapter,
    session_id: str,
    request_id: str,
    query_sha256: str,
) -> tuple[bool, RewriteFastPathResult | None]:
    session = _active_deepresearch_session(adapter, session_id)
    if session is None:
        return False, None
    try:
        state = session.get_state(_DEEPRESEARCH_REWRITE_REPLAY_STATE_KEY)
        current_target = target_from_state(
            session.get_state(PENDING_HTML_EXPORT_STATE_KEY)
        )
    except Exception:  # noqa: BLE001
        return True, None
    decoded_replays = _decode_deepresearch_rewrite_replays(state)
    if not decoded_replays and state is not None:
        is_empty_state = (
            isinstance(state, dict)
            and set(state) == {"schema_version", "entries"}
            and state.get("schema_version")
            == _DEEPRESEARCH_REWRITE_REPLAY_SCHEMA_VERSION
            and state.get("entries") == []
        )
        if not is_empty_state:
            return True, None
    for saved_request_id, saved_query_sha256, result, saved_target in decoded_replays:
        if saved_request_id == request_id:
            if (
                saved_query_sha256 != query_sha256
                or current_target is None
                or saved_target != current_target.to_state()
            ):
                return True, None
            return True, result
    return False, None


async def _run_deepresearch_rewrite_fast_path_transaction(
    adapter,
    query: object,
    *,
    session_id: str,
    request_id: str,
) -> RewriteFastPathResult | None:
    """Linearize the irreversible rewrite and its conversation checkpoint."""
    if (
        not _valid_deepresearch_rewrite_request_id(request_id)
        or adapter._deepresearch_rewrite_tx_uncertain
    ):
        return None
    instance = getattr(adapter, "_instance", None)
    send_lock = getattr(instance, "_interaction_send_lock", None)
    control_lock = getattr(instance, "_interaction_control_lock", None)
    keep_open = getattr(instance, "_should_keep_interaction_open_locked", None)
    if send_lock is None or control_lock is None or not callable(keep_open):
        return None

    async with send_lock:
        async with control_lock:
            if keep_open():
                return None
            if not isinstance(query, str):
                return None
            try:
                query_sha256 = hashlib.sha256(
                    query.encode("utf-8")
                ).hexdigest()
            except UnicodeEncodeError:
                return None
            replay_found, replay = _load_deepresearch_rewrite_replay(adapter,
                session_id,
                request_id,
                query_sha256,
            )
            if replay_found:
                return replay or _deepresearch_rewrite_replay_conflict()
            from jiuwenswarm.agents.harness.common.tools.deepresearch import (
                rewrite_tools,
            )

            published = False
            published_target = None

            def mark_published(publish_result: dict[str, object]) -> None:
                nonlocal published, published_target
                published = True
                published_target = target_from_commit_result(
                    {
                        "status": "completed",
                        "report_path": publish_result.get("report_path"),
                        "revision_id": publish_result.get("revision_id"),
                    }
                )

            observe_rewrite_publish = getattr(
                rewrite_tools, "_observe_rewrite_publish"
            )
            with observe_rewrite_publish(mark_published):
                fast_path_task = asyncio.create_task(
                    _try_deepresearch_rewrite_fast_path(adapter, query)
                )
            consumer_cancellation: asyncio.CancelledError | None = None
            task_failure: BaseException | None = None
            result = None
            try:
                result = await asyncio.shield(fast_path_task)
            except asyncio.CancelledError as exc:
                current_task = asyncio.current_task()
                inner_task_cancelled = (
                    fast_path_task.done()
                    and fast_path_task.cancelled()
                    and current_task is not None
                    and current_task.cancelling() == 0
                )
                if inner_task_cancelled:
                    fast_path_task.result()
                consumer_cancellation = exc
                published_when_cancelled = published
                if not published_when_cancelled:
                    fast_path_task.cancel()
                while not fast_path_task.done():
                    try:
                        await asyncio.shield(fast_path_task)
                    except asyncio.CancelledError as repeated_exc:
                        if consumer_cancellation is None:
                            consumer_cancellation = repeated_exc
                    except Exception as inner_exc:  # noqa: BLE001
                        task_failure = inner_exc
                        break
                if task_failure is None:
                    if fast_path_task.cancelled():
                        task_failure = asyncio.CancelledError()
                    else:
                        try:
                            result = fast_path_task.result()
                        except asyncio.CancelledError as inner_exc:
                            task_failure = inner_exc
                        except Exception as inner_exc:  # noqa: BLE001
                            task_failure = inner_exc
                if not published_when_cancelled and not published:
                    raise consumer_cancellation from None
            except Exception as exc:  # noqa: BLE001
                if not published:
                    raise
                task_failure = exc

            if published:
                result_target = (
                    target_from_commit_result(result.commit_result)
                    if isinstance(result, RewriteFastPathResult)
                    else None
                )
                invalid_published_result = (
                    task_failure is not None
                    or
                    not isinstance(result, RewriteFastPathResult)
                    or result.status != "completed"
                    or published_target is None
                    or result_target != published_target
                )
                if invalid_published_result:
                    if task_failure is not None:
                        logger.error(
                            "[DeepResearchRewriteFastPath] post-publish runner "
                            "failed session_id=%s error_type=%s",
                            session_id,
                            type(task_failure).__name__,
                        )
                    result = _deepresearch_rewrite_publish_uncertain(
                        published_target
                    )
                    if published_target is None:
                        adapter._deepresearch_rewrite_tx_uncertain = True
                        if consumer_cancellation is not None:
                            raise consumer_cancellation
                        return result
            elif task_failure is not None:
                if consumer_cancellation is not None:
                    raise consumer_cancellation
                raise task_failure
            if result is None or result.status != "completed":
                if not (
                    isinstance(result, RewriteFastPathResult)
                    and result.error_code == "PUBLISH_STATE_UNCERTAIN"
                ):
                    return result
            persistence_task = asyncio.create_task(
                _persist_deepresearch_rewrite_fast_path_turn(adapter,
                    session_id=session_id,
                    request_id=request_id,
                    query_sha256=query_sha256,
                    query=query,
                    result=result,
                )
            )
            while True:
                try:
                    persisted = await asyncio.shield(persistence_task)
                    break
                except asyncio.CancelledError as exc:
                    if consumer_cancellation is None:
                        consumer_cancellation = exc
                    if persistence_task.done():
                        if persistence_task.cancelled():
                            adapter._deepresearch_rewrite_tx_uncertain = True
                            raise exc
                        persisted = persistence_task.result()
                        break
            if (
                not persisted
                and result.error_code == "PUBLISH_STATE_UNCERTAIN"
            ):
                adapter._deepresearch_rewrite_tx_uncertain = True
            if consumer_cancellation is not None:
                raise consumer_cancellation
            if persisted:
                return result
            return replace(
                result,
                error_code="CONTEXT_PERSIST_FAILED",
                message=(
                    "改写版本已生成，但无法保存到当前会话上下文。"
                    "请刷新会话后再继续操作。"
                ),
            )


async def _persist_deepresearch_rewrite_fast_path_turn(
    adapter,
    *,
    session_id: str,
    request_id: str | None = None,
    query_sha256: str | None = None,
    query: object,
    result: RewriteFastPathResult,
) -> bool:
    """Persist a fast rewrite as one valid tool-call conversation turn."""
    if not isinstance(query, str):
        return False

    terminal_kind = _deepresearch_rewrite_terminal_kind(result)
    html_target = target_from_commit_result(result.commit_result)
    if terminal_kind is None or html_target is None:
        return False
    replay_request_id = (
        request_id
        if _valid_deepresearch_rewrite_request_id(request_id)
        and (
            terminal_kind == "report_delivery_failed"
            or terminal_kind == "publish_uncertain"
            or result.error_code is None
        )
        else None
    )
    session = _active_deepresearch_session(adapter, session_id)
    instance = getattr(adapter, "_instance", None)
    react_agent = getattr(instance, "react_agent", None)
    init_context = getattr(react_agent, "_init_context", None)
    context_engine = getattr(react_agent, "context_engine", None)
    if (
        session is None
        or not callable(init_context)
        or context_engine is None
    ):
        return False

    context = None
    context_messages_before = None
    owned_messages = ()
    previous_target = None
    previous_replay_state = None
    messages_added = False
    state_updated = False

    async def rollback() -> bool:
        restored = True
        if messages_added and context is not None:
            try:
                current_messages = context.get_messages()
                before_count = len(context_messages_before)
                owned_tail = current_messages[before_count:]
                if (
                    current_messages[:before_count] != context_messages_before
                    or len(owned_tail) > len(owned_messages)
                    or any(
                        actual != expected
                        for actual, expected in zip(owned_tail, owned_messages)
                    )
                ):
                    raise RuntimeError("context ownership cannot be proven")
                if owned_tail:
                    context.pop_messages(len(owned_tail), with_history=True)
                if context.get_messages() != context_messages_before:
                    raise RuntimeError("context rollback cannot be proven")
            except Exception as rollback_exc:  # noqa: BLE001
                logger.warning(
                    "[DeepResearchRewriteFastPath] message rollback failed "
                    "session_id=%s error_type=%s",
                    session_id,
                    type(rollback_exc).__name__,
                )
                restored = False
        if state_updated:
            try:
                restored_state = {
                    PENDING_HTML_EXPORT_STATE_KEY: previous_target,
                }
                if replay_request_id is not None:
                    restored_state[_DEEPRESEARCH_REWRITE_REPLAY_STATE_KEY] = (
                        previous_replay_state
                    )
                session.update_state(restored_state)
            except Exception as rollback_exc:  # noqa: BLE001
                logger.warning(
                    "[DeepResearchRewriteFastPath] state rollback failed "
                    "session_id=%s error_type=%s",
                    session_id,
                    type(rollback_exc).__name__,
                )
                restored = False
        if messages_added or state_updated:
            try:
                restored_context_states = await context_engine.save_contexts(session)
                if (
                    not isinstance(restored_context_states, dict)
                    or session.get_state("context") != restored_context_states
                ):
                    raise RuntimeError(
                        "context rollback local state was incomplete"
                    )
            except asyncio.CancelledError as rollback_exc:
                logger.warning(
                    "[DeepResearchRewriteFastPath] context rollback save "
                    "cancelled session_id=%s error_type=%s",
                    session_id,
                    type(rollback_exc).__name__,
                )
                restored = False
            except Exception as rollback_exc:  # noqa: BLE001
                logger.warning(
                    "[DeepResearchRewriteFastPath] context rollback save failed "
                    "session_id=%s error_type=%s",
                    session_id,
                    type(rollback_exc).__name__,
                )
                restored = False
            try:
                await session.commit()
            except asyncio.CancelledError as rollback_exc:
                logger.warning(
                    "[DeepResearchRewriteFastPath] checkpoint rollback commit "
                    "cancelled session_id=%s error_type=%s",
                    session_id,
                    type(rollback_exc).__name__,
                )
                restored = False
            except Exception as rollback_exc:  # noqa: BLE001
                logger.warning(
                    "[DeepResearchRewriteFastPath] checkpoint rollback commit "
                    "failed "
                    "session_id=%s error_type=%s",
                    session_id,
                    type(rollback_exc).__name__,
                )
                restored = False
        if not restored:
            adapter._deepresearch_rewrite_tx_uncertain = True
        return restored

    try:
        previous_target = session.get_state(PENDING_HTML_EXPORT_STATE_KEY)
        if replay_request_id is not None:
            if not isinstance(query_sha256, str):
                return False
            previous_replay_state = session.get_state(
                _DEEPRESEARCH_REWRITE_REPLAY_STATE_KEY
            )
        next_state = {
            PENDING_HTML_EXPORT_STATE_KEY: html_target.to_state(),
        }
        if replay_request_id is not None:
            existing_entries = _decode_deepresearch_rewrite_replays(
                previous_replay_state
            )
            encoded_entries = []
            for (
                saved_request_id,
                saved_query_sha256,
                saved_result,
                saved_target,
            ) in existing_entries:
                if saved_request_id == replay_request_id:
                    continue
                encoded_entries.append(
                    _encode_deepresearch_rewrite_replay_entry(
                        saved_request_id,
                        saved_query_sha256,
                        saved_result,
                        RewriteHtmlTarget(
                            report_path=saved_target["report_path"],
                            revision_id=saved_target["revision_id"],
                        ),
                    )
                )
            encoded_entries.append(
                _encode_deepresearch_rewrite_replay_entry(
                    replay_request_id,
                    query_sha256,
                    result,
                    html_target,
                )
            )
            replay_state = {
                "schema_version": _DEEPRESEARCH_REWRITE_REPLAY_SCHEMA_VERSION,
                "entries": encoded_entries[
                    -_DEEPRESEARCH_REWRITE_REPLAY_MAX_ENTRIES:
                ],
            }
            decoded_entries = _decode_deepresearch_rewrite_replays(
                replay_state
            )
            expected_request_ids = [
                entry["request_id"] for entry in replay_state["entries"]
            ]
            if [entry[0] for entry in decoded_entries] != expected_request_ids:
                return False
            next_state[_DEEPRESEARCH_REWRITE_REPLAY_STATE_KEY] = replay_state
        context = await init_context(session)
        context_messages_before = list(context.get_messages())
        tool_call_id = f"rewrite-fast-path-{uuid.uuid4().hex}"

        from openjiuwen.core.foundation.llm import (
            AssistantMessage,
            ToolMessage,
            UserMessage,
        )
        from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

        tool_call = ToolCall(
            id=tool_call_id,
            type="function",
            name="deepresearch_commit_rewrite",
            arguments="{}",
        )
        tool_result = json.dumps(
            result.commit_result,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        owned_messages = (
            UserMessage(content=query),
            AssistantMessage(content="", tool_calls=[tool_call]),
            ToolMessage(content=tool_result, tool_call_id=tool_call_id),
            AssistantMessage(content=result.message),
        )
        messages_added = True
        await context.add_messages(list(owned_messages))
        current_messages = list(context.get_messages())
        before_count = len(context_messages_before)
        if (
            current_messages[:before_count] != context_messages_before
            or tuple(current_messages[before_count:]) != owned_messages
        ):
            raise RuntimeError("context message write was incomplete")
        state_updated = True
        session.update_state(next_state)
        if any(
            session.get_state(key) != expected
            for key, expected in next_state.items()
        ):
            raise RuntimeError("session state write was incomplete")
        context_states = await context_engine.save_contexts(session)
        if (
            not isinstance(context_states, dict)
            or session.get_state("context") != context_states
        ):
            raise RuntimeError("context local state write was incomplete")
        await session.commit()
        return True
    except asyncio.CancelledError:
        await rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[DeepResearchRewriteFastPath] persist tool result failed "
            "session_id=%s error_type=%s",
            session_id,
            type(exc).__name__,
        )
        await rollback()
        return False
