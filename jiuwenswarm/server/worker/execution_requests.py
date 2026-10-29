# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.server.ws_send import send_wire_payload

_DIAGNOSIS_METHOD_NAMES = frozenset({
    "diagnosis.run", "diagnosis.list", "diagnosis.get", "diagnosis.cancel",
    "diagnosis.delete", "diagnosis.ttse.trigger",
})

import asyncio
import datetime as _dt
import logging
import sys
from typing import Any, Optional
from weakref import WeakValueDictionary

from openjiuwen.core.common.logging import server_logger
from jiuwenswarm.common.stage_timer import async_stage

from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    PLAN_MODE_EXITED_EVENT_TYPE,
)
from jiuwenswarm.agents.harness.common.a4p.rpc import get_a4p_req_methods
from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
    get_permissions_config_req_methods,
)
from jiuwenswarm.agents.harness.common.tools.acp_output_tools import (
    get_acp_output_manager,
)
from jiuwenswarm.common.config import (
    get_config,
    get_sandbox_endpoint,
    get_sandbox_startup_mode_explicit,
)
from jiuwenswarm.common.e2a.constants import (
    E2A_CANCEL_SOURCE_CLIENT_DISCONNECT,
    E2A_INTERNAL_CANCEL_SOURCE_KEY,
)
from jiuwenswarm.common.log_preview import preview_text
from gateway.common.mode_matrix import (
    deprecate_mode,
    is_plan_mode,
    is_single_agent_mode,
    is_team_mode,
)
from jiuwenswarm.common.schema.agent import (
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
)
from jiuwenswarm.common.schema.message import ReqMethod
from gateway.common.session_message import (
    SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY,
    SESSION_MESSAGE_INTERNAL_KEY,
    SESSION_MESSAGE_ORIGIN,
    SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY,
)
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.events import TERMINAL_ERROR_EVENT_TYPES, RuntimeEvent
from jiuwenswarm.runtime.plan import PlanModeController
from jiuwenswarm.runtime.request import (
    prepare_chat_turn,
    resolve_agent_request_mode,
    resolve_request_runtime_mode,
)
from jiuwenswarm.runtime.request import (
    sync_chat_request_metadata as _sync_chat_request_metadata,
)
from jiuwenswarm.runtime.session_provisioner import (
    SessionCreateInput,
    SessionForkInput,
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionError,
    SessionProvisionState,
    SessionSwitchInput,
)
from jiuwenswarm.server.runtime.agent_manager import ACP_DEFAULT_CAPABILITIES
from jiuwenswarm.server.runtime.session.session_history import (
    enqueue_history_request_completion,
    wait_for_history_receipt,
)
from jiuwenswarm.server.runtime.session.session_message_service import (
    SessionMessageExecutionResult,
    SessionMessagingError,
)
from jiuwenswarm.server.runtime.session.session_message_store import (
    SessionMessageRecord,
)
from jiuwenswarm.server.runtime.session.session_metadata import (
    build_server_push_message,
    get_session_metadata,
)
from jiuwenswarm.server.worker.execution_support import (
    _GATEWAY_ADAPTER_LEGACY_METHODS,
    _request_query_text,
    _SessionMessageResumeState,
    _StreamKeepalive,
    _TurnOutcomeTracker,
)

logger = logging.getLogger("jiuwenswarm.server.agent_ws_server")

_session_switch_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


_TRAJECTORY_REQ_METHODS = frozenset(
    {
        ReqMethod.TRAJECTORY_SUBJECTS_LIST,
        ReqMethod.TRAJECTORY_STREAM_FRAMES_GET,
        ReqMethod.TRAJECTORY_SEQUENCES_GET,
        ReqMethod.TRAJECTORY_CHECKPOINTS_GET,
        ReqMethod.TRAJECTORY_USAGE_GET,
        ReqMethod.TRAJECTORY_SUBJECT_GET,
        ReqMethod.TRAJECTORY_RAW_GET,
        ReqMethod.TRAJECTORY_ARCHIVE_OPEN,
        ReqMethod.TRAJECTORY_BLOB_READ,
        ReqMethod.TRAJECTORY_BLOB_CLOSE,
    },
)


