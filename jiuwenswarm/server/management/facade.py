# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stable management API delegating explicitly to independently loaded domains."""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING
from pathlib import Path
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.wire_truncate import _HISTORY_PAGE_SIZE

if TYPE_CHECKING:
    from jiuwenswarm.server.management.mcp import McpUpsertTypes
import asyncio

from jiuwenswarm.server.management.registry import ManagementServices


class ManagementFacade:
    """Management delegation shares its host's Runtime, tasks and persistence."""

    @property
    def management(self) -> ManagementServices:
        value = getattr(self, "_management_services", None)
        if value is None:
            value = ManagementServices(self)
            self._management_services = value
        return value

    async def _handle_session_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("sessions")._handle_session_list(
            ws, request, send_lock
        )

    async def _handle_session_rename(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("sessions")._handle_session_rename(
            ws, request, send_lock
        )

    async def _handle_session_rebind_project(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider(
            "sessions"
        )._handle_session_rebind_project(ws, request, send_lock)

    async def _find_team_session_ids(self, team_name: str) -> list[str]:
        return await self.management.provider("teams")._find_team_session_ids(team_name)

    async def _ensure_persistent_checkpointer_response(
        self, request: AgentRequest
    ) -> AgentResponse | None:
        return await self.management.provider(
            "rsi"
        )._ensure_persistent_checkpointer_response(request)

    @staticmethod
    def _team_binding_payload(binding: Any) -> dict[str, Any]:
        from jiuwenswarm.server.management.teams import TeamsManagement

        return TeamsManagement._team_binding_payload(binding)

    @staticmethod
    async def _create_team_binding_from_template(
        *, team_name: str, template_id: str, config_base: dict[str, Any]
    ) -> Any:
        from jiuwenswarm.server.management.teams import TeamsManagement

        return await TeamsManagement._create_team_binding_from_template(
            team_name=team_name, template_id=template_id, config_base=config_base
        )

    @classmethod
    async def _create_generated_team_binding(
        cls, *, description: str, config_base: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        from jiuwenswarm.server.management.teams import TeamsManagement

        return await TeamsManagement._create_generated_team_binding(
            description=description,
            config_base=config_base,
            create_binding=cls._create_team_binding_from_template,
        )

    @staticmethod
    def _active_team_session_map() -> dict[str, str]:
        from jiuwenswarm.server.management.teams import TeamsManagement

        return TeamsManagement._active_team_session_map()

    @staticmethod
    def _legacy_team_bindings_from_sessions(
        known_team_names: set[str],
    ) -> list[dict[str, Any]]:
        from jiuwenswarm.server.management.teams import TeamsManagement

        return TeamsManagement._legacy_team_bindings_from_sessions(known_team_names)

    async def _handle_team_templates_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("teams")._handle_team_templates_list(
            ws, request, send_lock
        )

    async def _handle_team_bindings_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("teams")._handle_team_bindings_list(
            ws, request, send_lock
        )

    async def _handle_team_binding_create(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("teams")._handle_team_binding_create(
            ws, request, send_lock
        )

    async def _handle_team_binding_generate(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("teams")._handle_team_binding_generate(
            ws, request, send_lock
        )

    async def _handle_team_session_bind(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("teams")._handle_team_session_bind(
            ws, request, send_lock
        )

    async def _handle_team_delete(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("teams")._handle_team_delete(
            ws, request, send_lock
        )

    async def _resolve_rewind_agent(
        self, channel_id: str, session_id: str | None = None
    ) -> tuple[Any, Any] | None:
        return await self.management.provider("sessions")._resolve_rewind_agent(
            channel_id, session_id
        )

    async def _handle_session_rewind_full(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
        restore_files: bool = False,
        compact: bool = False,
    ) -> None:
        return await self.management.provider("sessions")._handle_session_rewind_full(
            ws, request, send_lock, restore_files, compact
        )

    async def _handle_session_rewind_context(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider(
            "sessions"
        )._handle_session_rewind_context(ws, request, send_lock)

    async def _handle_permissions_config(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider(
            "authorization"
        )._handle_permissions_config(ws, request, send_lock)

    async def _handle_a4p_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("authorization")._handle_a4p_request(
            ws, request, send_lock
        )

    async def _handle_history_get(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("history")._handle_history_get(
            ws, request, send_lock
        )

    async def _handle_history_append_record(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("history")._handle_history_append_record(
            ws, request, send_lock
        )

    async def _handle_proactive_tick(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("proactive")._handle_proactive_tick(
            ws, request, send_lock
        )

    async def _handle_proactive_feedback(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("proactive")._handle_proactive_feedback(
            ws, request, send_lock
        )

    async def _cancel_targeted_expert(self, request: AgentRequest) -> AgentResponse:
        return await self.management.provider("teams")._cancel_targeted_expert(request)

    async def _targeted_expert_runtime_stream(self, request: AgentRequest) -> Any:
        stream = self.management.provider("teams")._targeted_expert_runtime_stream(
            request
        )
        try:
            async for item in stream:
                yield item
        finally:
            await stream.aclose()

    async def _targeted_expert_response_stream(self, request: AgentRequest) -> Any:
        stream = self.management.provider("teams")._targeted_expert_response_stream(
            request
        )
        try:
            async for item in stream:
                yield item
        finally:
            await stream.aclose()

    async def _handle_team_snapshot(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("teams")._handle_team_snapshot(
            ws, request, send_lock
        )

    async def _team_list_entries(
        self, session_id: str, channel_id: str
    ) -> list[dict[str, Any]]:
        return await self.management.provider("teams")._team_list_entries(
            session_id, channel_id
        )

    @staticmethod
    def _lookup_session_org_binding(session_id: str, team_id: str) -> str | None:
        from jiuwenswarm.server.management.teams import TeamsManagement

        return TeamsManagement._lookup_session_org_binding(session_id, team_id)

    async def _handle_team_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("teams")._handle_team_list(
            ws, request, send_lock
        )

    async def _handle_org_snapshot(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("teams")._handle_org_snapshot(
            ws, request, send_lock
        )

    async def _build_org_snapshot(
        self, *, session_id: str, team_id: str, organization_id: str, backend: Any
    ) -> dict[str, Any]:
        return await self.management.provider("teams")._build_org_snapshot(
            session_id=session_id,
            team_id=team_id,
            organization_id=organization_id,
            backend=backend,
        )

    @staticmethod
    def _resolve_team_backend(session_id: str, team_id: str) -> Any | None:
        from jiuwenswarm.server.management.teams import TeamsManagement

        return TeamsManagement._resolve_team_backend(session_id, team_id)

    async def _handle_team_mq_publish(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("teams")._handle_team_mq_publish(
            ws, request, send_lock
        )

    async def _handle_team_members_get(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("teams")._handle_team_members_get(
            ws, request, send_lock
        )

    async def _handle_command_workflows(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("workflows")._handle_command_workflows(
            ws, request, send_lock
        )

    async def _handle_swarmflow_pause(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider(
            "workflow_control"
        )._handle_swarmflow_pause(ws, request, send_lock)

    async def _handle_swarmflow_resume(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider(
            "workflow_control"
        )._handle_swarmflow_resume(ws, request, send_lock)

    async def _handle_swarmflow_stop(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider(
            "workflow_control"
        )._handle_swarmflow_stop(ws, request, send_lock)

    async def _run_swarmflow_control(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock, *, action: str
    ) -> None:
        return await self.management.provider(
            "workflow_control"
        )._run_swarmflow_control(ws, request, send_lock, action=action)

    async def _handle_team_history_get(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("history")._handle_team_history_get(
            ws, request, send_lock
        )

    async def _handle_history_get_stream(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("history")._handle_history_get_stream(
            ws, request, send_lock
        )

    async def _handle_command_add_dir(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("commands")._handle_command_add_dir(
            ws, request, send_lock
        )

    async def _handle_command_chrome(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("commands")._handle_command_chrome(
            ws, request, send_lock
        )

    async def _handle_command_compact(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("commands")._handle_command_compact(
            ws, request, send_lock
        )

    async def _handle_command_compact_partial(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider(
            "commands"
        )._handle_command_compact_partial(ws, request, send_lock)

    async def _handle_command_context(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("commands")._handle_command_context(
            ws, request, send_lock
        )

    async def _handle_command_recap(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("commands")._handle_command_recap(
            ws, request, send_lock
        )

    async def _handle_command_btw(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("commands")._handle_command_btw(
            ws, request, send_lock
        )

    async def _handle_command_diff(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("commands")._handle_command_diff(
            ws, request, send_lock
        )

    async def _handle_command_simplify(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("commands")._handle_command_simplify(
            ws, request, send_lock
        )

    async def _handle_command_model(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("commands")._handle_command_model(
            ws, request, send_lock
        )

    @staticmethod
    def _mask_sensitive_fields(payload: Any) -> Any:
        from jiuwenswarm.server.management.mcp import McpManagement

        return McpManagement._mask_sensitive_fields(payload)

    @staticmethod
    async def _pre_check_mcp_server(server_payload: dict[str, Any]) -> tuple[bool, str]:
        from jiuwenswarm.server.management.mcp import McpManagement

        return await McpManagement._pre_check_mcp_server(server_payload)

    @staticmethod
    async def _pre_check_mcp_http_auth(
        server_payload: dict[str, Any],
    ) -> tuple[bool, str]:
        from jiuwenswarm.server.management.mcp import McpManagement

        return await McpManagement._pre_check_mcp_http_auth(server_payload)

    @staticmethod
    async def _fetch_mcp_tools_from_config(
        entry: dict[str, Any],
    ) -> list[dict[str, Any]]:
        from jiuwenswarm.server.management.mcp import McpManagement

        return await McpManagement._fetch_mcp_tools_from_config(entry)

    @staticmethod
    def _normalize_mcp_payload(
        params: dict[str, Any], current: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        from jiuwenswarm.server.management.mcp import McpManagement

        return McpManagement._normalize_mcp_payload(params, current)

    def _normalize_mcp_add_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.management.provider("mcp")._normalize_mcp_add_payload(params)

    def _normalize_mcp_update_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.management.provider("mcp")._normalize_mcp_update_payload(params)

    async def _persist_and_probe_mcp(
        self,
        request: AgentRequest,
        payload: dict[str, Any],
        old_item: dict[str, Any] | None,
        types: McpUpsertTypes,
        item: dict[str, Any] | None = None,
    ) -> AgentResponse:
        return await self.management.provider("mcp")._persist_and_probe_mcp(
            request, payload, old_item, types, item
        )

    async def _handle_command_mcp(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_command_mcp(
            ws, request, send_lock
        )

    async def _get_asset_publish_api(self):
        return await self.management.provider("assets")._get_asset_publish_api()

    async def _start_asset_services(self):
        return await self.management.provider("assets")._start_asset_services()

    async def _handle_asset_publish(self, ws, request, send_lock):
        return await self.management.provider("assets")._handle_asset_publish(
            ws, request, send_lock
        )

    async def _handle_mcp_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_mcp_list(
            ws, request, send_lock
        )

    async def _handle_mcp_show(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_mcp_show(
            ws, request, send_lock
        )

    async def _handle_mcp_install(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_mcp_install(
            ws, request, send_lock
        )

    async def _handle_mcp_uninstall(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_mcp_uninstall(
            ws, request, send_lock
        )

    async def _run_mcp_connect_flow(
        self, name: str, *, rollback_on_probe_failure: bool = True
    ) -> dict[str, Any]:
        return await self.management.provider("mcp")._run_mcp_connect_flow(
            name, rollback_on_probe_failure=rollback_on_probe_failure
        )

    async def _handle_mcp_connect(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_mcp_connect(
            ws, request, send_lock
        )

    async def _handle_mcp_cancel_connect(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_mcp_cancel_connect(
            ws, request, send_lock
        )

    async def _handle_mcp_wait_auth(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_mcp_wait_auth(
            ws, request, send_lock
        )

    async def _handle_mcp_disconnect(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_mcp_disconnect(
            ws, request, send_lock
        )

    async def _finalize_cli_auth(
        self, name: str, item: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.management.provider("mcp")._finalize_cli_auth(name, item)

    async def _await_cli_auth(
        self, name: str, step_index: int, *, max_attempts: int = 200, delay: float = 3.0
    ) -> dict[str, Any]:
        return await self.management.provider("mcp")._await_cli_auth(
            name, step_index, max_attempts=max_attempts, delay=delay
        )

    async def _handle_mcp_register_custom(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_mcp_register_custom(
            ws, request, send_lock
        )

    async def _handle_mcp_delete_custom(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_mcp_delete_custom(
            ws, request, send_lock
        )

    async def _handle_mcp_save_credentials(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_mcp_save_credentials(
            ws, request, send_lock
        )

    async def _handle_command_sandbox(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("sandbox")._handle_command_sandbox(
            ws, request, send_lock
        )

    async def _handle_sandbox_enable(self, channel_id: str) -> dict[str, Any]:
        return await self.management.provider("sandbox")._handle_sandbox_enable(
            channel_id
        )

    async def _handle_sandbox_disable(self, channel_id: str) -> dict[str, Any]:
        return await self.management.provider("sandbox")._handle_sandbox_disable(
            channel_id
        )

    async def _handle_sandbox_exclude_add(
        self, channel_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.management.provider("sandbox")._handle_sandbox_exclude_add(
            channel_id, params
        )

    async def _handle_sandbox_exclude_remove(
        self, channel_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.management.provider("sandbox")._handle_sandbox_exclude_remove(
            channel_id, params
        )

    def _dry_run_files_policy(
        self, channel_id: str, params: dict[str, Any], files: dict[str, Any]
    ) -> None:
        return self.management.provider("sandbox")._dry_run_files_policy(
            channel_id, params, files
        )

    async def _handle_sandbox_files_set(
        self, channel_id: str, params: dict[str, Any], *, bucket: str
    ) -> dict[str, Any]:
        return await self.management.provider("sandbox")._handle_sandbox_files_set(
            channel_id, params, bucket=bucket
        )

    async def _handle_sandbox_files_remove(
        self, channel_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.management.provider("sandbox")._handle_sandbox_files_remove(
            channel_id, params
        )

    def _resolve_active_project_dir(
        self, channel_id: str, params: dict[str, Any] | None = None
    ) -> str | None:
        return self.management.provider("sandbox")._resolve_active_project_dir(
            channel_id, params
        )

    def _resolve_active_is_code_agent(self, channel_id: str) -> bool:
        return self.management.provider("sandbox")._resolve_active_is_code_agent(
            channel_id
        )

    @staticmethod
    def _effective_files_from_adapter(
        adapter: Any,
    ) -> dict[str, list[dict[str, str]]] | None:
        from jiuwenswarm.server.management.sandbox import SandboxManagement

        return SandboxManagement._effective_files_from_adapter(adapter)

    def _attach_effective_sandbox_files(
        self,
        payload: dict[str, Any],
        channel_id: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        return self.management.provider("sandbox")._attach_effective_sandbox_files(
            payload, channel_id, params
        )

    @staticmethod
    def _read_landlock_compatibility(policy_path: Path | None) -> str:
        from jiuwenswarm.server.management.sandbox import SandboxManagement

        return SandboxManagement._read_landlock_compatibility(policy_path)

    async def _attach_landlock_status(self, payload: dict[str, Any]) -> None:
        return await self.management.provider("sandbox")._attach_landlock_status(
            payload
        )

    async def _apply_sandbox_runtime_patch(
        self, channel_id: str, runtime: dict[str, Any], *, files_changed: bool
    ) -> None:
        return await self.management.provider("sandbox")._apply_sandbox_runtime_patch(
            channel_id, runtime, files_changed=files_changed
        )

    @staticmethod
    def _resolve_adapter(agent: Any) -> Any:
        from jiuwenswarm.server.management.sandbox import SandboxManagement

        return SandboxManagement._resolve_adapter(agent)

    @staticmethod
    def resolve_adapter(agent: Any) -> Any:
        from jiuwenswarm.server.management.sandbox import SandboxManagement

        return SandboxManagement.resolve_adapter(agent)

    @staticmethod
    def _parse_sandbox_host_port(url: str) -> tuple[str, int]:
        from jiuwenswarm.server.management.sandbox import SandboxManagement

        return SandboxManagement._parse_sandbox_host_port(url)

    @staticmethod
    def _is_tcp_port_bindable(host: str, port: int) -> bool:
        from jiuwenswarm.server.management.sandbox import SandboxManagement

        return SandboxManagement._is_tcp_port_bindable(host, port)

    @staticmethod
    def _pick_free_tcp_port(host: str) -> int:
        from jiuwenswarm.server.management.sandbox import SandboxManagement

        return SandboxManagement._pick_free_tcp_port(host)

    def _allocate_internal_jiuwenbox_port(self, host: str, preferred_port: int) -> int:
        return self.management.provider("sandbox")._allocate_internal_jiuwenbox_port(
            host, preferred_port
        )

    async def _handle_command_resume(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("commands")._handle_command_resume(
            ws, request, send_lock
        )

    async def _handle_command_session(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("commands")._handle_command_session(
            ws, request, send_lock
        )

    async def _handle_command_status(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("commands")._handle_command_status(
            ws, request, send_lock
        )

    async def _handle_browser_runtime_restart(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("agents")._handle_browser_runtime_restart(
            ws, request, send_lock
        )

    async def _handle_agents_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("agents")._handle_agents_list(
            ws, request, send_lock
        )

    async def _handle_agents_get(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("agents")._handle_agents_get(
            ws, request, send_lock
        )

    async def _generate_agent_with_llm(
        self, name: str, description: str
    ) -> tuple[str, str] | None:
        return await self.management.provider("agents")._generate_agent_with_llm(
            name, description
        )

    async def _handle_agents_create(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("agents")._handle_agents_create(
            ws, request, send_lock
        )

    async def _handle_agents_update(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("agents")._handle_agents_update(
            ws, request, send_lock
        )

    async def _handle_agents_delete(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("agents")._handle_agents_delete(
            ws, request, send_lock
        )

    async def _handle_agents_set_enabled(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock, enabled: bool
    ) -> None:
        return await self.management.provider("agents")._handle_agents_set_enabled(
            ws, request, send_lock, enabled
        )

    async def _handle_agents_tools_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("agents")._handle_agents_tools_list(
            ws, request, send_lock
        )

    async def _handle_config_cache_clear(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("agents")._handle_config_cache_clear(
            ws, request, send_lock
        )

    async def _handle_agent_prewarm_sync(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("agents")._handle_agent_prewarm_sync(
            ws, request, send_lock
        )

    async def _handle_auth_credentials_update(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider(
            "authorization"
        )._handle_auth_credentials_update(ws, request, send_lock)

    async def _handle_agent_reload_config(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("agents")._handle_agent_reload_config(
            ws, request, send_lock
        )

    async def _handle_extensions_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("extensions")._handle_extensions_list(
            ws, request, send_lock
        )

    async def _handle_extensions_import(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("extensions")._handle_extensions_import(
            ws, request, send_lock
        )

    async def _handle_extensions_delete(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("extensions")._handle_extensions_delete(
            ws, request, send_lock
        )

    async def _handle_extensions_toggle(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("extensions")._handle_extensions_toggle(
            ws, request, send_lock
        )

    async def _handle_hooks_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("authorization")._handle_hooks_list(
            ws, request, send_lock
        )

    @staticmethod
    def get_conversation_history(
        session_id: str, page_idx: int, *, subagent_id: str | None = None
    ) -> dict[str, Any] | None:
        from jiuwenswarm.server.management.history import HistoryManagement

        return HistoryManagement.get_conversation_history(
            session_id, page_idx, subagent_id=subagent_id
        )

    @staticmethod
    def get_conversation_history_cursor(
        session_id: str,
        cursor: str | None,
        *,
        limit: int = _HISTORY_PAGE_SIZE,
        subagent_id: str | None = None,
    ) -> dict[str, Any]:
        from jiuwenswarm.server.management.history import HistoryManagement

        return HistoryManagement.get_conversation_history_cursor(
            session_id, cursor, limit=limit, subagent_id=subagent_id
        )

    async def _handle_rsi_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("rsi")._handle_rsi_request(
            ws, request, send_lock
        )

    def _get_rsi_handlers(self):
        return self.management.provider("rsi")._get_rsi_handlers()

    @staticmethod
    def _rsi_harness_refs_provider(params: dict[str, Any] | None = None) -> str | None:
        from jiuwenswarm.server.management.rsi import RsiManagement

        return RsiManagement._rsi_harness_refs_provider(params)

    async def _handle_harness_packages_get(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("packages")._handle_harness_packages_get(
            ws, request, send_lock
        )

    async def _handle_harness_packages_scan(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("packages")._handle_harness_packages_scan(
            ws, request, send_lock
        )

    async def _handle_harness_packages_activate(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider(
            "packages"
        )._handle_harness_packages_activate(ws, request, send_lock)

    async def _handle_harness_packages_deactivate(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider(
            "packages"
        )._handle_harness_packages_deactivate(ws, request, send_lock)

    async def _handle_harness_packages_delete(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider(
            "packages"
        )._handle_harness_packages_delete(ws, request, send_lock)

    def _resolve_model(self, model_name: Optional[str] = None) -> Optional[Any]:
        return self.management.provider("schedule")._resolve_model(model_name)

    def _build_model_cache(self) -> None:
        return self.management.provider("schedule")._build_model_cache()

    async def _handle_schedule_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock, action: str
    ) -> None:
        return await self.management.provider("schedule")._handle_schedule_request(
            ws, request, send_lock, action
        )

    async def _await_host_oauth_auth(
        self, name: str, *, max_attempts: int = 200, delay: float = 1.0
    ) -> dict[str, Any]:
        return await self.management.provider("mcp")._await_host_oauth_auth(name, max_attempts=max_attempts, delay=delay)

    @staticmethod
    def _command_mcp_internal_state(request: AgentRequest) -> AgentResponse:
        from jiuwenswarm.server.management.mcp import McpManagement

        return McpManagement._command_mcp_internal_state(request)

    async def _command_mcp_oauth_callback(self, ws: Any, request: AgentRequest) -> AgentResponse:
        return await self.management.provider("mcp")._command_mcp_oauth_callback(ws, request)

    async def _handle_mcp_oauth_start(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("mcp")._handle_mcp_oauth_start(ws, request, send_lock)

    async def _cancel_organization(self, request: AgentRequest) -> AgentResponse:
        return await self.management.provider("teams")._cancel_organization(request)

    def _expert_request_snapshot(self, session_id: str) -> list[dict[str, str]]:
        return self.management.provider("teams")._expert_request_snapshot(session_id)

    @staticmethod
    def _is_targeted_expert_cancel_request(request: AgentRequest) -> bool:
        from jiuwenswarm.server.management.teams import TeamsManagement

        return TeamsManagement._is_targeted_expert_cancel_request(request)

    @staticmethod
    def _is_targeted_expert_request(request: AgentRequest) -> bool:
        from jiuwenswarm.server.management.teams import TeamsManagement

        return TeamsManagement._is_targeted_expert_request(request)

    async def _load_persisted_organization(
        self, session_id: str, team_id: str = ""
    ) -> tuple[Any, Any] | None:
        return await self.management.provider("teams")._load_persisted_organization(session_id, team_id)

    @staticmethod
    def _organization_request_snapshot(session_id: str) -> list[dict[str, str]]:
        from jiuwenswarm.server.management.teams import TeamsManagement

        return TeamsManagement._organization_request_snapshot(session_id)

    async def _stop_expert_streams(self) -> None:
        return await self.management.provider("teams")._stop_expert_streams()

    @staticmethod
    async def _stop_organization_team(session_id: str, team_id: str) -> None:
        from jiuwenswarm.server.management.teams import TeamsManagement

        return await TeamsManagement._stop_organization_team(session_id, team_id)

    @staticmethod
    def _team_target_and_owner(request: AgentRequest) -> tuple[str, str] | None:
        from jiuwenswarm.server.management.teams import TeamsManagement

        return TeamsManagement._team_target_and_owner(request)

    def _get_trajectory_host(self) -> Any:
        return self.management.provider("trajectory")._get_trajectory_host()

    async def _handle_trajectory_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        return await self.management.provider("trajectory")._handle_trajectory_request(ws, request, send_lock)

    async def _handle_recommendation(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        return await self.management.provider("recommendations")._handle_recommendation(ws, request, send_lock)

    async def _push_recommendation_event(self, event_type: str, payload: dict[str, Any]) -> None:
        return await self.management.provider("recommendations")._push_recommendation_event(event_type, payload)

    def _recommendation_active_request_ids(self) -> frozenset[str]:
        return self.management.provider("recommendations")._recommendation_active_request_ids()

    def _recommendation_host_instance(self) -> Any:
        return self.management.provider("recommendations")._recommendation_host_instance()

    async def _start_recommendation_host(self) -> None:
        return await self.management.provider("recommendations")._start_recommendation_host()

    async def _stop_recommendation_host(self) -> None:
        return await self.management.provider("recommendations")._stop_recommendation_host()





