class ExecutionRequests:
    """Request execution and durable mailbox handlers bound to one host."""

    _stateless_fallback_agents: dict[str, Any]

    async def _dispatch_gateway_adapter_request(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> bool:
        """Dispatch migrated Gateway user-business RPCs to their adapters.

        The registry is intentionally checked before the legacy handler chain:
        AgentOS requests must operate on this process's injected user directory.
        A small set of legacy handlers is retained when it owns runtime cleanup.
        """
        if (
            request.req_method is None
            or request.req_method in _GATEWAY_ADAPTER_LEGACY_METHODS
        ):
            return False
        registry = getattr(self, "_adapter_registry", None)
        if registry is None:
            return False
        adapter = registry.get(request.req_method.value)
        if adapter is None:
            return False
        try:
            response = await adapter.handle(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[AgentWebSocketServer] Gateway adapter failed: request_id=%s method=%s",
                request.request_id,
                request.req_method.value,
            )
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "INTERNAL_ERROR"},
                metadata=request.metadata,
            )
        # The mailbox belongs to this AgentServer, not the metadata adapter.
        if (
            request.req_method == ReqMethod.SESSION_GET_METADATA
            and request.channel_id == "web"
            and response.ok
        ):
            service = getattr(self, "_session_message_service", None)
            if service is not None and isinstance(response.payload, dict):
                try:
                    params = request.params if isinstance(request.params, dict) else {}
                    session_id = str(params.get("session_id") or "")
                    queue_state = await service.queue_state_for_target(
                        session_id,
                        request.user_id,
                    )
                    response.payload["queued_session_messages"] = queue_state[
                        "messages"
                    ]
                    response.payload["can_continue_queued_session_messages"] = bool(
                        queue_state.get("can_continue_queued")
                    )
                except Exception:
                    logger.warning(
                        "[AgentWebSocketServer] queue snapshot failed: session_id=%s",
                        session_id,
                        exc_info=True,
                    )
        if getattr(response, "agent_ref", None) is None:
            response.agent_ref = request.agent_ref
        wire = self.services.encode_response(response, response_id=request.request_id)
        async with send_lock:
            await self.services.send(ws, wire)
        return True

    async def _handle_gateway_cron_callback(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> bool:
        """Consume Gateway-owned cron callbacks without creating a chat turn."""
        if request.req_method not in {
            ReqMethod.CRON_JOBS_SYNC,
            ReqMethod.CRON_COMMAND_ACK,
            ReqMethod.CRON_RUN_NOW_ACK,
        }:
            return False

        params = request.params if isinstance(request.params, dict) else {}
        from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import (
            install_gateway_jobs_snapshot,
            resolve_gateway_cron_command_ack,
            resolve_gateway_run_ack,
        )

        if request.req_method == ReqMethod.CRON_JOBS_SYNC:
            install_gateway_jobs_snapshot(
                params.get("jobs", []), user_id=request.user_id
            )
        elif request.req_method == ReqMethod.CRON_COMMAND_ACK:
            resolve_gateway_cron_command_ack(
                str(params.get("command_id") or ""),
                {"data": params.get("data")},
            )
        else:
            resolve_gateway_run_ack(
                str(params.get("ack_request_id") or ""),
                str(params.get("run_id") or ""),
            )

        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"status": "ok"},
            metadata=request.metadata,
            agent_ref=request.agent_ref,
        )
        wire = self.services.encode_response(response, response_id=request.request_id)
        async with send_lock:
            await self.services.send(ws, wire)
        return True

    @staticmethod
    def _request_requires_sandbox_bootstrap(request: AgentRequest) -> bool:
        """Only configured internal Sandbox execution shares its startup barrier.

        A chat parameter cannot override the trusted server backend. Explicit
        internal mode may enable Sandbox at startup even when enabled is false;
        waiting preserves that existing Linux configuration contract.
        """
        if request.req_method == ReqMethod.COMMAND_SANDBOX:
            # Serialize config mutation with bootstrap, avoiding a late startup
            # enabling Sandbox after /sandbox disable returned successfully.
            return True
        if request.req_method not in {
            ReqMethod.CHAT_SEND,
            ReqMethod.CHAT_RESUME,
            ReqMethod.CHAT_ANSWER,
        }:
            return False
        if not sys.platform.startswith("linux"):
            return False
        try:
            return (
                get_sandbox_startup_mode_explicit() == "internal"
                and str(get_sandbox_endpoint().get("type") or "jiuwenbox")
                .strip()
                .lower()
                != "yuanrong"
            )
        except Exception:  # noqa: BLE001 - an unknown backend retains its barrier
            return True

    async def dispatch_parsed_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Dispatch an already-parsed request. Used by Front after CONTROL_READY."""
        if request.req_method == ReqMethod.CHAT_SEND:
            server_logger.info(
                "[AgentServer] chat input received: request_id=%s session_id=%s channel_id=%s query=%s",
                request.request_id,
                request.session_id,
                request.channel_id,
                preview_text(_request_query_text(request)),
            )
    
        pending_chat_request: tuple[AgentRuntime, str, str] | None = None
        registry = getattr(self, "_preparation_registry", None)
        priority_token = None
        if (
            registry is not None
            and not getattr(self, "_execution_minimum_observed", False)
            and self._should_trigger_before_chat_request_hook(request)
        ):
            priority_token = registry.pause_background()
            tokens = getattr(self, "_startup_preparation_tokens", None)
            if tokens is None:
                tokens = self._startup_preparation_tokens = set()
            tokens.add(priority_token)
        try:
            if self._request_requires_sandbox_bootstrap(request):
                preparation = getattr(self, "_sandbox_start_task", None)
                if preparation is not None:
                    from jiuwenswarm.server.worker.preparation import (
                        CapabilityUnavailableError,
                    )

                    try:
                        if registry is not None:
                            await registry.ensure("sandbox")
                        else:
                            await asyncio.shield(preparation)
                    except CapabilityUnavailableError:
                        # Startup was already best-effort. Keep explicit
                        # /sandbox enable retry and the existing backend/error
                        # resolution available after a failed warm pass.
                        logger.warning("[AgentServer] Sandbox bootstrap unavailable")
            if request.req_method is not None and request.req_method.value.startswith(
                "proactive."
            ):
                preparation = getattr(self, "proactive_preparation_task", None)
                if preparation is not None:
                    if registry is not None:
                        await registry.ensure("proactive")
                    else:
                        await asyncio.shield(preparation)
            from jiuwenswarm.server.runtime.group_requests import dispatch_group_request
    
            if await dispatch_group_request(self, ws, request, send_lock):
                return
            if request.req_method is not None and request.req_method.value.startswith(
                "assets.publish."
            ):
                await self._handle_asset_publish(ws, request, send_lock)
                return
            if request.req_method is not None and request.req_method.value.startswith(
                "personal_context."
            ):
                from jiuwenswarm.server.personal_context.ws_handler import (
                    handle_personal_context_request,
                )
    
                preparation = getattr(self, "_personal_context_start_task", None)
                if preparation is not None:
                    if registry is not None:
                        await registry.ensure("personal_context")
                    else:
                        await asyncio.shield(preparation)
                manager = getattr(self, "_agent_manager", None)
                runtime_callback = getattr(
                    manager, "set_personal_context_runtime_enabled", None
                )
                await handle_personal_context_request(
                    self._personal_context_host,
                    ws,
                    request,
                    send_lock,
                    runtime_enabled_changed=(
                        runtime_callback if callable(runtime_callback) else None
                    ),
                )
                return
    
            if (
                request.channel_id == "acp"
                and request.req_method != ReqMethod.INITIALIZE
            ):
                metadata = dict(request.metadata or {})
                ws_caps = self._get_ws_acp_client_capabilities(ws)
                metadata.setdefault(
                    "acp_client_capabilities",
                    ws_caps or self._agent_manager.get_client_capabilities("acp"),
                )
                request.metadata = metadata
    
            if await self._handle_gateway_cron_callback(ws, request, send_lock):
                return
    
            if await self._handle_lifecycle_request(ws, request, send_lock):
                return
            from jiuwenswarm.server.runtime.session.lifecycle import (
                LifecycleError,
                guard,
            )
            guarded_params = request.params if isinstance(request.params, dict) else {}
            guarded_method = request.req_method.value if request.req_method else ""
            unguarded_methods = {
                "session.list", "project.list", "project.info", "project.get_sessions",
                "project.get_cron_sessions", "project.pinned_sessions", "chat.cancel",
                "session.stop", "voice.task.checkpoint.ack", "recommendation.job",
            }
            # 仅广场/目录只读 RPC 跳过会话存活校验（#5886）。不要复用
            # _is_stateless_method_request：其前缀含 skills.toggle / rebuild 等写操作。
            if (
                guarded_method not in unguarded_methods
                and not self._skips_session_lifecycle_guard(request)
            ):
                try:
                    guard(
                        str(
                            guarded_params.get("session_id") or request.session_id or ""
                        ),
                        str(guarded_params.get("project_id") or ""),
                    )
                except LifecycleError as exc:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={"code": exc.code, "error": str(exc)},
                        metadata=request.metadata,
                    )
                    async with send_lock:
                        await self.services.send(
                            ws,
                            self.services.encode_response(
                                resp, response_id=request.request_id
                            ),
                        )
                    return
    
            if await self._dispatch_gateway_adapter_request(ws, request, send_lock):
                return
    
            if (
                request.req_method
                in {
                    ReqMethod.CHAT_SEND,
                    ReqMethod.CHAT_RESUME,
                    ReqMethod.CHAT_ANSWER,
                }
                and request.session_id
            ):
                runtime = self._execution_runtime()
                runtime.begin_chat_request(request.session_id, request.request_id)
                pending_chat_request = (runtime, request.session_id, request.request_id)
    
            # Extensions must observe and may normalize chat input before
            # automatic team binding or any other request-side effect. Runtime
            # execution below is told not to trigger this hook a second time.
            await self._trigger_before_chat_request_hook(request)
    
            if request.req_method == ReqMethod.HEARTBEAT_JOB:
                await self._handle_heartbeat_job(ws, request, send_lock)
                return
    
            if request.req_method == ReqMethod.SESSION_LIST:
                await self._handle_session_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_RENAME:
                await self._handle_session_rename(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_REBIND_PROJECT:
                await self._handle_session_rebind_project(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_SWITCH:
                await self._handle_session_switch(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_PLAN_STATUS:
                await self._handle_session_plan_status(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_INPUT_INTENT:
                await self._handle_session_input_intent(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.RECOMMENDATION:
                await self._handle_recommendation(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.PROMPT_SUGGESTION_CANCEL:
                from jiuwenswarm.agents.harness.common.recommendation.prompt_suggestion import (
                    forks,
                )

                session_id = str(request.session_id or "").strip()
                try:
                    cancelled = forks.cancel_suggestion(session_id) if session_id else False
                except Exception:  # noqa: BLE001 - a failed cancel still owns the client an answer
                    logger.warning(
                        "[AgentWebSocketServer] cancel suggestion failed: session_id=%s",
                        session_id,
                        exc_info=True,
                    )
                    cancelled = False
                response = AgentResponse(
                    request_id=request.request_id, channel_id=request.channel_id,
                    ok=bool(session_id),
                    payload=({"cancelled": cancelled} if session_id
                             else {"error": "session_id is required", "code": "BAD_REQUEST"}),
                    metadata=request.metadata,
                )
                async with send_lock:
                    wire_payload = encode_agent_response_for_wire(
                        response, response_id=request.request_id
                    )
                    await send_wire_payload(ws, wire_payload)
                return
            if request.req_method == ReqMethod.SESSION_MESSAGE_CONTINUE_QUEUED:
                await self._handle_session_message_continue_queued(
                    ws, request, send_lock
                )
                return
            if request.req_method == ReqMethod.SESSION_REWIND:
                await self._handle_session_rewind_full(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_REWIND_AND_RESTORE:
                await self._handle_session_rewind_full(
                    ws, request, send_lock, restore_files=True
                )
                return
            if request.req_method == ReqMethod.SESSION_REWIND_COMPACT:
                await self._handle_session_rewind_full(
                    ws, request, send_lock, compact=True
                )
                return
            if request.req_method == ReqMethod.SESSION_REWIND_CONTEXT:
                await self._handle_session_rewind_context(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_TEMPLATES_LIST:
                await self._handle_team_templates_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_BINDINGS_LIST:
                await self._handle_team_bindings_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_BINDING_CREATE:
                await self._handle_team_binding_create(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_BINDING_GENERATE:
                await self._handle_team_binding_generate(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_SESSION_BIND:
                await self._handle_team_session_bind(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_DELETE:
                await self._handle_team_delete(ws, request, send_lock)
                return
            if request.req_method in get_permissions_config_req_methods():
                await self._handle_permissions_config(ws, request, send_lock)
                return
            if request.req_method in get_a4p_req_methods():
                await self._handle_a4p_request(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HISTORY_GET:
                if request.is_stream:
                    await self._handle_history_get_stream(ws, request, send_lock)
                else:
                    await self._handle_history_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HISTORY_APPEND_RECORD:
                await self._handle_history_append_record(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_SNAPSHOT:
                await self._handle_team_snapshot(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_MQ_PUBLISH:
                await self._handle_team_mq_publish(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.PROACTIVE_TICK:
                await self._handle_proactive_tick(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.PROACTIVE_FEEDBACK:
                await self._handle_proactive_feedback(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_WORKFLOWS:
                await self._handle_command_workflows(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SWARMFLOW_PAUSE:
                await self._handle_swarmflow_pause(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SWARMFLOW_RESUME:
                await self._handle_swarmflow_resume(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SWARMFLOW_STOP:
                await self._handle_swarmflow_stop(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_HISTORY_GET:
                await self._handle_team_history_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_MEMBERS_GET:
                await self._handle_team_members_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.TEAM_LIST:
                await self._handle_team_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.ORG_SNAPSHOT:
                await self._handle_org_snapshot(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_ADD_DIR:
                await self._handle_command_add_dir(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_CHROME:
                await self._handle_command_chrome(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_COMPACT:
                await self._handle_command_compact(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_COMPACT_PARTIAL:
                await self._handle_command_compact_partial(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_CONTEXT:
                await self._handle_command_context(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_RECAP:
                await self._handle_command_recap(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_BTW:
                await self._handle_command_btw(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_DIFF:
                await self._handle_command_diff(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_SIMPLIFY:
                await self._handle_command_simplify(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_MODEL:
                await self._handle_command_model(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_MCP:
                await self._handle_command_mcp(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_LIST:
                await self._handle_mcp_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_SHOW:
                await self._handle_mcp_show(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_INSTALL:
                await self._handle_mcp_install(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_UNINSTALL:
                await self._handle_mcp_uninstall(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_CONNECT:
                await self._handle_mcp_connect(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_WAIT_AUTH:
                await self._handle_mcp_wait_auth(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_OAUTH_START:
                await self._handle_mcp_oauth_start(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_CANCEL_CONNECT:
                await self._handle_mcp_cancel_connect(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_DISCONNECT:
                await self._handle_mcp_disconnect(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_REGISTER_CUSTOM:
                await self._handle_mcp_register_custom(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_DELETE_CUSTOM:
                await self._handle_mcp_delete_custom(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.MCP_SAVE_CREDENTIALS:
                await self._handle_mcp_save_credentials(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_SANDBOX:
                await self._handle_command_sandbox(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_RESUME:
                await self._handle_command_resume(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_SESSION:
                await self._handle_command_session(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_STATUS:
                await self._handle_command_status(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.BROWSER_RUNTIME_RESTART:
                await self._handle_browser_runtime_restart(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.CONFIG_CACHE_CLEAR:
                await self._handle_config_cache_clear(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENT_RELOAD_CONFIG:
                await self._handle_agent_reload_config(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENT_PREWARM_SYNC:
                await self._handle_agent_prewarm_sync(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AUTH_CREDENTIALS_UPDATE:
                await self._handle_auth_credentials_update(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_LIST:
                await self._handle_extensions_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_IMPORT:
                await self._handle_extensions_import(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_DELETE:
                await self._handle_extensions_delete(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_TOGGLE:
                await self._handle_extensions_toggle(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HOOKS_LIST:
                await self._handle_hooks_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HARNESS_PACKAGES_GET:
                await self._handle_harness_packages_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HARNESS_PACKAGES_SCAN:
                await self._handle_harness_packages_scan(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HARNESS_PACKAGES_ACTIVATE:
                await self._handle_harness_packages_activate(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HARNESS_PACKAGES_DEACTIVATE:
                await self._handle_harness_packages_deactivate(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HARNESS_PACKAGES_DELETE:
                await self._handle_harness_packages_delete(ws, request, send_lock)
                return
            # Trajectory diagnosis: run, query and TTSE writes proxied by the Gateway.
            if request.req_method is not None and request.req_method.value in _DIAGNOSIS_METHOD_NAMES:
                await self._handle_diagnosis_request(ws, request, send_lock)
                return
            if request.req_method in _TRAJECTORY_REQ_METHODS:
                await self._handle_trajectory_request(ws, request, send_lock)
                return
            # RSI 优化平台：16 个 rsi.* web method 统一分发（B2）
            if isinstance(
                request.req_method, ReqMethod
            ) and request.req_method.value.startswith("rsi."):
                await self._handle_rsi_request(ws, request, send_lock)
                return
            # Schedule task management
            if request.req_method == ReqMethod.SCHEDULE_CHECK_CONFIG:
                await self._handle_schedule_request(
                    ws, request, send_lock, "check_config"
                )
                return
            if request.req_method == ReqMethod.SCHEDULE_UPDATE_CONFIG:
                await self._handle_schedule_request(
                    ws, request, send_lock, "update_config"
                )
                return
            if request.req_method == ReqMethod.SCHEDULE_CREATE:
                await self._handle_schedule_request(ws, request, send_lock, "create")
                return
            if request.req_method == ReqMethod.SCHEDULE_RUN:
                await self._handle_schedule_request(ws, request, send_lock, "run")
                return
            if request.req_method == ReqMethod.SCHEDULE_LIST:
                await self._handle_schedule_request(ws, request, send_lock, "list")
                return
            if request.req_method == ReqMethod.SCHEDULE_STATUS:
                await self._handle_schedule_request(ws, request, send_lock, "status")
                return
            if request.req_method == ReqMethod.SCHEDULE_LOGS:
                await self._handle_schedule_request(ws, request, send_lock, "logs")
                return
            if request.req_method == ReqMethod.SCHEDULE_CANCEL:
                await self._handle_schedule_request(ws, request, send_lock, "cancel")
                return
            if request.req_method == ReqMethod.SCHEDULE_DELETE:
                await self._handle_schedule_request(ws, request, send_lock, "delete")
                return
            if request.req_method == ReqMethod.ISSUE_WATCH_ONCE:
                await self._handle_schedule_request(
                    ws, request, send_lock, "issue_watch_once"
                )
                return
            if request.req_method == ReqMethod.ISSUE_STATE_LIST:
                await self._handle_schedule_request(
                    ws, request, send_lock, "issue_state_list"
                )
                return
            if request.req_method == ReqMethod.ISSUE_DELETE:
                await self._handle_schedule_request(
                    ws, request, send_lock, "issue_delete"
                )
                return
            if request.req_method == ReqMethod.ISSUE_MATRIX:
                await self._handle_schedule_request(
                    ws, request, send_lock, "issue_matrix"
                )
                return
            if request.req_method == ReqMethod.AGENTS_LIST:
                await self._handle_agents_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENTS_GET:
                await self._handle_agents_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENTS_CREATE:
                await self._handle_agents_create(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENTS_UPDATE:
                await self._handle_agents_update(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENTS_DELETE:
                await self._handle_agents_delete(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENTS_ENABLE:
                await self._handle_agents_set_enabled(ws, request, send_lock, True)
                return
            if request.req_method == ReqMethod.AGENTS_DISABLE:
                await self._handle_agents_set_enabled(ws, request, send_lock, False)
                return
            if request.req_method == ReqMethod.AGENTS_TOOLS_LIST:
                await self._handle_agents_tools_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.CHAT_CANCEL:
                params = request.params if isinstance(request.params, dict) else {}
                owner_stop = params.get("organization_owner_stop") is True or params.get("organization_stop") is True
                if owner_stop or self._is_targeted_expert_cancel_request(request):
                    await self._handle_cancel(ws, request, send_lock)
                    return
                if isinstance(request.params, dict) and request.params.get(
                    "wait_for_stop"
                ):
                    try:
                        await self._execution_runtime().stop_session_for_archive(
                            channel_id=request.channel_id or "default",
                            session_id=request.session_id or "default",
                        )
                        response = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=True,
                            payload={"success": True},
                        )
                    except Exception as exc:
                        response = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"success": False, "error": str(exc)},
                        )
                    async with send_lock:
                        await self.services.send(
                            ws,
                            self.services.encode_response(
                                response, response_id=request.request_id
                            ),
                        )
                    return
                # 中断请求：根据 intent 决定是否取消流式任务
                sid = request.session_id or "default"
                intent = (
                    request.params.get("intent", "cancel")
                    if isinstance(request.params, dict)
                    else "cancel"
                )
                cleanup_after_cancel = self._is_client_disconnect_cancel_request(
                    request
                )
    
                # 只有 cancel/supplement 才取消流式任务
                # pause/resume 不取消，因为任务仍在运行（pause 在 checkpoint 阻塞，resume 解除阻塞）
                stream_tasks: list[asyncio.Task] = []
                if intent in ("cancel", "supplement"):
                    entries = self._session_stream_tasks.get(sid, {})
                    for stream_task, stream_stop_event in list(entries.items()):
                        if stream_task.done():
                            continue
                        logger.info(
                            "[AgentWebSocketServer] cancel: 终止 session 流式任务: session_id=%s intent=%s",
                            sid,
                            intent,
                        )
                        stream_stop_event.set()
                        stream_task.cancel()
                        stream_tasks.append(stream_task)
    
                cancel_response: AgentResponse | None = None
                try:
                    # 专门处理 cancel，复用已有 agent（不再 fallthrough 到 _handle_unary）
                    # allow_create=False：找不到已有 agent 时不 fallback 新建（见 _handle_cancel docstring）。
                    cancel_response = await self._handle_cancel(
                        ws,
                        request,
                        send_lock,
                        allow_create=False,
                        send_response=not cleanup_after_cancel,
                    )
                finally:
                    if stream_tasks:
                        results = await asyncio.gather(
                            *stream_tasks, return_exceptions=True
                        )
                        for result in results:
                            if isinstance(result, Exception) and not isinstance(
                                result, asyncio.CancelledError
                            ):
                                logger.warning(
                                    "[AgentWebSocketServer] cancel: stream task cleanup failed: "
                                    "session_id=%s intent=%s error=%s",
                                    sid,
                                    intent,
                                    result,
                                )
                    if cleanup_after_cancel and intent in ("cancel", "supplement"):
                        cleanup_succeeded = (
                            await self._cleanup_client_disconnect_session_runtime(
                                request
                            )
                        )
                        if cancel_response is not None:
                            if not cleanup_succeeded:
                                cancel_response.ok = False
                                cancel_response.payload = {
                                    "event_type": "chat.interrupt_result",
                                    "success": False,
                                    "error": "session runtime cleanup failed",
                                }
                            wire = self.services.encode_response(
                                cancel_response,
                                response_id=request.request_id,
                            )
                            async with send_lock:
                                await self.services.send(ws, wire)
                return
            prewarmer = getattr(self, "_profile_prewarmer", None)
            if prewarmer is not None and self._should_trigger_before_chat_request_hook(
                request
            ):
                try:
                    resolved_mode = resolve_request_runtime_mode(request)
                    if not resolved_mode.is_team:
                        profile = (
                            "code.normal" if resolved_mode.is_code_profile else "agent"
                        )
                        prewarmer.note_use(
                            mode=profile,
                            channel_id=request.channel_id,
                            config=get_config(),
                        )
                except Exception as exc:
                    logger.debug(
                        "[Worker] profile readiness observation failed: %s",
                        type(exc).__name__,
                    )
            await self._ensure_auto_team_binding_for_chat(request)
            # chat.send 入口采集隐式反馈：用户在推荐后的文本回复关联到最近推荐。
            # best-effort，失败绝不影响主 chat 流（见方法实现）。
            if request.req_method == ReqMethod.CHAT_SEND:
                await self._try_record_implicit_feedback(request)
            if request.is_stream:
                await self._handle_stream(ws, request, send_lock)
            else:
                await self._handle_unary(ws, request, send_lock)
        except asyncio.CancelledError:
            # 流式任务被 interrupt 取消，正常退出无需报错
            logger.info(
                "[AgentWebSocketServer] 任务被取消: request_id=%s session_id=%s",
                request.request_id,
                request.session_id,
            )
        except Exception as e:
            if self.services.is_transport_closed(e):
                logger.info(
                    "[AgentWebSocketServer] Transport closed; abandoning response: %s",
                    self.services.diagnostics(
                        ws,
                        e,
                        {
                            "request_id": request.request_id,
                            "channel_id": request.channel_id,
                            "session_id": request.session_id,
                            "is_stream": request.is_stream,
                        },
                    ),
                )
                return
            logger.exception(
                "[AgentWebSocketServer] 处理请求失败: request_id=%s: %s",
                request.request_id,
                e,
            )
            wire = self._send_error_response(
                ws,
                request,
                send_lock,
                str(e),
                (
                    getattr(e, "code", None)
                    if isinstance(getattr(e, "code", None), str)
                    and getattr(e, "code", None).startswith("AGENT_GROUP_")
                    else None
                ),
            )
            async with send_lock:
                await self.services.send(ws, wire)
        except BaseException as e:
            # Last-resort net for BaseExceptionGroup escapes. A
            # BaseExceptionGroup is only an Exception subclass when *every*
            # sub-exception is, so one containing GeneratorExit/CancelledError
            # (from anyio task-group teardown — the MCP SDK's streamable/
            # SSE clients, but also openjiuwen's runner/manager task groups
            # on cooperative cancellation) sails through ``except Exception``
            # above. Per-call coercion lives in the MCP module
            # (call_timeout_patch / tools fetch); this top-level net catches
            # anything that slips past those, coerces it to an Exception,
            # and sends a failure response so the frontend doesn't hang on a
            # missing res. KeyboardInterrupt and bare CancelledError
            # (cooperative cancellation, e.g. ws disconnect) are re-raised
            # verbatim by reraise_as_exception so they propagate as signals.
            from jiuwenswarm.server.runtime.mcp.exc_group import (
                reraise_as_exception,
            )
    
            try:
                reraise_as_exception(e)
            except KeyboardInterrupt:
                logger.exception(
                    "[AgentWebSocketServer] 处理请求失败(KeyboardInterrupt): request_id=%s",
                    request.request_id,
                )
                raise
            except Exception as coerced:
                logger.exception(
                    "[AgentWebSocketServer] 处理请求失败(BaseException 逃逸): request_id=%s: %s",
                    request.request_id,
                    coerced,
                )
                wire = self._send_error_response(
                    ws,
                    request,
                    send_lock,
                    str(coerced),
                )
                async with send_lock:
                    await self.services.send(ws, wire)
        finally:
            if priority_token is not None:
                registry.resume_background(priority_token)
                self._startup_preparation_tokens.discard(priority_token)
            if pending_chat_request is not None:
                runtime, session_id, request_id = pending_chat_request
                runtime.end_chat_request(session_id, request_id)

    @staticmethod
    def _should_trigger_before_chat_request_hook(request: AgentRequest) -> bool:
        return request.req_method in (
            ReqMethod.CHAT_SEND,
            ReqMethod.CHAT_RESUME,
            ReqMethod.CHAT_ANSWER,
        )

    async def _try_record_implicit_feedback(self, request: AgentRequest) -> None:
        """Best-effort 采集隐式反馈：用户在收到推荐后的文本回复。

        主动推荐送达后，用户若直接用文本回复（"简洁点""不需要"…）而不是点卡片
        上的赞/踩按钮，这条回复就是隐式反馈。把它关联到该会话最近一条推荐，
        交由 ``record_implicit_feedback`` 做情感分类后入 buffer，供下次 tick 梯度更新。

        与 Runtime activity participant 同款 best-effort：任何异常只 log debug，
        绝不阻断主 chat 流。只在 ``chat.send`` 且来源不是 proactive 自己触发的
        推荐指令时才介入（``source=proactive_recommendation`` 是系统主动塞给主
        agent 的指令，不是用户说的话，见 proactive_adapter 触发处）。
        """
        try:
            params = request.params if isinstance(request.params, dict) else {}
            # proactive 自己触发主 agent 的指令带 source=proactive_recommendation，
            # 那不是用户输入，跳过
            if str(params.get("source") or "").strip() == "proactive_recommendation":
                return
            session_id = str(
                request.session_id or params.get("session_id") or ""
            ).strip()
            if not session_id:
                return
            query = _request_query_text(request)
            if not query:
                return

            from jiuwenswarm.agents.harness.common.recommendation.feedback_collector import (
                find_latest_recommendation,
                record_implicit_feedback,
            )

            # max_age_seconds=0 砍掉时间窗：不靠时间硬挡"无关反馈"——是否相关、是否
            # 产生梯度交给梯度更新器的模型判断（看 rec_content + user_reply 语义）。
            # 配合 record_feedback 的"同 rec_id 只采紧跟第一条、后续不覆盖"逻辑，
            # 每条推荐只关联它之后紧跟的第一条用户回复。
            latest = find_latest_recommendation(session_id, max_age_seconds=0)
            if latest is None:
                return
            rec_id = latest.get("id")
            if not rec_id:
                return

            # 已对该 rec_id 给过显式反馈（赞/踩）的话，record_feedback 的去重逻辑
            # 会自动丢弃隐式补充——无需在此预判。
            record_implicit_feedback(rec_id, query)
        except Exception as exc:
            logger.debug(
                "[AgentWebSocketServer] implicit feedback hook failed; preserving chat: "
                "session_id=%s error=%s",
                request.session_id,
                exc,
            )

    @staticmethod
    def _is_client_disconnect_cancel_request(request: AgentRequest) -> bool:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        return (
            str(metadata.get(E2A_INTERNAL_CANCEL_SOURCE_KEY) or "").strip()
            == E2A_CANCEL_SOURCE_CLIENT_DISCONNECT
        )

    async def _cleanup_client_disconnect_session_runtime(
        self, request: AgentRequest
    ) -> bool:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(request.session_id or params.get("session_id") or "").strip()
        if not session_id:
            return False
        channel_id = request.channel_id or "default"
        try:
            cleaned = await self._execution_runtime().cleanup_session(
                channel_id=channel_id,
                session_id=session_id,
            )
            logger.info(
                "[AgentWebSocketServer] client disconnect session runtime cleanup: "
                "channel_id=%s session_id=%s cleaned=%s",
                channel_id,
                session_id,
                cleaned,
            )
            return True
        except Exception as exc:
            logger.warning(
                "[AgentWebSocketServer] client disconnect session runtime cleanup failed: "
                "channel_id=%s session_id=%s error=%s",
                channel_id,
                session_id,
                exc,
            )
            return False
        finally:
            # Persisted history remains on disk, but this connection-scoped
            # marker must not grow with every short-lived TUI process. Mode
            # locks are weakly cached and disappear automatically after their
            # last active/waiting user releases them.
            self._execution_runtime().plan_controller.exited_sessions.discard(
                session_id
            )
            # 同理：内存标记不留给已断开的会话。真在 plan 里的会话靠 metadata
            # 那道判据继续被识别，不依赖这个集合。
            self._execution_runtime().plan_controller.active_sessions.discard(
                session_id
            )

    async def _trigger_before_chat_request_hook(self, request: AgentRequest) -> None:
        if self._should_trigger_before_chat_request_hook(request):
            await self.services.before_chat(request)

    async def _handle_cancel(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
        *,
        allow_create: bool = False,
        send_response: bool = True,
    ) -> AgentResponse:
        """Cancel through the transport-independent Runtime operation.

        定向判定统一走 ``_is_targeted_expert_cancel_request``（owner 未知时也判为定向）。
        这里若沿用严格版 ``_is_targeted_expert_request`` 重判，owner 未知的取消会在本函数里
        翻成 False、掉进会话级取消，把 owner 团队正在跑的 turn 一起取消——与
        ``_cancel_targeted_expert`` 的 "never the owner turn" 相悖。
        """
        params = request.params if isinstance(request.params, dict) else {}
        if params.get("organization_owner_stop") is True or params.get("organization_stop") is True:
            resp = await self._cancel_organization(request)
        elif self._is_targeted_expert_cancel_request(request):
            resp = await self._cancel_targeted_expert(request)
        else:
            from jiuwenswarm.server.runtime.organization_execution import owner_is_stopped, set_owner_stopped

            resume_owner = params.get("intent") in {"resume", "supplement"} and owner_is_stopped(request.session_id)
            if resume_owner:
                from jiuwenswarm.server.runtime.organization_owner_inbox import owner_inbox

                owner_id = str((get_session_metadata(request.session_id) or {}).get("team_name") or "")
                store = owner_inbox()
                if await asyncio.to_thread(store.enabled_at, request.session_id, owner_id) is None:
                    await asyncio.to_thread(store.enable, request.session_id, owner_id, 0.0)
                set_owner_stopped(request.session_id, False)
            try:
                resp = await self._execution_runtime().cancel_request(request, allow_create=allow_create)
            except (Exception, asyncio.CancelledError):
                if resume_owner:
                    set_owner_stopped(request.session_id, True)
                raise
            if resume_owner and (not resp.ok or (resp.payload or {}).get("success") is False):
                set_owner_stopped(request.session_id, True)

        if send_response:
            wire = self.services.encode_response(
                resp,
                response_id=request.request_id,
            )
            async with send_lock:
                await self.services.send(ws, wire)
        return resp

    @staticmethod
    def _resolve_code_language() -> str:
        """Determine the display language for code mode plan approval messages.

        Returns ``"cn"`` or ``"en"`` based on configuration.
        Defaults to ``"cn"`` if the config key is missing.
        """
        try:
            config = get_config()
            return config.get("language", "cn")
        except Exception:
            return "cn"

    @staticmethod
    def _should_sync_code_mode_state(request: AgentRequest) -> bool:
        return PlanModeController.should_sync(request)

    @staticmethod
    def _is_explicit_plan_entry_request(request: AgentRequest) -> bool:
        return PlanModeController.is_explicit_entry(request)

    def _session_mode_sync_lock(self, session_id: str) -> asyncio.Lock:
        return self._execution_runtime().plan_controller.lock_for(session_id)

    async def _push_plan_mode_exited(
        self,
        request: AgentRequest,
        *,
        exit_mode: str | None = None,
    ) -> None:
        """Notify the client that plan mode ended after user approval.

        ``mode`` 保持 TUI 已消费的语义：退出 plan 后应回到的普通模式。code 单
        agent 仍是 ``code.normal``；work 单 agent / 集群按各自 profile 动态计算。
        """
        session_id = request.session_id
        if not session_id:
            return
        if not exit_mode:
            # ``normal_mode`` 对非 plan 的 canonical 模式原样返回，所以这里不需要
            # 分情况：plan 请求得到它的退出目标，普通请求得到它自己。别写死
            # "code.normal"——最常走的这条路径（plan→normal 恢复后回调）恰好是
            # 普通请求，写死会把 code 的模式推给 work 会话。
            exit_mode = resolve_request_runtime_mode(request).normal_mode
        await self.send_push(
            {
                "channel_id": request.channel_id or "default",
                "session_id": session_id,
                "payload": {
                    "event_type": PLAN_MODE_EXITED_EVENT_TYPE,
                    "mode": exit_mode,
                },
            }
        )

    async def _check_post_process_plan_exit(
        self,
        request: AgentRequest,
        agent: Any,
    ) -> None:
        controller = self._execution_runtime().plan_controller
        for payload in await controller.check_post_process_exit(request, agent):
            await self._push_plan_mode_exited(
                request,
                exit_mode=str(payload.get("mode") or ""),
            )

    @staticmethod
    def _is_stateless_method_request(request: AgentRequest) -> bool:
        """Return True for prefix-matched read-only RPCs that skip adapter setup."""
        return request.req_method is not None and request.req_method.value.startswith(
            (
                "skills.",
                "skilldev.",
                "plugins.",
                "symphony.",
                "agent_groups.",
                "agent_templates.",
                "plugin_packages.",
            )
        )

    @staticmethod
    def _is_readonly_goal_get_request(request: AgentRequest) -> bool:
        """``command.goal`` + ``action=get``：只读查询，不得兜底新建 session metadata.

        与 skills.list 同类问题：走 ``_prepare_code_mode_chat_turn`` 会触发
        ``sync_session_request_metadata`` 在无 metadata 时写出
        ``metadata.json``。get 仍需要真实 agent（可能从 checkpointer 读已有
        Goal），故不能整段塞进 ``_is_stateless_method_request``。
        """
        if request.req_method != ReqMethod.COMMAND_GOAL:
            return False
        params = request.params if isinstance(request.params, dict) else {}
        action = str(params.get("action") or "get").strip().lower()
        return action == "get"

    async def _get_stateless_agent(self, channel_id: str) -> Any:
        """为无状态请求取 agent，**不触发任何 mode 的 adapter 重建**.

        优先用 AgentManager 已缓存的 agent 模式 agent（get_agent_nowait 命中即返回，
        不命中返回 None，绝不创建）；都没缓存时复用（或首次构造）本 server 上按
        channel 缓存的轻量 JiuWenSwarm()（**不调 create_instance**，_adapter 保持
        None）——其 process_message 内部对 skills/skilldev/plugins/symphony 的无状态
        短路会在 _ensure_adapter 之前 return，碰不到 adapter。真正的 adapter 重建
        留给 chat.send。

        相比 5084467df 原版用 get_agent(mode="agent") 作 fallback（会触发 agent 模式
        adapter 重建，治标不治本），此处彻底解耦。Fallback 必须按 channel 复用，
        否则每次 cache miss 新建 SkillManager，SkillNet install/install_status 会
        落到不同实例并误报「安装会话已过期」。
        """
        cached = self._agent_manager.get_agent_nowait(
            channel_id=channel_id, mode="agent"
        )
        if cached is not None:
            return cached
        agent = self._stateless_fallback_agents.get(channel_id)
        if agent is not None:
            return agent
        from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

        lock = getattr(self, "_stateless_fallback_lock", None)
        if lock is None:
            lock = self._stateless_fallback_lock = asyncio.Lock()
        async with lock:
            agent = self._stateless_fallback_agents.get(channel_id)
            if agent is None:
                agent = await asyncio.to_thread(JiuWenSwarm)
                self._stateless_fallback_agents[channel_id] = agent
            return agent

    @async_stage("dispatch_turn_preparation")
    async def _prepare_code_mode_chat_turn(
        self,
        request: AgentRequest,
        channel_id: str,
        *,
        sync_metadata: bool = True,
    ) -> tuple[str, str | None, Any]:
        """Compatibility wrapper around transport-independent Runtime setup."""
        return await prepare_chat_turn(
            self._agent_manager,
            request,
            channel_id,
            sync_metadata=sync_metadata,
            metadata_sync=_sync_chat_request_metadata,
        )

    def _session_may_hold_plan_state(
        self, request: AgentRequest, session_id: str
    ) -> bool:
        return self._execution_runtime().plan_controller.may_hold_state(
            request, session_id
        )

    async def _handle_session_plan_status(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """``session.plan_status``：只读查询当前会话是否处于计划模式。

        不调用 ``switch_mode`` / ``ensure_live_session_instance``，也不改
        ``_plan_active_sessions``。单 agent 有 live session 时以 ``plan_mode``
        为准（能纠正 metadata 仍是 ``*.plan``、agent 已退出的情况）；否则回退
        metadata.mode。集群的 plan 写在 metadata / team runtime，不走
        DeepAgent ``plan_mode``——同 session 上常有为 Goal 等 RPC 拉起的
        DeepAdapter，默认 ``plan_mode=normal``，若当成权威会把
        ``team.work.plan`` 误判成未在计划里。
        """
        params = request.params if isinstance(request.params, dict) else {}
        sid = str(params.get("session_id") or request.session_id or "").strip()
        if not sid:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "session_id is required", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        else:
            meta = get_session_metadata(
                sid,
                cache_bust=True,
                enable_writeback=False,
            )
            if not meta:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "session not found", "code": "NOT_FOUND"},
                    metadata=request.metadata,
                )
            else:
                metadata_mode = meta.get("mode")
                live_plan_mode = (
                    None
                    if is_team_mode(metadata_mode)
                    else self._try_read_live_plan_mode(sid)
                )
                in_plan = self._combine_session_in_plan(
                    live_plan_mode=live_plan_mode,
                    session_id=sid,
                    metadata_mode=metadata_mode,
                )
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"session_id": sid, "in_plan": in_plan},
                    metadata=request.metadata,
                )
        if getattr(resp, "agent_ref", None) is None:
            resp.agent_ref = request.agent_ref
        wire = self.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await self.services.send(ws, wire)

    def _combine_session_in_plan(
        self,
        *,
        live_plan_mode: str | None,
        session_id: str,
        metadata_mode: Any,
    ) -> bool:
        """Combine live agent plan_mode, in-process marker, and metadata.mode.

        Team sessions ignore live DeepAgent ``plan_mode``: cluster plan is
        persisted on ``metadata.mode`` (``team.*.plan``), while a live
        DeepAdapter on the same session_id typically still has the default
        ``normal`` plan_mode and would falsely report not-in-plan.
        """
        if is_team_mode(metadata_mode):
            if session_id in self._execution_runtime().plan_controller.active_sessions:
                return True
            return is_plan_mode(metadata_mode)
        if isinstance(live_plan_mode, str) and live_plan_mode.strip():
            return live_plan_mode.strip() == "plan"
        if session_id in self._execution_runtime().plan_controller.active_sessions:
            return True
        return is_plan_mode(metadata_mode)

    def _try_read_live_plan_mode(self, session_id: str) -> str | None:
        """Read ``plan_mode.mode`` from a live DeepAgent, if one is already running.

        Does not start a session or build an adapter. Missing live state is
        not an error: the caller falls back to metadata.
        """
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            resolve_live_agent_session,
        )

        agents_by_channel = getattr(self._agent_manager, "agents", None) or {}
        if not isinstance(agents_by_channel, dict):
            return None
        for channel_agents in agents_by_channel.values():
            if not isinstance(channel_agents, dict):
                continue
            for agent in channel_agents.values():
                getter = getattr(agent, "get_live_session_instance", None)
                if not callable(getter):
                    continue
                try:
                    deep_agent = getter(session_id)
                    if deep_agent is None:
                        continue
                    session = resolve_live_agent_session(deep_agent, session_id)
                    if session is None:
                        continue
                    load_state = getattr(deep_agent, "load_state", None)
                    if not callable(load_state):
                        continue
                    state = load_state(session)
                    mode = getattr(getattr(state, "plan_mode", None), "mode", None)
                except Exception as exc:
                    logger.warning(
                        "[session.plan_status] skip live agent while reading "
                        "plan_mode: session=%s error=%s",
                        session_id,
                        exc,
                    )
                    continue
                if isinstance(mode, str) and mode.strip():
                    return mode.strip()
        return None

    @staticmethod
    async def _open_plan_state_session(
        agent: Any,
        session_id: str | None,
    ) -> tuple[Any, Any, bool]:
        return await PlanModeController.open_state_session(agent, session_id)

    async def _ensure_code_mode_state(
        self,
        request: AgentRequest,
        mode: str,
        sub_mode: str,
        agent: Any,
    ) -> bool:
        controller = self._execution_runtime().plan_controller
        result = await controller.ensure_state(request, mode, sub_mode, agent)
        if not result.restored:
            for payload in result.events:
                await self._push_plan_mode_exited(
                    request,
                    exit_mode=str(payload.get("mode") or ""),
                )
        return result.restored

    async def _handle_unary(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        await self._handle_unary_impl(ws, request, send_lock)

    async def _handle_unary_impl(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """非流式处理：调用 process_message，返回一条 E2AResponse 线 JSON。"""
        if request.req_method == ReqMethod.INITIALIZE:
            await self._handle_initialize(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.SESSION_CREATE:
            await self._handle_session_create(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.SESSION_FORK:
            await self._handle_session_fork(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.ACP_TOOL_RESPONSE:
            await self._handle_acp_tool_response(ws, request, send_lock)
            return

        runtime = self._execution_runtime()
        resume_state = await self._open_session_message_resume(request)
        resume_outcome = "unknown"
        resume_error = "resumed turn ended before its outcome was confirmed"

        async def _send_control_event(event: RuntimeEvent) -> None:
            await self._send_runtime_event(
                ws,
                event,
                send_lock,
                streaming=False,
                sequence=0,
            )

        outcome_tracker = _TurnOutcomeTracker()
        try:
            if request.req_method == ReqMethod.CHAT_ANSWER:
                events = await runtime.answer_interaction(
                    request,
                    trigger_hook=False,
                    on_control_event=_send_control_event,
                )
            else:
                events = await runtime.invoke(
                    request,
                    trigger_hook=False,
                    on_control_event=_send_control_event,
                )

            for event in events:
                outcome_tracker.observe(event)
                if event.event_type == "chat.ask_user_question":
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    await self._persist_repeated_session_question(
                        request,
                        next_interrupt_request_id=str(
                            payload.get("request_id") or ""
                        ).strip(),
                        next_interrupt_source=str(payload.get("source") or "").strip(),
                        resume_state=resume_state,
                    )
                await self._send_runtime_event(
                    ws,
                    event,
                    send_lock,
                    streaming=False,
                    sequence=0,
                )
            if resume_state is not None and resume_state.waiting_user:
                outcome_tracker.waiting_user = True
            resume_outcome = outcome_tracker.outcome()
            resume_error = (
                outcome_tracker.error
                if resume_outcome == "failed"
                else outcome_tracker.unconfirmed_error()
                if resume_outcome == "unknown"
                else ""
            )
        finally:
            try:
                await self._finalize_session_message_resume(
                    request,
                    resume_state,
                    outcome=resume_outcome,
                    error=resume_error,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[SessionMessaging] failed to finalize resumed unary turn: "
                    "request_id=%s",
                    request.request_id,
                )
        logger.info(
            "[AgentWebSocketServer] 非流式响应已发送: request_id=%s",
            request.request_id,
        )

    async def _handle_stream(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        await self._handle_stream_impl(ws, request, send_lock)

    async def _handle_heartbeat_job(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Serve the stable heartbeat.job.* API from the local Rail runtime."""
        from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import (
            HeartbeatRuntimeUnavailableError,
        )

        params = request.params if isinstance(request.params, dict) else {}
        action = str(params.get("action") or "").strip()
        data = params.get("data") if isinstance(params.get("data"), dict) else {}
        effective_user_id = str(request.user_id or "").strip()
        if not effective_user_id and request.session_id:
            effective_user_id = str(
                (get_session_metadata(request.session_id) or {}).get("user_id") or ""
            ).strip()
        try:
            result = await self._heartbeat_runtime.handle_operation(
                action,
                data,
                channel_id=request.channel_id or "web",
                session_id=request.session_id or "",
                user_id=effective_user_id,
                source=("tui_rpc" if request.channel_id == "tui" else "web_rpc"),
            )
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"result": result},
                metadata=request.metadata,
            )
        except PermissionError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "FORBIDDEN"},
                metadata=request.metadata,
            )
        except KeyError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc).strip("'"), "code": "NOT_FOUND"},
                metadata=request.metadata,
            )
        except ValueError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        except HeartbeatRuntimeUnavailableError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "SERVICE_UNAVAILABLE"},
                metadata=request.metadata,
            )
        except RuntimeError as exc:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "CONFLICT"},
                metadata=request.metadata,
            )
        wire = self.services.encode_response(
            response,
            response_id=request.request_id,
        )
        async with send_lock:
            await self.services.send(ws, wire)

    async def execute_internal_heartbeat(self, request: AgentRequest) -> None:
        """Execute an AgentServer-owned Heartbeat through the shared Runtime."""
        channel_id = request.channel_id or "web"
        params = request.params if isinstance(request.params, dict) else {}
        prompt = str(params.get("content") or params.get("query") or "")
        await self.send_push(
            {
                "request_id": request.request_id,
                "channel_id": channel_id,
                "session_id": request.session_id,
                "payload": {
                    "event_type": "chat.processing_status",
                    "session_id": request.session_id,
                    "is_processing": True,
                    "is_complete": False,
                    "content": prompt,
                },
                "is_complete": False,
                "metadata": request.metadata,
            }
        )

        processing_finished = False

        def _retain_agent(agent: Any) -> None:
            self._heartbeat_runtime.retain_agent(
                request.session_id or "default",
                agent,
            )

        runtime_stream = self._execution_runtime().stream(
            request,
            trigger_hook=False,
            background=True,
            on_agent_ready=_retain_agent,
        )
        try:
            async for event in runtime_stream:
                payload = (
                    dict(event.payload)
                    if isinstance(event.payload, dict)
                    else event.payload
                )
                event_type = (
                    str(payload.get("event_type") or "")
                    if isinstance(payload, dict)
                    else ""
                )
                # 模型/Agent 级失败会以 chat.error / execution.error / team.error
                # 等事件类型送达，且 ok 默认 True（RuntimeEvent.from_agent_message）。
                # 仅靠 event.ok 会漏判这类终端失败，导致心跳本轮被误记为
                # succeeded（界面显示「上次运行成功」）。与
                # execute_internal_session_message / cron scheduler 对齐，按
                # event_type 识别终端错误。
                if not event.ok or event_type in TERMINAL_ERROR_EVENT_TYPES:
                    payload = dict(payload or {})
                    payload["event_type"] = "chat.error"
                    payload["error"] = str(
                        payload.get("error")
                        or payload.get("message")
                        or "Runtime execution failed"
                    )
                is_processing_start = (
                    isinstance(payload, dict)
                    and payload.get("event_type") == "chat.processing_status"
                    and bool(payload.get("is_processing"))
                )
                if is_processing_start and not str(payload.get("content") or ""):
                    payload = {**payload, "content": prompt}
                finishes_processing = (
                    isinstance(payload, dict)
                    and payload.get("event_type") == "chat.processing_status"
                    and payload.get("is_processing") is False
                )
                pushed = await self.send_push(
                    {
                        "request_id": event.request_id,
                        "channel_id": event.channel_id or channel_id,
                        "session_id": event.session_id or request.session_id,
                        "payload": payload,
                        "agent_ref": event.agent_ref,
                        "is_complete": event.is_complete,
                        "metadata": (
                            event.metadata
                            if event.metadata is not None
                            else request.metadata
                        ),
                    }
                )
                if finishes_processing and pushed:
                    processing_finished = True
                if not event.ok or event_type in TERMINAL_ERROR_EVENT_TYPES:
                    error_value = (
                        payload.get("error") if isinstance(payload, dict) else None
                    )
                    raise RuntimeError(str(error_value or "Runtime execution failed"))
        finally:
            try:
                await runtime_stream.aclose()
            finally:
                if not processing_finished:
                    await self.send_push(
                        {
                            "request_id": request.request_id,
                            "channel_id": channel_id,
                            "session_id": request.session_id,
                            "payload": {
                                "event_type": "chat.processing_status",
                                "session_id": request.session_id,
                                "is_processing": False,
                                "is_complete": True,
                            },
                            "is_complete": False,
                            "metadata": request.metadata,
                        }
                    )

    async def _push_session_message_status(self, record: SessionMessageRecord) -> None:
        """Push a mailbox status hint using the target Session's saved route."""

        message = build_server_push_message(
            session_id=record.target_session_id,
            request_id=record.execution_request_id or record.message_id,
            payload={
                "event_type": "session.message.updated",
                "message": {
                    "message_id": record.message_id,
                    "source_session_id": record.source_session_id,
                    "source_title": record.source_title_snapshot,
                    "target_session_id": record.target_session_id,
                    "chain_id": record.chain_id,
                    "hop_count": record.hop_count,
                    "status": record.status,
                    "input_mode": record.input_mode,
                    "created_at": record.created_at,
                    "started_at": record.started_at,
                    "finished_at": record.finished_at,
                    "error_code": record.last_error_code,
                    "error": record.last_error,
                },
            },
            fallback_channel_id="web",
        )
        if message["channel_id"] == "web":
            message["payload"]["message"]["content"] = record.content
        message.setdefault("metadata", {})[SESSION_MESSAGE_OWNER_SCOPE_METADATA_KEY] = (
            record.owner_scope_id
        )
        message["metadata"][SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY] = False
        if record.owner_scope_id == "local":
            target_metadata = await asyncio.to_thread(
                get_session_metadata,
                record.target_session_id,
                cache_bust=True,
                enable_writeback=False,
            )
            if not target_metadata or str(
                target_metadata.get("user_id") or ""
            ).strip() not in {"", "local"}:
                return
            message["metadata"][
                SESSION_MESSAGE_ANONYMOUS_OWNER_METADATA_KEY
            ] = not bool(str(target_metadata.get("user_id") or "").strip())
        await self.send_push(message)

    async def execute_internal_session_message(
        self, record: SessionMessageRecord
    ) -> SessionMessageExecutionResult:
        """Execute one claimed mailbox record in its target product Session."""

        metadata = get_session_metadata(
            record.target_session_id,
            cache_bust=True,
            enable_writeback=False,
        )
        mode = deprecate_mode(metadata.get("mode")) if metadata else ""
        channel_id = str(metadata.get("channel_id") or "").strip().lower()
        # Missing metadata, unsupported mode/channel, and Cron targets are unavailable.
        target_unsupported = (
            not metadata
            or not (is_single_agent_mode(mode) or is_team_mode(mode))
            or channel_id not in {"web", "tui"}
            or bool(str(metadata.get("cron_id") or "").strip())
        )
        if target_unsupported:
            return SessionMessageExecutionResult(
                status="failed",
                error_code="UNSUPPORTED_TARGET",
                error="target Session no longer supports Agent messaging",
            )

        stored_user_id = str(metadata.get("user_id") or "").strip()
        owner_matches = (
            not stored_user_id
            if record.owner_scope_id == "local"
            else stored_user_id == record.owner_scope_id
        )
        if not owner_matches:
            return SessionMessageExecutionResult(
                status="failed",
                error_code="NOT_FOUND_OR_FORBIDDEN",
                error="target Session ownership changed before execution",
            )

        cross_session = {
            "message_id": record.message_id,
            "source_session_id": record.source_session_id,
            "source_request_id": record.source_request_id,
            "source_tool_call_id": record.source_tool_call_id,
            "source_title": record.source_title_snapshot,
            "chain_id": record.chain_id,
            "parent_message_id": record.parent_message_id,
            "hop_count": record.hop_count,
            "language": str(get_config().get("preferred_language") or "zh"),
        }
        params: dict[str, Any] = {
            "query": record.content,
            "mode": mode,
            SESSION_MESSAGE_INTERNAL_KEY: cross_session,
        }
        if record.input_mode:
            params["input_mode"] = record.input_mode
        for key in ("project_id", "project_dir", "work_mode"):
            value = metadata.get(key)
            if value is not None and str(value).strip():
                params[key] = value
        model_name = metadata.get("model")
        if isinstance(model_name, str) and model_name.strip():
            params["model_name"] = model_name.strip()

        request = AgentRequest(
            request_id=record.execution_request_id,
            channel_id=channel_id,
            session_id=record.target_session_id,
            req_method=ReqMethod.CHAT_SEND,
            params=params,
            is_stream=True,
            timestamp=_dt.datetime.now(_dt.timezone.utc).timestamp(),
            metadata={SESSION_MESSAGE_INTERNAL_KEY: cross_session},
            user_id=stored_user_id,
        )

        execution_scope = getattr(
            getattr(self, "_current_ws", None), "execution_scope", None
        )
        if callable(execution_scope):
            with execution_scope(request):
                return await self._run_internal_session_message(
                    record, request, channel_id, cross_session
                )
        return await self._run_internal_session_message(
            record, request, channel_id, cross_session
        )

    async def _run_internal_session_message(
        self,
        record: SessionMessageRecord,
        request: AgentRequest,
        channel_id: str,
        cross_session: dict[str, Any],
    ) -> SessionMessageExecutionResult:
        mode = (
            deprecate_mode(request.params.get("mode"))
            if isinstance(request.params, dict)
            else ""
        )
        public_cross_session = {
            **cross_session,
            "content": record.content,
        }

        def _with_cross_session_marker(
            payload: dict[str, Any],
            *,
            request_id: str,
        ) -> dict[str, Any]:
            if payload.get("event_type") == "chat.input_received":
                # This input has its own author and message association, even
                # when the receiving task was started by another mailbox item.
                return payload
            return {
                **payload,
                # ask_user 等事件的 request_id 是交互关联 ID，不能覆盖；
                # 后台轮自身的稳定身份单独使用 turn_request_id。
                "request_id": payload.get("request_id") or request_id,
                "turn_request_id": request_id,
                "message_origin": SESSION_MESSAGE_ORIGIN,
                "session_message_id": record.message_id,
                "cross_session": public_cross_session,
            }

        async def start_processing() -> None:
            await self.send_push(
                build_server_push_message(
                    session_id=record.target_session_id,
                    request_id=request.request_id,
                    payload=_with_cross_session_marker(
                        {
                            "event_type": "chat.processing_status",
                            "session_id": record.target_session_id,
                            "is_processing": True,
                            "is_complete": False,
                            "content": record.content,
                        },
                        request_id=request.request_id,
                    ),
                    fallback_channel_id=channel_id,
                )
            )

        supplemental_delivery = record.input_mode == "steer"
        delivered = False
        delivery_error_code = ""
        if not supplemental_delivery:
            await start_processing()

        outcome_tracker = _TurnOutcomeTracker()
        processing_finished = False
        runtime = self._execution_runtime()
        if supplemental_delivery:
            # Resume persisted idle Sessions through the public lifecycle API.
            await runtime.start()
            await runtime.create_or_resume_session(
                channel_id=channel_id,
                session_id=record.target_session_id,
            )
        runtime_stream = runtime.stream(
            request,
            trigger_hook=False,
            background=not supplemental_delivery,
        )
        stream_completed = False
        try:
            async for event in runtime_stream:
                payload = (
                    dict(event.payload)
                    if isinstance(event.payload, dict)
                    else event.payload
                )
                event_type = (
                    str(payload.get("event_type") or "")
                    if isinstance(payload, dict)
                    else ""
                )
                if (
                    supplemental_delivery
                    and event.ok
                    and event_type == "runtime.accepted"
                ):
                    if payload.get("input_delivery") == "chat":
                        supplemental_delivery = False
                        await start_processing()
                    elif payload.get("input_boundary") == "stream":
                        delivered = True
                        continue
                elif (
                    supplemental_delivery
                    and event.ok
                    and event_type == "team.input.accepted"
                ):
                    # Team targets acknowledge with ``team.input.accepted``
                    # instead of ``runtime.accepted``: the input has entered
                    # the Team and its output arrives via the team broadcast,
                    # so this stream stays receipt-only. Publish the
                    # processing frame (cross-session marked) so the frontend
                    # renders the input turn; without it the bubble only
                    # appears after a history reload.
                    await start_processing()
                    delivered = True
                    continue
                elif (
                    supplemental_delivery
                    and event.ok
                    and event_type == "chat.processing_status_deferred"
                ):
                    # A team follow-up delivered into an already-running Team
                    # stream ends with the deferred marker instead of the
                    # accepted receipt. The input was injected by interact(),
                    # so publish the same processing frame for its turn.
                    # (First-request submit-only streams hit the accepted
                    # branch above first and skip this marker via continue.)
                    await start_processing()
                    delivered = True
                    continue
                elif (
                    record.input_mode == "steer"
                    and event.ok
                    and event_type == "runtime.accepted"
                ):
                    service = getattr(self, "_session_message_service", None)
                    if service is not None:
                        service.on_steering_fallback_started(record)
                outcome_tracker.observe(event)
                if not event.ok or event_type in TERMINAL_ERROR_EVENT_TYPES:
                    error_payload = dict(payload or {})
                    error_payload["event_type"] = "chat.error"
                    error_payload["error"] = str(
                        error_payload.get("error")
                        or error_payload.get("message")
                        or "Runtime execution failed"
                    )
                    payload = error_payload
                    delivery_error_code = str(error_payload.get("code") or "")
                    outcome_tracker.fail(str(error_payload.get("error") or ""))
                if supplemental_delivery:
                    # Receipt-only delivery does not own target task output or
                    # its processing/history completion. The original stream
                    # publishes the ordered, source-tagged input boundary.
                    continue
                if (
                    isinstance(payload, dict)
                    and payload.get("event_type") == "chat.ask_user_question"
                ):
                    interrupt_request_id = str(payload.get("request_id") or "").strip()
                    interrupt_source = str(payload.get("source") or "").strip()
                    service = getattr(self, "_session_message_service", None)
                    if not interrupt_request_id or not interrupt_source:
                        outcome_tracker.fail(
                            "Runtime emitted an uncorrelated user question"
                        )
                    elif service is None:
                        outcome_tracker.fail("Session message service is unavailable")
                    elif not await service.mark_waiting(
                        record.message_id,
                        interrupt_request_id=interrupt_request_id,
                        interrupt_source=interrupt_source,
                    ):
                        outcome_tracker.fail(
                            "Failed to persist user-question correlation"
                        )
                    if outcome_tracker.saw_error:
                        payload = {
                            "event_type": "chat.error",
                            "error": outcome_tracker.error,
                        }
                if isinstance(payload, dict):
                    payload = _with_cross_session_marker(
                        payload,
                        request_id=event.request_id or request.request_id,
                    )
                    processing_finished = processing_finished or (
                        payload.get("event_type") == "chat.processing_status"
                        and payload.get("is_processing") is False
                    )
                push = build_server_push_message(
                    session_id=record.target_session_id,
                    request_id=event.request_id or request.request_id,
                    payload=payload if isinstance(payload, dict) else {},
                    fallback_channel_id=channel_id,
                )
                push["is_complete"] = event.is_complete
                await self.send_push(push)
            stream_completed = True
        finally:
            try:
                await runtime_stream.aclose()
            finally:
                try:
                    team_processing_handed_off = (
                        is_team_mode(mode) and outcome_tracker.saw_team_input_accepted
                    )
                    if (
                        not supplemental_delivery
                        and not processing_finished
                        and not team_processing_handed_off
                    ):
                        await self.send_push(
                            build_server_push_message(
                                session_id=record.target_session_id,
                                request_id=request.request_id,
                                payload=_with_cross_session_marker(
                                    {
                                        "event_type": "chat.processing_status",
                                        "session_id": record.target_session_id,
                                        "is_processing": False,
                                        "is_complete": True,
                                    },
                                    request_id=request.request_id,
                                ),
                                fallback_channel_id=channel_id,
                            )
                        )
                finally:
                    if not stream_completed or outcome_tracker.outcome() in {
                        "failed",
                        "unknown",
                    }:
                        release = getattr(
                            runtime, "release_session_message_interactions", None
                        )
                        if callable(release):
                            await release(
                                record.target_session_id, request_id=request.request_id
                            )

        if is_team_mode(mode) and outcome_tracker.saw_team_input_accepted:
            return SessionMessageExecutionResult(status="delivered")

        if supplemental_delivery:
            if outcome_tracker.outcome() == "failed":
                return SessionMessageExecutionResult(
                    status="unknown"
                    if delivery_error_code == "SESSION_INPUT_DELIVERY_UNKNOWN"
                    else "failed",
                    error_code=delivery_error_code or "DELIVERY_FAILED",
                    error=outcome_tracker.error
                    or "Runtime rejected supplemental input",
                )
            if delivered:
                return SessionMessageExecutionResult(status="delivered")
            return SessionMessageExecutionResult(
                status="unknown",
                error_code="DELIVERY_NOT_CONFIRMED",
                error="Runtime did not confirm supplemental delivery",
            )

        outcome = outcome_tracker.outcome()
        terminal_status = {
            "succeeded": "success",
            "failed": "failed",
            "waiting_user": "waiting_user",
            "unknown": "unknown",
        }[outcome]
        try:
            receipt = enqueue_history_request_completion(
                record.target_session_id,
                request.request_id,
                terminal_status=terminal_status,
            )
            if receipt is not None:
                await wait_for_history_receipt(receipt, timeout=5.0)
        except BaseException:
            if outcome == "waiting_user":
                release = getattr(runtime, "release_session_message_interactions", None)
                if callable(release):
                    await release(
                        record.target_session_id, request_id=request.request_id
                    )
            raise

        if outcome == "failed":
            return SessionMessageExecutionResult(
                status="failed",
                error_code="EXECUTION_FAILED",
                error=outcome_tracker.error or "Runtime execution failed",
            )
        if outcome == "unknown":
            return SessionMessageExecutionResult(
                status="unknown",
                error_code="EXECUTION_NOT_COMPLETED",
                error=outcome_tracker.unconfirmed_error(),
            )
        if outcome == "waiting_user":
            return SessionMessageExecutionResult(status="waiting_user")
        return SessionMessageExecutionResult(
            status="delivered" if is_team_mode(mode) else "succeeded"
        )

    async def _handle_stream_impl(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """流式处理：调用 process_message_stream，逐条发送 E2AResponse 线 JSON。"""
        session_id = self._organization_stream_key(request)
        targeted_expert = self._is_targeted_expert_chat(request)
        resume_state = None if targeted_expert else await self._open_session_message_resume(request)
        resume_outcome = "unknown"
        resume_error = "resumed turn ended before its outcome was confirmed"
        current_task = asyncio.current_task()
        stream_stop_event = asyncio.Event()
        uses_session_runtime = AgentRuntime.uses_session_runtime(request)
        if current_task is not None and (targeted_expert or not uses_session_runtime):
            self._session_stream_tasks.setdefault(session_id, {})[current_task] = stream_stop_event
            if targeted_expert:
                if not hasattr(self, "_expert_stream_requests"):
                    self._expert_stream_requests = {}
                self._expert_stream_requests[current_task] = request

        chunk_count = 0
        outcome_tracker = _TurnOutcomeTracker()
        keepalive = _StreamKeepalive(
            ws,
            request,
            send_lock,
            services=self.services,
        )
        runtime_stream: Any | None = None

        async def _send_control_event(event: RuntimeEvent) -> None:
            await self._send_runtime_event(
                ws,
                event,
                send_lock,
                streaming=True,
                sequence=chunk_count,
            )

        try:
            if targeted_expert:
                from jiuwenswarm.server.runtime.organization_execution import (
                    OrganizationTurn,
                    register_turn,
                )

                register_turn(OrganizationTurn(
                    str(request.session_id or ""), str(request.params["target_team_id"]),
                    request.request_id, "org_expert_direct",
                ))
                runtime_stream = self._targeted_expert_runtime_stream(request)
            else:
                runtime_stream = self._execution_runtime().stream(
                    request,
                    trigger_hook=False,
                    on_control_event=_send_control_event,
                )
            keepalive.start()
            async for event in runtime_stream:
                # Runtime control events normally use the callback above. Keep
                # compatibility with custom Runtime implementations without
                # consuming a wire sequence number or resetting the keepalive timer.
                if event.event_type == PLAN_MODE_EXITED_EVENT_TYPE:
                    await _send_control_event(event)
                    continue
                outcome_tracker.observe(event)
                if event.event_type == "chat.ask_user_question":
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    await self._persist_repeated_session_question(
                        request,
                        next_interrupt_request_id=str(
                            payload.get("request_id") or ""
                        ).strip(),
                        next_interrupt_source=str(payload.get("source") or "").strip(),
                        resume_state=resume_state,
                    )
                chunk_count += 1
                # 通知 keepalive 有真实 chunk 发送，重置空闲计时。
                keepalive.notify_activity(terminal=event.is_complete)
                try:
                    sent_original = await self._send_runtime_event(
                        ws,
                        event,
                        send_lock,
                        streaming=True,
                        sequence=chunk_count - 1,
                    )
                    if not sent_original:
                        logger.warning(
                            "[AgentWebSocketServer] 流式响应因单个 chunk 超限而停止: "
                            "request_id=%s seq=%s",
                            request.request_id,
                            chunk_count - 1,
                        )
                        return
                except Exception as exc:
                    if not self.services.is_transport_closed(exc):
                        raise
                    logger.info(
                        "[AgentWebSocketServer] 流式响应停止，WebSocket 已关闭: request_id=%s",
                        request.request_id,
                    )
                    return
            if resume_state is not None and resume_state.waiting_user:
                outcome_tracker.waiting_user = True
            resume_outcome = outcome_tracker.outcome()
            resume_error = (
                outcome_tracker.error
                if resume_outcome == "failed"
                else outcome_tracker.unconfirmed_error()
                if resume_outcome == "unknown"
                else ""
            )
        finally:
            try:
                # 尽早阻止新的 keepalive；Runtime 清理仍先完成，以尽快释放其资源。
                keepalive.signal_stop()
                try:
                    if runtime_stream is not None:
                        close_stream = getattr(runtime_stream, "aclose", None)
                        if callable(close_stream):
                            await close_stream()
                finally:
                    try:
                        # 显式停止并唤醒 keepalive；Task.cancel 只作为有界的兜底。
                        await keepalive.stop()
                    finally:
                        # 清除自身的宿主生命周期记录；同 session 的其它请求不受影响。
                        entries = self._session_stream_tasks.get(session_id)
                        getattr(self, "_expert_stream_requests", {}).pop(current_task, None)
                        if entries is not None and current_task is not None:
                            entries.pop(current_task, None)
                            if not entries:
                                self._session_stream_tasks.pop(session_id, None)
                        if targeted_expert:
                            from jiuwenswarm.server.runtime.organization_execution import (
                                unregister_turn,
                            )

                            unregister_turn()
            finally:
                try:
                    await self._finalize_session_message_resume(
                        request,
                        resume_state,
                        outcome=resume_outcome,
                        error=resume_error,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[SessionMessaging] failed to finalize resumed stream turn: "
                        "request_id=%s",
                        request.request_id,
                    )
        logger.info(
            "[AgentWebSocketServer] 流式响应已发送: request_id=%s 共 %s 个 chunk",
            request.request_id,
            chunk_count,
        )

    async def _persist_repeated_session_question(
        self,
        request: AgentRequest,
        *,
        next_interrupt_request_id: str,
        next_interrupt_source: str,
        resume_state: _SessionMessageResumeState | None = None,
    ) -> bool | None:
        """Persist a resumed mailbox turn's next question before it is pushed."""

        if resume_state is None:
            resume_state = await self._open_session_message_resume(request)
        if resume_state is None:
            return None
        service = getattr(self, "_session_message_service", None)
        if service is None:
            return None
        next_request_id = str(next_interrupt_request_id or "").strip()
        next_source = str(next_interrupt_source or "").strip()
        if not next_request_id or not next_source:
            raise RuntimeError("Runtime emitted an uncorrelated follow-up question")
        persisted = await service.complete_waiting_after_resume(
            resume_state.target_session_id,
            interrupt_request_id=resume_state.interrupt_request_id,
            interrupt_source=resume_state.interrupt_source,
            waiting_user=True,
            failed=False,
            next_interrupt_request_id=next_request_id,
            next_interrupt_source=next_source,
        )
        if not persisted:
            raise RuntimeError("Failed to persist follow-up question correlation")
        resume_state.interrupt_request_id = next_request_id
        resume_state.interrupt_source = next_source
        resume_state.waiting_user = True
        return True

    async def _open_session_message_resume(
        self,
        request: AgentRequest,
    ) -> _SessionMessageResumeState | None:
        """Capture the mailbox identity before executing an interrupt answer."""

        from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
            is_interrupt_resume_payload,
        )

        if not is_interrupt_resume_payload(request.params):
            return None
        service = getattr(self, "_session_message_service", None)
        if service is None:
            return None
        params = request.params if isinstance(request.params, dict) else {}
        interrupt_request_id = str(params.get("request_id") or "").strip()
        interrupt_source = str(params.get("source") or "").strip()
        target_session_id = request.session_id or "default"
        record = await service.find_waiting_for_resume(
            target_session_id,
            interrupt_request_id,
            interrupt_source,
        )
        if record is None:
            return None
        request.trusted_session_message_route = {
            "message_id": record.message_id,
            "chain_id": record.chain_id,
            "parent_message_id": record.parent_message_id,
            "hop_count": record.hop_count,
        }
        return _SessionMessageResumeState(
            message_id=record.message_id,
            target_session_id=target_session_id,
            interrupt_request_id=interrupt_request_id,
            interrupt_source=interrupt_source,
        )

    async def _finalize_session_message_resume(
        self,
        request: AgentRequest,
        resume_state: _SessionMessageResumeState | None,
        *,
        outcome: str,
        error: str,
    ) -> None:
        """Persist history and resolve a resumed mailbox item exactly once."""

        if resume_state is None:
            return
        if outcome not in {"succeeded", "failed", "waiting_user", "unknown"}:
            raise ValueError(f"unsupported resumed-turn outcome: {outcome}")

        terminal_status = {
            "succeeded": "success",
            "failed": "failed",
            "waiting_user": "waiting_user",
            "unknown": "unknown",
        }[outcome]
        error_code = (
            "EXECUTION_FAILED"
            if outcome == "failed"
            else "RESUME_OUTCOME_UNKNOWN"
            if outcome == "unknown"
            else ""
        )
        try:
            receipt = enqueue_history_request_completion(
                resume_state.target_session_id,
                request.request_id,
                terminal_status=terminal_status,
            )
            if receipt is not None:
                await wait_for_history_receipt(receipt, timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            outcome = "unknown"
            error_code = "HISTORY_PERSISTENCE_UNCONFIRMED"
            error = f"history completion was not confirmed: {exc}"

        if outcome == "waiting_user":
            return
        service = getattr(self, "_session_message_service", None)
        if service is None:
            return
        await service.finalize_waiting_message(
            message_id=resume_state.message_id,
            target_session_id=resume_state.target_session_id,
            status=outcome,
            error_code=error_code,
            error=error,
        )
        if outcome in {"failed", "unknown"}:
            release = getattr(
                self._execution_runtime(), "release_session_message_interactions", None
            )
            if callable(release):
                await release(
                    resume_state.target_session_id,
                    request_id=f"session-message-{resume_state.message_id}",
                )

    async def _complete_waiting_session_message_after_external_turn(
        self,
        request: AgentRequest,
        *,
        waiting_user: bool,
        waiting_correlation_persisted: bool,
        failed: bool,
        error: str,
        next_interrupt_request_id: str,
        next_interrupt_source: str,
    ) -> None:
        """Compatibility wrapper for resolving a resumed mailbox turn."""

        from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
            is_interrupt_resume_payload,
        )

        if not is_interrupt_resume_payload(request.params):
            return
        service = getattr(self, "_session_message_service", None)
        if service is None:
            return
        if waiting_user and not waiting_correlation_persisted:
            return
        params = request.params if isinstance(request.params, dict) else {}
        interrupt_request_id = (
            next_interrupt_request_id
            if waiting_correlation_persisted
            else str(params.get("request_id") or "").strip()
        )
        interrupt_source = (
            next_interrupt_source
            if waiting_correlation_persisted
            else str(params.get("source") or "").strip()
        )
        target_session_id = request.session_id or "default"
        record = await service.find_waiting_for_resume(
            target_session_id,
            interrupt_request_id,
            interrupt_source,
        )
        if record is None:
            return
        state = _SessionMessageResumeState(
            message_id=record.message_id,
            target_session_id=target_session_id,
            interrupt_request_id=interrupt_request_id,
            interrupt_source=interrupt_source,
            waiting_user=waiting_user,
        )
        await self._finalize_session_message_resume(
            request,
            state,
            outcome=(
                "failed" if failed else "waiting_user" if waiting_user else "succeeded"
            ),
            error=error,
        )

    async def _send_runtime_event(
        self,
        ws: Any,
        event: RuntimeEvent,
        send_lock: asyncio.Lock,
        *,
        streaming: bool,
        sequence: int,
    ) -> bool:
        if event.event_type == PLAN_MODE_EXITED_EVENT_TYPE:
            await self.send_push(
                {
                    "channel_id": event.channel_id,
                    "session_id": event.session_id,
                    "payload": event.payload,
                }
            )
            return True
        if not event.ok and event.event_type == "runtime.error":
            # Before Runtime extraction, an Agent execution exception escaped
            # to the AgentServer request boundary and was encoded as a unary
            # AgentResponse error even for a streaming request. Keep that
            # external wire contract; ``runtime.error`` is an internal Runtime
            # event used by in-process clients and must not leak onto E2A.
            error_value = (
                event.payload.get("error") if isinstance(event.payload, dict) else None
            )
            message = AgentResponse(
                request_id=event.request_id,
                channel_id=event.channel_id,
                ok=False,
                payload={
                    "error": str(
                        error_value
                        if error_value is not None
                        else "Runtime execution failed"
                    )
                },
                agent_ref=event.agent_ref,
                metadata=event.metadata,
            )
            wire = self.services.encode_response(
                message,
                response_id=event.request_id,
            )
        elif streaming:
            payload = dict(event.payload) if event.payload is not None else None
            if not event.ok:
                # AgentResponseChunk has no `ok` field. Reuse the established
                # chat.error contract so the E2A codec preserves failed status.
                payload = dict(payload or {})
                payload["event_type"] = "chat.error"
                payload.setdefault("error", "Runtime execution failed")
            if isinstance(payload, dict) and payload.get("event_type") == "chat.error":
                # 集群的失败是 ok 事件里的 chat.error（team.error 转过来的），也要分类
                self._annotate_model_error(payload, event.session_id)
            message = AgentResponseChunk(
                request_id=event.request_id,
                channel_id=event.channel_id,
                payload=payload,
                is_complete=event.is_complete,
                agent_ref=event.agent_ref,
                metadata=event.metadata,
            )
            wire = self.services.encode_chunk(
                message,
                response_id=event.request_id,
                sequence=sequence,
            )
        else:
            message = AgentResponse(
                request_id=event.request_id,
                channel_id=event.channel_id,
                ok=event.ok,
                payload=event.payload,
                agent_ref=event.agent_ref,
                metadata=event.metadata,
            )
            wire = self.services.encode_response(
                message,
                response_id=event.request_id,
            )
        async with send_lock:
            return await self.services.send(ws, wire)

    async def _try_start_heartbeat_runtime(self) -> bool:
        """Best-effort idempotent recovery after checkpointer readiness."""
        if self._heartbeat_runtime.is_available:
            return True
        try:
            await self._heartbeat_runtime.start()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] Heartbeat runtime is not ready yet: %s",
                exc,
            )
            return False

    async def _ensure_auto_team_binding_for_chat(
        self, request: AgentRequest
    ) -> Any | None:
        """Forward an existing team binding without creating one from the query."""
        if request.req_method != ReqMethod.CHAT_SEND:
            return None

        params = request.params if isinstance(request.params, dict) else {}
        if not isinstance(request.params, dict):
            request.params = params
        session_id = str(request.session_id or params.get("session_id") or "").strip()
        if not session_id:
            return None

        metadata = get_session_metadata(session_id, cache_bust=True)
        raw_mode = params.get("mode")
        effective_mode = (
            raw_mode
            if isinstance(raw_mode, str) and raw_mode.strip()
            else metadata.get("mode")
        )
        _, _, canonical_mode = resolve_agent_request_mode(effective_mode)
        if not self._is_team_metadata_mode({"mode": canonical_mode}):
            return None

        existing_team_name = str(metadata.get("team_name") or "").strip()
        if existing_team_name:
            has_input = any(params.get(key) for key in ("content", "query", "media_items", "files", "attachments"))
            if has_input and not params.get("attach_only") and not self._is_targeted_expert_chat(request):
                if metadata.get("organization_owner_stopped") is True:
                    from jiuwenswarm.server.runtime.organization_execution import set_owner_stopped
                    from jiuwenswarm.server.runtime.organization_owner_inbox import owner_inbox

                    store = owner_inbox()
                    if await asyncio.to_thread(store.enabled_at, session_id, existing_team_name) is None:
                        await asyncio.to_thread(store.enable, session_id, existing_team_name, 0.0)
                    set_owner_stopped(session_id, False)
            params.setdefault("team_name", existing_team_name)
            template_id = str(metadata.get("team_template_id") or "").strip()
            if template_id:
                params.setdefault("team_template_id", template_id)
            return existing_team_name
        return None

    @staticmethod
    def _is_team_metadata_mode(metadata: dict[str, Any]) -> bool:
        return is_team_mode(metadata.get("mode"))

    def _send_error_response(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
        error: str,
        code: str | None = None,
    ) -> dict[str, Any]:
        """Build a failure through the injected host delivery contract."""
        return self.services.error_payload(request, error, code)

    @classmethod
    def _organization_stream_key(cls, request: AgentRequest) -> str:
        params = request.params if isinstance(request.params, dict) else {}
        # session_id 的取法三处原本不一致（`request.session_id or "default"` 与
        # `str(request.session_id or "").strip()` 与 `request.session_id or params.get(...)`）：
        # 取消请求只把 session_id 放在 params 里时，注册侧算出的 key 是 `<sid>::organization::<team>`，
        # 取消侧算出的却是 `default::organization::<team>`，查表落空——专家流没被取消，
        # 响应还可能报成功。这里统一成「请求字段优先、params 兜底」。
        session_id = request.session_id or params.get("session_id") or "default"
        team_id = str(params.get("target_team_id") or "").strip()
        return (
            f"{session_id}::organization::{team_id}"
            if cls._is_targeted_expert_request(request) else session_id
        )

    @classmethod
    def _is_targeted_expert_chat(cls, request: AgentRequest) -> bool:
        """Return whether chat.send explicitly targets a non-owner Team."""
        if request.req_method != ReqMethod.CHAT_SEND:
            return False
        return cls._is_targeted_expert_request(request)

    async def _reload_after_config_save(
        self,
        updated_keys: Any = None,
        *,
        env_updates: dict[str, Any] | None = None,
        config_payload: dict[str, Any] | None = None,
        reload_options: dict[str, Any] | None = None,
    ) -> bool:
        """Apply a ConfigAdapter save to the agents in this process."""
        from jiuwenswarm.runtime.config_reload import (
            reload_runtime_after_config_save,
        )

        return await reload_runtime_after_config_save(
            self._agent_manager,
            updated_keys,
            env_updates=env_updates,
            config_payload=config_payload,
            reload_options=reload_options,
        )

    async def _handle_initialize(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 initialize 方法（非流式）.

        调用 AgentManager.initialize 完成初始化，返回 capabilities。

        Args:
            ws: WebSocket 连接
            request: AgentRequest
            send_lock: 发送锁
        """
        logger.info(
            "[AgentServer] initialize: request_id=%s channel_id=%s",
            request.request_id,
            request.channel_id,
        )

        try:
            params = request.params if isinstance(request.params, dict) else {}
            client_capabilities = params.get("clientCapabilities", {})
            logger.info(
                "[AgentServer] initialize clientCapabilities: %s",
                client_capabilities,
            )

            extra_config = {
                "protocol_version": params.get("protocolVersion", "0.1.0"),
                "client_capabilities": client_capabilities,
            }
            if request.channel_id == "acp":
                self._set_ws_acp_client_capabilities(ws, client_capabilities)

            channel_id = request.channel_id or "default"
            capabilities = await self._agent_manager.initialize(
                channel_id=channel_id,
                extra_config=extra_config,
            )
            if capabilities is None:
                capabilities = ACP_DEFAULT_CAPABILITIES.copy()

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=capabilities,
            )
            wire = self.services.encode_response(resp, response_id=request.request_id)
            async with send_lock:
                await self.services.send(ws, wire)

            logger.info(
                "[AgentServer] initialize completed: capabilities=%s", capabilities
            )

        except Exception as e:
            logger.exception("[AgentServer] initialize failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = self.services.encode_response(resp, response_id=request.request_id)
            async with send_lock:
                await self.services.send(ws, wire)

    async def _handle_session_create(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Translate ``session.create`` between WebSocket wire and Runtime."""
        operation = "session.create"
        logger.info(
            "[AgentServer] %s: request_id=%s",
            operation,
            request.request_id,
        )
    
        runtime = None
        prepared = None
        response_delivered = False
    
        async def abort_prepared(
            primary_error: BaseException | None,
        ) -> None:
            if runtime is None:
                return
            if prepared is None:
                return
            if prepared.state is not SessionProvisionState.PREPARED:
                return
            try:
                await runtime.abort_session_provision(prepared)
            except asyncio.CancelledError:
                if primary_error is None:
                    raise
                logger.warning(
                    "[AgentServer] session.create abort was cancelled while "
                    "preserving %s",
                    type(primary_error).__name__,
                )
            except Exception as abort_error:  # noqa: BLE001
                logger.warning(
                    "[AgentServer] session.create abort failed while preserving %s: %s",
                    (
                        type(primary_error).__name__
                        if primary_error is not None
                        else "normal completion"
                    ),
                    abort_error,
                )
    
        try:
            channel_id = request.channel_id or "default"
            params = request.params if isinstance(request.params, dict) else {}
            persist_session_supplied = "persist_session" in params
            raw_persist_session = params.get("persist_session", False)
            if not isinstance(raw_persist_session, bool):
                raise SessionProvisionError(
                    "persist_session must be a boolean",
                    code="BAD_REQUEST",
                )
    
            if "moa_enabled" in params and not isinstance(params["moa_enabled"], bool):
                raise SessionProvisionError("moa_enabled must be a boolean", code="BAD_REQUEST")
    
            explicit_work_mode_marker = params.get("_work_mode_explicit")
            requested = params.get("session_id")
            requested_session_id = (
                requested.strip() if isinstance(requested, str) else ""
            )
            if requested_session_id and str(channel_id).strip().lower() == "tui":
                logger.warning(
                    "[AgentServer] TUI supplied session_id via session.create; "
                    "bypassing prewarm compatibility path: session_id=%s",
                    requested_session_id,
                )
    
            runtime = self._execution_runtime()
            await runtime.start()
            prepared = await runtime.prepare_session_create(
                SessionCreateInput(
                    channel_id=channel_id,
                    requested_session_id=requested_session_id or None,
                    previous_session_id=str(
                        params.get("previous_session_id") or ""
                    ).strip(),
                    create_token=str(params.get("create_token") or "").strip(),
                    persist_session=raw_persist_session,
                    persist_session_supplied=persist_session_supplied,
                    mode=params.get("mode", "agent"),
                    previous_mode=params.get("previous_mode"),
                    is_swarm=bool(params.get("is_swarm")),
                    team_hint=bool(params.get("team")),
                    project_id=params.get("project_id", ""),
                    project_dir=params.get("project_dir", ""),
                    cwd=params.get("cwd", ""),
                    work_mode=params.get("work_mode"),
                    work_mode_explicit=(
                        explicit_work_mode_marker
                        if isinstance(explicit_work_mode_marker, bool)
                        else None
                    ),
                    title=params.get("title", ""),
                    user_id=str(
                        getattr(request, "user_id", "")
                        or params.get("user_id", "")
                        or ""
                    ).strip(),
                    model_name=str(params.get("model_name") or "").strip(),
                    model_selection=params.get("model_selection"),
                    moa_enabled=params.get("moa_enabled"),
                    cron_id=str(params.get("cron_id") or "").strip(),
                )
            )
            params.pop("_work_mode_explicit", None)
            result = prepared.result
    
            # Preserve the established mutation visible to in-process callers.
            params["project_id"] = result.project_id
            params["project_dir"] = result.project_dir
            params["work_mode"] = result.work_mode
            params["mode"] = result.canonical_mode
    
            payload = {
                "sessionId": result.session_id,
                "session_id": result.session_id,
                "projectId": result.project_id,
                "projectDir": result.project_dir,
                "workMode": result.work_mode,
                "persist_session": result.persist_session,
                "moa_enabled": result.moa_enabled,
                "prewarm_hit": result.prewarm_hit,
                "prewarm_status": result.prewarm_status,
            }
            if result.explicit_id_compatibility:
                payload.update(
                    {
                        "created": result.created,
                        "mode": result.canonical_mode,
                    }
                )
    
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
            wire = self.services.encode_response(
                response,
                response_id=request.request_id,
            )
            async with send_lock:
                if not await self.services.send(ws, wire):
                    raise RuntimeError("session provision result was not delivered")
            response_delivered = True
    
            await runtime.commit_session_provision(
                prepared,
                timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
                context=SessionProvisionCommitContext(
                    foreground_scope_id=str(params.get("view_id") or f"ws:{id(ws)}")
                ),
            )
            logger.info(
                "[AgentServer] %s completed: session_id=%s",
                operation,
                result.session_id,
            )
        except SessionProvisionError as error:
            logger.warning("[AgentServer] %s rejected: %s", operation, error)
            await abort_prepared(error)
            if response_delivered:
                return
            payload = {"error": str(error)}
            if error.code is not None:
                payload["code"] = error.code
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload=payload,
            )
            wire = self.services.encode_response(
                response,
                response_id=request.request_id,
            )
            async with send_lock:
                await self.services.send(ws, wire)
        except Exception as error:
            logger.exception("[AgentServer] %s failed: %s", operation, error)
            await abort_prepared(error)
            if response_delivered:
                return
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(error)},
            )
            wire = self.services.encode_response(
                response,
                response_id=request.request_id,
            )
            async with send_lock:
                await self.services.send(ws, wire)
        finally:
            await abort_prepared(sys.exception())

    async def _handle_session_fork(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Translate ``session.fork`` between WebSocket wire and Runtime.
    
        Args:
            ws: WebSocket connection.
            request: AgentRequest with source_session_id, target_session_id, title.
            send_lock: Send lock.
        """
        logger.info("[AgentServer] session.fork: request_id=%s", request.request_id)
    
        runtime = None
        prepared = None
        try:
            params = request.params if isinstance(request.params, dict) else {}
            if "side_conversation" in params:
                raise SessionProvisionError(
                    "side_conversation is no longer supported",
                    code="BAD_REQUEST",
                )
            source = str(params.get("source_session_id") or "").strip()
            target = str(params.get("target_session_id") or "").strip()
            fork_title = str(params.get("title") or "").strip()
            equipment_override = params.get("session_equipment_override")
            if equipment_override is not None and not isinstance(
                equipment_override, dict
            ):
                raise SessionProvisionError(
                    "session_equipment_override must be an object",
                    code="BAD_REQUEST",
                )
            fork_point = params.get("fork_point")
            if not isinstance(fork_point, dict):
                fork_point = {}
            channel_id = request.channel_id or "default"
    
            if not source:
                raise SessionProvisionError(
                    "source_session_id is required",
                    code="BAD_REQUEST",
                )
    
            runtime = self._execution_runtime()
            await runtime.start()
            prepared = await runtime.prepare_session_fork(
                SessionForkInput(
                    channel_id=channel_id,
                    source_session_id=source,
                    target_session_id=target or None,
                    title=fork_title,
                    cutoff_message_id=str(fork_point.get("message_id") or "").strip(),
                    cutoff_role=str(fork_point.get("role") or "").strip(),
                    cutoff_content=str(fork_point.get("content") or ""),
                    cutoff_timestamp=fork_point.get("timestamp"),
                    session_equipment_override=equipment_override,
                )
            )
            result = await runtime.commit_session_provision(
                prepared,
                timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
            )
    
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": result.session_id,
                    "source_session_id": result.source_session_id,
                    "title": result.title,
                    "moa_enabled": result.moa_enabled,
                },
            )
            wire = self.services.encode_response(resp, response_id=request.request_id)
            async with send_lock:
                await self.services.send(ws, wire)
    
            logger.info(
                "[AgentServer] session.fork completed: source=%s target=%s title=%s",
                source,
                result.session_id,
                result.title,
            )
    
        except SessionProvisionError as e:
            logger.warning("[AgentServer] session.fork rejected: %s", e)
            payload = {"error": str(e)}
            if e.code is not None:
                payload["code"] = e.code
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload=payload,
            )
            wire = self.services.encode_response(resp, response_id=request.request_id)
            async with send_lock:
                await self.services.send(ws, wire)
        except ValueError as e:
            logger.warning("[AgentServer] session.fork ValueError: %s", e)
            code = (
                "NOT_FOUND"
                if "not found" in str(e)
                else "ALREADY_EXISTS"
                if "already exists" in str(e)
                else "BAD_REQUEST"
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e), "code": code},
            )
            wire = self.services.encode_response(resp, response_id=request.request_id)
            async with send_lock:
                await self.services.send(ws, wire)
        except Exception as e:
            logger.exception("[AgentServer] session.fork failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = self.services.encode_response(resp, response_id=request.request_id)
            async with send_lock:
                await self.services.send(ws, wire)
        finally:
            if (
                runtime is not None
                and prepared is not None
                and prepared.state is SessionProvisionState.PREPARED
            ):
                primary_error = sys.exception()
                try:
                    await runtime.abort_session_provision(prepared)
                except asyncio.CancelledError:
                    if primary_error is None:
                        raise
                    logger.warning(
                        "[AgentServer] session.fork abort was cancelled while "
                        "preserving %s",
                        type(primary_error).__name__,
                    )
                except Exception as abort_exc:  # noqa: BLE001
                    logger.warning(
                        "[AgentServer] session.fork abort failed: %s",
                        abort_exc,
                    )

    async def _handle_acp_tool_response(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        jsonrpc_id = params.get("jsonrpc_id")
        response_payload = params.get("response")
        if not isinstance(response_payload, dict):
            response_payload = {}

        if get_acp_output_manager().complete_jsonrpc_response(
            jsonrpc_id, response_payload
        ):
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"accepted": True},
            )
        else:
            logger.info(
                "[AgentServer] ignore unknown/late acp tool response: jsonrpc_id=%s request_id=%s",
                jsonrpc_id,
                request.request_id,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "accepted": False,
                    "ignored": True,
                    "reason": "unknown_or_late_response",
                    "jsonrpc_id": jsonrpc_id,
                },
            )

        wire = self.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await self.services.send(ws, wire)

    async def handle_acp_tool_response_for_test(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Public test helper that delegates to ACP tool-response handling."""
        await self._handle_acp_tool_response(ws, request, send_lock)

    @staticmethod
    def _annotate_model_error(payload: dict, session_id: Optional[str]) -> None:
        if payload.get("code"):
            return
        try:
            from jiuwenswarm.common.auth.apig import classify_model_error
            from jiuwenswarm.common.auth.login_credentials import (
                session_uses_login_model,
            )

            code = classify_model_error(
                str(payload.get("error") or ""),
                login_model=session_uses_login_model(session_id),
            )
        except Exception:  # noqa: BLE001 — 分类失败不该影响错误本身的上报
            return
        if code:
            payload["code"] = code
            payload["upstream"] = True

    async def _handle_lifecycle_request(self, ws, request, send_lock) -> bool:
        from jiuwenswarm.server.runtime.session import lifecycle as lc
        from jiuwenswarm.server.runtime.session.session_archive import (
            SessionArchiveService,
        )
        method = request.req_method.value if request.req_method else ""
        methods = {
            "session.archive",
            "session.unarchive",
            "session.archived.list",
            "session.delete",
            "cron.sessions.delete",
            "project.sessions.archive",
            "project.sessions.delete_archived",
            "project.lifecycle",
        }
        if method not in methods:
            return False
        service = getattr(self, "_archive_service", None)
        if service is None:
            service = self._archive_service = SessionArchiveService(
                self._execution_runtime()
            )
        params = request.params if isinstance(request.params, dict) else {}
        ok = True
        try:
            if method == "session.archived.list":
                payload = await asyncio.to_thread(service.list_sessions, params)
            elif method == "cron.sessions.delete":
                payload = await service.delete_cron_sessions(
                    params.get("cron_id"), request.channel_id or ""
                )
            elif method.startswith("project.sessions."):
                payload = await service.project_batch(
                    params.get("project_id"),
                    method.rsplit(".", 1)[1],
                    request.channel_id or "",
                )
            elif method == "project.lifecycle" and params.get("events"):
                # Full-directory scan of lifecycle state: keep it off the loop.
                payload = {"events": await asyncio.to_thread(lc.event_snapshots)}
            elif method == "project.lifecycle" and params.get("inventory"):
                from jiuwenswarm.server.runtime.session.project_store import (
                    list_projects,
                )
                payload = {"projects": [dict(project_id=p.project_id,
                            operation=lc.state("project", p.project_id).get("operation"))
                            for p in list_projects(include_hidden=True, cache_bust=True)]}
                known = {item["project_id"] for item in payload["projects"]}
                directory = lc.get_agent_root_dir() / "lifecycle" / "resources"
                for path in directory.glob("project_*.json"):
                    operation = lc.read_json(path).get("operation") or {}
                    project_id = operation.get("resource_id")
                    if (
                        project_id
                        and project_id not in known
                        and operation.get("status") != "completed"
                    ):
                        payload["projects"].append(
                            dict(project_id=project_id, operation=operation)
                        )
            elif method == "project.lifecycle":
                project_id = lc.validate_id(params.get("project_id"))

                def _project_snapshot():
                    # cron 准入闸门会并发打这个分支：读盘必须离开事件循环
                    # （issue #4885，同 events 分支的 to_thread 先例）。
                    # state 只读一次，经 projection(value=) 复用，同一 JSON 不读两遍。
                    from jiuwenswarm.server.runtime.session.project_store import (
                        get_project_by_id,
                    )
                    return lc.state("project", project_id), get_project_by_id(
                        project_id, cache_bust=True
                    )

                value, project = await asyncio.to_thread(_project_snapshot)
                payload = lc.projection("project", project_id, value=value)
                payload["exists"] = project is not None
                # 调度闸门(project_execution_allowed)据此拒隐藏项目:被移除
                # 项目的定时任务不到点触发、不进任务列表。
                payload["hidden"] = bool(project is not None and project.hidden)
                payload["operation"] = value.get("operation")
                if params.get("running_sessions"):
                    # Compatibility shim for Gateways that still run the old
                    # project.remove precheck. New Gateways never send this
                    # parameter, so it is not part of the new removal path.
                    if project is None or project.hidden:
                        payload["has_running_sessions"] = False
                    else:
                        from jiuwenswarm.server.runtime.gateway_adapter.project_adapter import (
                            _project_conversation_state,
                        )

                        def _has_running_sessions() -> bool:
                            return bool(
                                _project_conversation_state(
                                    project_id,
                                    self._execution_runtime(),
                                    ignore_heartbeats=True,
                                )[1]
                            )

                        payload["has_running_sessions"] = await asyncio.to_thread(
                            _has_running_sessions
                        )
            elif method.startswith("session."):
                ids = lc.parse_ids(params, delete=method == "session.delete")
                results = []
                for sid in ids:
                    try:
                        results.append(
                            await service.session(
                                sid,
                                method.split(".")[1],
                                request.channel_id or "",
                            )
                        )
                    except lc.LifecycleError as exc:
                        results.append(
                            dict(
                                session_id=sid,
                                ok=False,
                                code=exc.code,
                                error=str(exc),
                                **exc.details,
                            )
                        )
                if method == "session.delete" and "session_ids" not in params:
                    ok = results[0]["ok"]
                    # project_id 超出 §5.10.5 单条字段表，但 Gateway 需要
                    # 它发出符合 §5.10.11 契约的 session.deleted 事件。
                    payload = (
                        {
                            "session_id": ids[0],
                            "project_id": results[0].get("project_id", ""),
                            # Deletion is accepted before its background
                            # cleanup; keep the same completion contract as the
                            # batch form so callers can tell the two apart.
                            **{
                                key: results[0][key]
                                for key in ("accepted", "finalizing", "stop_pending")
                                if key in results[0]
                            },
                        }
                        if ok
                        else {
                            key: value for key, value in results[0].items()
                            if key not in {"session_id", "ok"}
                        }
                    )
                else:
                    succeeded = sum(item["ok"] for item in results)
                    payload = dict(
                        succeeded_count=succeeded,
                        failed_count=len(results) - succeeded,
                        results=results,
                    )
            # methods 集合已穷尽上面的分支;不再有项目级删除级联,
            # 任何新增方法都必须在这里拿到显式分支。
        except lc.LifecycleError as exc:
            ok, payload = False, dict(code=exc.code, error=str(exc), **exc.details)
        except Exception as exc:
            logger.exception("lifecycle operation failed")
            ok, payload = False, dict(code="INTERNAL_ERROR", error=str(exc))
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=ok,
            payload=payload,
            metadata=request.metadata,
        )
        async with send_lock:
            await self.services.send(
                ws, self.services.encode_response(resp, response_id=request.request_id)
            )
        return True

    async def _handle_session_input_intent(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Publish a user input intent without naming optional consumers."""
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        intent_id = str(params.get("intent_id") or request.request_id or "").strip()
        if not session_id:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "session_id is required", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        else:
            try:
                outcome = await self._execution_runtime().record_session_input_intent(
                    request,
                    view_id=str(params.get("view_id") or "default-view"),
                )
                logger.info(
                    "[AgentWebSocketServer] session.input.intent processed: "
                    "session_id=%s intent_id=%s outcome=%s",
                    session_id,
                    intent_id,
                    outcome,
                )
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "session_id": session_id,
                        "scheduled": outcome == "scheduled",
                        "outcome": outcome,
                    },
                    metadata=request.metadata,
                )
            except Exception as exc:
                logger.warning(
                    "[AgentWebSocketServer] session.input.intent failed closed: "
                    "session_id=%s error=%s",
                    session_id,
                    exc,
                )
                # KVC is an optional optimization; typing must not fail.
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "session_id": session_id,
                        "scheduled": False,
                        "outcome": "failed",
                    },
                    metadata=request.metadata,
                )

        wire = self.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await self.services.send(ws, wire)

    async def _handle_session_message_continue_queued(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        service = getattr(self, "_session_message_service", None)
        try:
            if not session_id:
                raise SessionMessagingError(
                    "INVALID_ARGUMENT", "session_id is required"
                )
            if service is None:
                raise SessionMessagingError(
                    "HOST_CAPABILITY_UNAVAILABLE", "Session messaging is unavailable"
                )
            payload = await service.continue_queued_for_target(
                session_id, request.user_id
            )
            ok = True
        except SessionMessagingError as exc:
            payload = {"code": exc.code, "error": str(exc)}
            ok = False
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=ok,
            payload=payload,
            metadata=request.metadata,
        )
        async with send_lock:
            await self.services.send(
                ws,
                self.services.encode_response(response, response_id=request.request_id),
            )

    async def _handle_session_switch(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Translate ``session.switch`` between WebSocket wire and Runtime."""
        params = request.params if isinstance(request.params, dict) else {}
        target = str(params.get("session_id") or request.session_id or "").strip()
        previous_session_id = str(params.get("previous_session_id") or "").strip()
    
        if not target:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "session_id is required", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
            wire = self.services.encode_response(resp, response_id=request.request_id)
            async with send_lock:
                await self.services.send(ws, wire)
            return
    
        channel_id = str(request.channel_id or "").strip() or "default"
        lock_key = f"{id(ws)}:{channel_id}"
        switch_lock = _session_switch_locks.get(lock_key)
        if switch_lock is None:
            switch_lock = asyncio.Lock()
            _session_switch_locks[lock_key] = switch_lock
    
        async with switch_lock:
            runtime = self._execution_runtime()
            prepared = None
            commit_context = SessionProvisionCommitContext(
                foreground_scope_id=str(params.get("view_id") or f"ws:{id(ws)}")
            )
            try:
                await runtime.start()
                prepared = await runtime.prepare_session_switch(
                    SessionSwitchInput(
                        channel_id=channel_id,
                        target_session_id=target,
                        previous_session_id=previous_session_id,
                        mode=params.get("mode", "agent.plan"),
                        previous_mode=params.get("previous_mode"),
                        team_hint=bool(params.get("team")),
                    )
                )
                result = await runtime.commit_session_provision(
                    prepared,
                    timing=(SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY),
                    context=commit_context,
                )
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={
                        "session_id": result.session_id,
                        "mode": result.mode,
                        "switched": result.switched,
                        "moa_enabled": result.moa_enabled,
                    },
                    metadata=request.metadata,
                )
    
                wire = self.services.encode_response(
                    resp,
                    response_id=request.request_id,
                )
                async with send_lock:
                    await self.services.send(ws, wire)
            finally:
                primary_error = sys.exception()
                if (
                    prepared is not None
                    and prepared.state is SessionProvisionState.PREPARED
                ):
                    try:
                        await runtime.abort_session_provision(prepared)
                    except asyncio.CancelledError:
                        if primary_error is None:
                            raise
                        logger.warning(
                            "[AgentServer] session.switch abort was cancelled "
                            "while preserving %s",
                            type(primary_error).__name__,
                        )
                    except Exception as abort_exc:  # noqa: BLE001
                        logger.warning(
                            "[AgentServer] session.switch abort failed: %s",
                            abort_exc,
                        )

    @classmethod
    def _skips_session_lifecycle_guard(cls, request: AgentRequest) -> bool:
        """Return True when the RPC must not be blocked by a deleted session.

        Narrower than ``_is_stateless_method_request``: that predicate only
        means "skip adapter rebuild", and includes write methods under
        ``skills.*`` / ``plugins.*``. This one is the session-lifecycle
        bypass for plaza/catalog reads only.
        """
        if request.req_method is None:
            return False
        method = request.req_method.value
        if method in cls._SESSION_LIFECYCLE_GUARD_SKIP_METHODS:
            return True
        return method.startswith(cls._SESSION_LIFECYCLE_GUARD_SKIP_PREFIXES)

    _SESSION_LIFECYCLE_GUARD_SKIP_METHODS = frozenset({
        "skills.list",
        "plugins.list",
    })

    _SESSION_LIFECYCLE_GUARD_SKIP_PREFIXES = (
        "skills.swarmskillshub.",
        "skills.online_search.",
    )



