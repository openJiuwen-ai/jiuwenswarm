"""注册、刷新与卸载进程级 MCP，并完成请求作用域工具装载。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.core.foundation.tool import McpServerConfig, ToolCard
from openjiuwen.core.runner import Runner

from jiuwenswarm.common.mcp_config import (
    CONNECTOR_DISCOVERY_FAILED,
    CONNECTOR_NO_TOOLS,
    OfficeClawMcpRegistration,
    RequestScopedOfficeClawMcpTool,
    build_mcp_server_config,
    clear_agent_office_claw_tool_ids,
    extract_enabled_mcp_server_entries,
    extract_office_claw_mcp,
    extract_request_mcp_servers,
    is_asyncio_outer_cancellation,
    is_request_scoped_mcp_tool_id,
    list_office_claw_mcp_tools,
    list_request_mcp_server_tools,
    preflight_mcp_server_reachable,
    publish_live_office_claw_allowlist,
    publish_request_scoped_mcp_registration,
    register_live_office_claw_tool_instance,
    release_request_scoped_mcp_sessions,
    replace_request_scoped_mcp_registration,
    revoke_live_office_claw_allowlist,
    revoke_request_scoped_mcp_registration,
    set_agent_office_claw_tool_ids,
    unregister_live_office_claw_tool_instance,
    validate_office_claw_mcp_config,
)
from jiuwenswarm.common.mcp_server_registry import (
    DisabledMcpServerError,
    McpRegistryChatError,
    UnknownMcpServerError,
    extract_mcp_server_list,
    get_mcp_server_registry,
)
from jiuwenswarm.common.mcp_tool_list_refresh_patch import (
    refresh_registered_mcp_tool_lists,
    resolve_mcp_tool_list_ttl_s,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import (
    _leftover_request_mcp_servers,
    _session_adapter_key,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.rail_factory import (
    _office_claw_thread_id_from_tools,
    _stage_request_mcp_tools,
)

logger = logging.getLogger(__name__)


class OfficeClawMcpBuiltinNameConflict(RuntimeError):
    """A request-scoped connector tool's short name collides with a *built-in*
    agent tool (not a foreign request-scoped tool).

    Unlike a foreign short-name conflict (which must fail closed to prevent a
    concurrent request from stealing the mapping), colliding with a built-in
    tool is benign: the connector tool simply cannot claim that short name, so
    the caller downgrades by skipping just that tool instead of rolling back
    the whole registration. Carries ``existing_id`` for diagnostics.
    """

    def __init__(self, message: str, *, existing_id: str = "") -> None:
        super().__init__(message)
        self.existing_id = existing_id


@dataclass
class _RequestMcpToolBuffers:
    """One-request MCP tool-install accumulators (G.FNM.03)."""

    tool_ids: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    registered_tools: list[RequestScopedOfficeClawMcpTool] = field(default_factory=list)
    seen_names: set[str] = field(default_factory=set)
    # Discover every source before mutating the shared resource/ability managers.
    # The bool preserves fail-closed registration for identity-pinned system tools.
    pending_tools: list[tuple[RequestScopedOfficeClawMcpTool, bool]] = field(default_factory=list)
    research_servers: list[dict[str, Any]] = field(default_factory=list, repr=False)
    research_tool_names: dict[str, list[str]] = field(default_factory=dict)
    unavailable_connectors: dict[str, str] = field(default_factory=dict)


async def _register_mcp_server(adapter, cfg: McpServerConfig, *, tag: str) -> bool:
    if adapter._instance is None:
        return False
    # Pre-flight reachability check for HTTP-based MCP servers. If the host
    # is down we skip registration here instead of entering the mcp
    # streamable-http context — otherwise openjiuwen leaks orphaned anyio
    # background tasks on the failed initialize() and logs noisy
    # ``aclose()``/cancel-scope RuntimeErrors.
    reachable, reason = await preflight_mcp_server_reachable(cfg)
    if not reachable:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] MCP server unreachable, skipping registration: "
            "name=%s transport=%s path=%s reason=%s",
            cfg.server_name, cfg.client_type, cfg.server_path, reason,
        )
        return False
    # Remote MCP connect failures may surface as CancelledError: anyio
    # TaskGroup cancel()+uncancel() the host task, and CancelledError is not
    # an Exception subclass (Python 3.8+), so the bare ``except Exception``
    # below cannot isolate it. ResourceMgr isolation that keys off
    # connect_task.cancelled() also fails after uncancel(). Catch
    # CancelledError here and use cancelling() to tell apart:
    #   - cancelling()==0 → anyio-internal cancel → skip this server
    #   - cancelling()>0  → real outer cancel (interrupt / WS drop) → re-raise
    try:
        ttl_s = _mcp_tool_list_ttl_s(adapter)
        # SDK rejects expiry_time <= 0; ttl==0 means "every turn" via turn hook.
        expiry = ttl_s if ttl_s > 0 else None
        result = await Runner.resource_mgr.add_mcp_server(
            cfg, tag=tag, expiry_time=expiry
        )
        ok = True
        if result is not None:
            is_ok = getattr(result, "is_ok", None)
            if callable(is_ok):
                ok = bool(is_ok())
            elif isinstance(result, bool):
                ok = result
        if ok:
            server_id = str(getattr(cfg, "server_id", "") or "").strip()
            if not server_id:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] MCP server_id missing after registration: %s", cfg
                )
                return False
            adapter._instance.ability_manager.add(cfg)
            adapter._registered_mcp_server_ids.add(server_id)
            adapter._registered_mcp_servers[server_id] = cfg
            return True
        logger.warning(
            "[JiuWenSwarmDeepAdapter] MCP server register failed: %s", cfg.server_name
        )
        return False
    except asyncio.CancelledError:
        if is_asyncio_outer_cancellation():
            raise
        logger.warning(
            "[JiuWenSwarmDeepAdapter] MCP register cancelled (anyio internal), skip: "
            "name=%s transport=%s path=%s",
            cfg.server_name,
            cfg.client_type,
            cfg.server_path,
        )
        return False
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] MCP server register failed: %s", exc)
        return False


def _mcp_tool_list_ttl_s(adapter) -> float:
    """Seconds between enterprise MCP tool-list refreshes (0 = every turn)."""
    config = adapter._config_base_cache if isinstance(adapter._config_base_cache, dict) else None
    if config is None and isinstance(adapter._config_cache, dict):
        config = adapter._config_cache
    return resolve_mcp_tool_list_ttl_s(config)


async def _refresh_stale_mcp_tool_lists(adapter) -> None:
    """Re-list remote MCP tools when TTL elapsed; invalidate rail if changed.

    Enterprise templates register tools once at add_mcp_server. Without this
    hook, mid-session tool additions on the MCP server stay invisible.
    """
    registered_ids = getattr(adapter, "_registered_mcp_server_ids", None)
    if not registered_ids:
        return
    try:
        changed = await refresh_registered_mcp_tool_lists(
            server_ids=sorted(registered_ids),
            ttl_s=_mcp_tool_list_ttl_s(adapter),
        )
    except Exception as exc:  # noqa: BLE001 — never block the user turn
        logger.warning(
            "[JiuWenSwarmDeepAdapter] MCP tool-list refresh failed: %s",
            exc,
        )
        return
    if not changed:
        return
    rail = getattr(adapter, "_progressive_tool_rail", None)
    invalidate = getattr(rail, "invalidate_deferred_tool_cache", None)
    if callable(invalidate):
        try:
            invalidate()
            logger.info(
                "[JiuWenSwarmDeepAdapter] progressive tool rail cache invalidated "
                "after MCP tool-list change"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[JiuWenSwarmDeepAdapter] progressive rail invalidate failed: %s",
                exc,
            )


async def _unregister_mcp_server(adapter, server_id: str) -> None:
    if adapter._instance is None:
        return
    cfg = adapter._registered_mcp_servers.get(server_id)
    server_name = getattr(cfg, "server_name", "") if cfg is not None else ""
    try:
            await Runner.resource_mgr.remove_mcp_server(server_id)
    except Exception as exc:
        logger.warning("[JiuWenSwarmDeepAdapter] MCP server remove failed: %s", exc)
    if server_name:
        try:
            adapter._instance.ability_manager.remove(server_name)
        except Exception as exc:
            logger.warning("[JiuWenSwarmDeepAdapter] MCP ability remove failed: %s", exc)
    adapter._registered_mcp_server_ids.discard(server_id)
    adapter._registered_mcp_servers.pop(server_id, None)


async def _append_identity_pinned_office_claw_tools(
    adapter,
    request: AgentRequest,
    raw_config: dict[str, Any],
    buffers: _RequestMcpToolBuffers,
    *,
    yield_to_existing: bool = False,
) -> str:
    params = validate_office_claw_mcp_config(raw_config)
    tool_defs = await list_office_claw_mcp_tools(params)
    _stage_request_mcp_tools(
        request, "office-claw", tool_defs, params, buffers,
        strict=True, yield_to_existing=yield_to_existing,
)
    env = params.get("env") if isinstance(params.get("env"), dict) else {}
    return str(env.get("OFFICE_CLAW_INVOCATION_ID") or "").strip()


async def _append_request_mcp_server_tools(
    adapter,
    request: AgentRequest,
    request_mcp_servers: dict[str, dict[str, Any]],
    buffers: _RequestMcpToolBuffers,
    invocation_id: str,
    *,
    skip_office_claw: bool = False,
) -> str:
    for server_name, server_config in request_mcp_servers.items():
        if server_name == "office-claw" and skip_office_claw:
            continue
        try:
            tool_defs, params = await list_request_mcp_server_tools(
                server_name, server_config,
            )
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] request-scoped MCP connector discovery failed: "
                "request_id=%s server=%s error=%s", request.request_id, server_name, exc,
            )
            if server_name != "office-claw":
                buffers.unavailable_connectors[server_name] = CONNECTOR_DISCOVERY_FAILED
            continue
        # Discovery reports failure as empty params; a legitimately empty
        # connector (e.g. every tool filtered off) still returns params.
        if server_name != "office-claw" and not tool_defs:
            buffers.unavailable_connectors[server_name] = (
                CONNECTOR_NO_TOOLS if params else CONNECTOR_DISCOVERY_FAILED
            )
        _stage_request_mcp_tools(request, server_name, tool_defs, params, buffers)
        if tool_defs:
            from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import project_search_server
            research_server = project_search_server(server_name, server_config, params)
            if research_server:
                buffers.research_servers.append(research_server)
                buffers.research_tool_names[server_name] = [str(tool.get("name") or "").strip()
                                                           for tool in tool_defs]
        if server_name == "office-claw" and not invocation_id:
            env = params.get("env") if isinstance(params.get("env"), dict) else {}
            invocation_id = str(env.get("OFFICE_CLAW_INVOCATION_ID") or "").strip()
    return invocation_id


def _install_staged_request_mcp_tools(adapter, buffers: _RequestMcpToolBuffers) -> None:
    """Install a fully discovered generation without yielding to another request."""
    # Older agent-core versions keep per-tool INFO logs without this optimization.
    logging_scope: AbstractContextManager[None]
    try:
        from openjiuwen.core.runner.resources_manager.registration_logging import batch_registration_logging
    except ModuleNotFoundError as exc:
        if exc.name != "openjiuwen.core.runner.resources_manager.registration_logging":
            raise
        logging_scope = nullcontext()
    else:
        logging_scope = batch_registration_logging()

    with logging_scope:
        for tool, strict in buffers.pending_tools:
            card = tool.card
            result = Runner.resource_mgr.add_tool(tool, tag="office-claw")
            is_ok = getattr(result, "is_ok", None)
            succeeded = bool(is_ok()) if callable(is_ok) else result is not False
            if not succeeded:
                if strict:
                    raise RuntimeError(f"failed to register OfficeClaw MCP tool: {card.name}")
                logger.warning("[JiuWenSwarmDeepAdapter] MCP add_tool failed: tool_id=%s", card.id)
                continue
            # Track before installing the card so failures can roll back only our resources.
            buffers.tool_ids.append(card.id)
            buffers.tool_names.append(card.name)
            buffers.registered_tools.append(tool)
            try:
                _install_office_claw_ability_card(adapter, card)
            except OfficeClawMcpBuiltinNameConflict:
                if strict:
                    raise
                # A user connector's built-in collision skips just that tool.
                try:
                    Runner.resource_mgr.remove_tool(card.id)
                except Exception as exc:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] MCP shadow-skip resource cleanup failed: "
                        "tool_id=%s error=%s", card.id, exc,
                    )
                buffers.tool_ids.pop()
                buffers.tool_names.pop()
                buffers.registered_tools.pop()
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] MCP tool shadows a built-in, skipped: tool_id=%s", card.id,
                )


def _activate_empty_request_scoped_mcp_registration(
    adapter, registration: OfficeClawMcpRegistration,
) -> OfficeClawMcpRegistration:
    if replace_request_scoped_mcp_registration(registration, registration):
        adapter._active_office_claw_mcp = registration
        adapter._deepresearch_mcp_snapshot = None
        set_agent_office_claw_tool_ids(adapter._instance, ())
        _sync_office_claw_allowlist_to_progressive_rail(adapter, None)
    logger.info("[latency] stage=2 name=mcp request_id=%s", registration.request_id)
    return registration


async def _register_mcp_from_registry(
    adapter,
    request: AgentRequest,
    server_names: list[str],
    office_claw_config: dict[str, Any] | None = None,
    leftover_servers: dict[str, dict[str, Any]] | None = None,
    *,
    generation: OfficeClawMcpRegistration,
) -> OfficeClawMcpRegistration:
    return await _register_request_mcp_tools(
        adapter,
        request,
        generation,
        server_names,
        office_claw_config,
        leftover_servers,
)


async def _register_request_mcp_tools(
    adapter,
    request: AgentRequest,
    generation: OfficeClawMcpRegistration,
    server_names: list[str],
    office_claw_config: dict[str, Any] | None,
    request_mcp_servers: dict[str, dict[str, Any]] | None,
) -> OfficeClawMcpRegistration:
    """Discover all sources, then install and publish only the current generation."""
    if adapter._instance is None:
        return _activate_empty_request_scoped_mcp_registration(adapter, generation)
    buffers = _RequestMcpToolBuffers()
    invocation_id = ""
    registration: OfficeClawMcpRegistration | None = None

    def _registration() -> OfficeClawMcpRegistration:
        return OfficeClawMcpRegistration(
            request_id=request.request_id,
            session_id=generation.session_id,
            tool_ids=tuple(buffers.tool_ids),
            tool_names=tuple(buffers.tool_names),
            tool_instances=tuple(buffers.registered_tools),
            invocation_id=invocation_id,
        )

    try:
        if server_names:
            snapshots = await get_mcp_server_registry().snapshot_for_chat(server_names)
            for name, tool_defs, params in snapshots:
                _stage_request_mcp_tools(
                    request, name, tool_defs, params, buffers, use_global_pool=True,
                )
                reader = getattr(get_mcp_server_registry(), "get_server", None)
                try:
                    policy = await reader(name) if callable(reader) else None
                except Exception:
                    policy = None
                if policy and policy.get("enabled") and tool_defs:
                    from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import (
                        project_search_server,
                    )
                    research_server = project_search_server(name, policy["config"], params)
                    if research_server:
                        buffers.research_servers.append(research_server)
                        buffers.research_tool_names[name] = [str(tool.get("name") or "").strip()
                                                             for tool in tool_defs]
        # Preserve source priority: registry -> leftovers -> office-claw when
        # a registry list is present; otherwise office-claw -> connectors.
        if office_claw_config is not None and not server_names:
            invocation_id = await _append_identity_pinned_office_claw_tools(
                adapter,
                request,
                office_claw_config,
                buffers,
            )
        if request_mcp_servers:
            invocation_id = await _append_request_mcp_server_tools(
                adapter,
                request,
                request_mcp_servers,
                buffers,
                invocation_id,
                skip_office_claw=office_claw_config is not None,
            )
        if office_claw_config is not None and server_names:
            invocation_id = await _append_identity_pinned_office_claw_tools(
                adapter,
                request,
                office_claw_config,
                buffers,
                yield_to_existing=True,
            )

        if buffers.unavailable_connectors:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] request-scoped MCP connectors unavailable this request: "
                "request_id=%s connectors=%s", request.request_id, buffers.unavailable_connectors,
            )
            for tool, _strict in buffers.pending_tools:
                if tool.server_name == "office-claw":
                    tool.set_unavailable_connectors(buffers.unavailable_connectors)

        if not replace_request_scoped_mcp_registration(generation, generation):
            logger.info("[latency] stage=2 name=mcp request_id=%s", request.request_id)
            return generation
        # There must be no await from this guard through publication: a slow
        # old discovery must never overwrite a newer request's tool bindings.
        install_started = time.perf_counter()
        _install_staged_request_mcp_tools(adapter, buffers)
        install_ms = (time.perf_counter() - install_started) * 1000
        registration = _registration()
        if not replace_request_scoped_mcp_registration(generation, registration):
            raise RuntimeError("request-scoped MCP generation superseded during install")
        adapter._active_office_claw_mcp = registration
        set_agent_office_claw_tool_ids(adapter._instance, registration.tool_ids)
        from jiuwenswarm.agents.harness.common.tools.deepresearch.mcp_search import fully_admitted_servers
        research_servers = fully_admitted_servers(
            buffers.research_servers, buffers.research_tool_names,
            [(tool.server_name, tool.card.name) for tool in registration.tool_instances],
        )
        adapter._deepresearch_mcp_snapshot = (registration, research_servers, frozenset(server_names))
        publish_live_office_claw_allowlist(registration.tool_ids)
        for tool in registration.tool_instances:
            register_live_office_claw_tool_instance(tool, registration.tool_ids)
        _sync_office_claw_allowlist_to_progressive_rail(
            adapter,
            registration.tool_ids,
            delivery_thread_id=_office_claw_thread_id_from_tools(buffers.registered_tools),
            invocation_id=invocation_id,
        )
        logger.info(
            "[JiuWenSwarmDeepAdapter] request-scoped MCP registered: "
            "request_id=%s session_id=%s tools=%s registered=%s skipped=%s install_ms=%.1f",
            request.request_id, request.session_id, buffers.tool_names,
            len(buffers.tool_ids), len(buffers.pending_tools) - len(buffers.tool_ids), install_ms,
        )
        logger.info("[latency] stage=2 name=mcp request_id=%s", request.request_id)
        return registration
    except (asyncio.CancelledError, McpRegistryChatError, UnknownMcpServerError, DisabledMcpServerError):
        await cleanup_request_scoped_office_claw_mcp(adapter, registration or _registration())
        revoke_request_scoped_mcp_registration(generation)
        raise
    except Exception as exc:
        # Restore the empty generation before cleanup can yield. Use the
        # published object's identity so rollback cannot retain a dead
        # registration or overwrite a concurrent newer request.
        if registration is not None:
            replace_request_scoped_mcp_registration(registration, generation)
        await cleanup_request_scoped_office_claw_mcp(adapter, registration or _registration())
        logger.warning(
            "[JiuWenSwarmDeepAdapter] request-scoped MCP registration failed; "
            "continuing without tools: request_id=%s error=%s", request.request_id, exc,
        )
        return _activate_empty_request_scoped_mcp_registration(adapter, generation)


async def register_request_scoped_office_claw_mcp(
    adapter, request: AgentRequest,
) -> OfficeClawMcpRegistration:
    """Install Relay MCP tools and publish their lifetime to in-process Teams.

    mcp_server_list keeps priority over overlapping request_mcp_servers;
    unlisted connectors and identity-pinned office-claw tools are retained.
    Empty requests still own a generation so members cannot reuse old tools.
    """
    server_list = extract_mcp_server_list(request.params)
    raw_config = extract_office_claw_mcp(request.params)
    request_mcp_servers = extract_request_mcp_servers(request.params)
    generation = OfficeClawMcpRegistration(
        request_id=request.request_id, session_id=_session_adapter_key(request.session_id),
        tool_ids=(), tool_names=(),
)
    publish_request_scoped_mcp_registration(generation)
    if server_list is not None:
        request_mcp_servers, dropped = _leftover_request_mcp_servers(
            request_mcp_servers, server_list,
        )
        if dropped:
            logger.info(
                "[JiuWenSwarmDeepAdapter] registry covers request connectors: "
                "request_id=%s names=%s", request.request_id, dropped,
            )
        if server_list:
            return await _register_mcp_from_registry(
                adapter,
                request,
                server_list,
                office_claw_config=raw_config,
                leftover_servers=request_mcp_servers,
                generation=generation,
            )
    return await _register_request_mcp_tools(
        adapter,
        request,
        generation,
        [],
        raw_config,
        request_mcp_servers,
)


def _owned_office_claw_tool_ids(adapter) -> frozenset[str]:
    active = adapter._active_office_claw_mcp
    if active is None:
        return frozenset()
    return frozenset(active.tool_ids)


def _install_office_claw_ability_card(adapter, card: ToolCard) -> None:
    """Register a request-scoped OfficeClaw tool card by short name.

    Same-id rebinds are allowed. A different id is replaced only when the
    existing card belongs to this adapter's active registration; otherwise
    registration fails hard so a concurrent request cannot silently steal
    the short-name mapping.
    """

    if adapter._instance is None:
        raise RuntimeError("OfficeClaw MCP ability install requires an initialized agent")
    ability_manager = adapter._instance.ability_manager
    tool_name = str(card.name or "").strip()
    tool_id = str(card.id or "").strip()
    if not tool_name or not tool_id:
        raise RuntimeError("OfficeClaw MCP tool card is missing name or id")

    existing = None
    getter = getattr(ability_manager, "get", None)
    if callable(getter):
        existing = getter(tool_name)
    existing_id = str(getattr(existing, "id", "") or "") if existing is not None else ""

    if existing is not None and existing_id and existing_id != tool_id:
        if existing_id in _owned_office_claw_tool_ids(adapter):
            try:
                ability_manager.remove(tool_name)
            except Exception as exc:
                raise RuntimeError(
                    f"failed to replace owned OfficeClaw MCP ability: {tool_name}"
                ) from exc
            try:
                Runner.resource_mgr.remove_tool(existing_id)
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] failed to remove replaced OfficeClaw MCP "
                    "resource: tool_id=%s error=%s",
                    existing_id,
                    exc,
                )
        elif is_request_scoped_mcp_tool_id(existing_id):
            # Foreign request-scoped tool (different request scope) owns the
            # short name. Fail closed so a concurrent request cannot silently
            # steal the mapping.
            raise RuntimeError(
                f"OfficeClaw MCP tool name conflicts with an existing tool: {tool_name} "
                f"(existing_id={existing_id}, new_id={tool_id})"
            )
        else:
            # The existing mapping points at a *built-in* agent tool (id does
            # not carry the request-scoped prefix). A connector tool merely
            # shadows a built-in short name — benign and common (e.g. a
            # ``filesystem`` connector exposing ``read_file``). Downgrade by
            # skipping just this tool so the rest of the connector and other
            # connectors still register, instead of rolling back the whole
            # request-scoped registration.
            raise OfficeClawMcpBuiltinNameConflict(
                f"OfficeClaw MCP connector tool '{tool_name}' shadows a built-in tool; "
                f"skipping (existing_id={existing_id}, new_id={tool_id})",
                existing_id=existing_id,
            )

    ability_result = ability_manager.add(card)
    added = getattr(ability_result, "added", None) if ability_result is not None else None
    if added is False:
        # Conflict-aware AbilityManager kept the existing card. Retry once if
        # we own it; otherwise fail closed.
        existing = getter(tool_name) if callable(getter) else None
        existing_id = str(getattr(existing, "id", "") or "") if existing is not None else ""
        if existing_id and existing_id in _owned_office_claw_tool_ids(adapter):
            ability_manager.remove(tool_name)
            ability_result = ability_manager.add(card)
            added = getattr(ability_result, "added", None) if ability_result is not None else None
        if added is False:
            if existing_id and is_request_scoped_mcp_tool_id(existing_id):
                raise RuntimeError(
                    f"OfficeClaw MCP tool name conflicts with an existing tool: {tool_name}"
                )
            raise OfficeClawMcpBuiltinNameConflict(
                f"OfficeClaw MCP connector tool '{tool_name}' shadows a built-in tool; "
                f"skipping (existing_id={existing_id or '-'}, new_id={tool_id})",
                existing_id=existing_id or "",
            )

    # Legacy AbilityManager.add() returns None and may overwrite by name.
    # Always verify the short-name mapping lands on *this* card id so a
    # conflict-aware or raced manager cannot leave a foreign bind in place.
    installed = getter(tool_name) if callable(getter) else None
    installed_id = str(getattr(installed, "id", "") or "") if installed is not None else ""
    if installed_id != tool_id:
        if installed_id and is_request_scoped_mcp_tool_id(installed_id):
            # Foreign request-scoped tool stole the short name: fail closed.
            raise RuntimeError(
                f"OfficeClaw MCP tool name conflicts with an existing tool: {tool_name} "
                f"(existing_id={installed_id or '-'}, new_id={tool_id})"
            )
        # Otherwise the mapping points at a built-in tool: downgrade by
        # skipping this connector tool rather than rolling back the whole
        # registration.
        raise OfficeClawMcpBuiltinNameConflict(
            f"OfficeClaw MCP connector tool '{tool_name}' shadows a built-in tool; "
            f"skipping (existing_id={installed_id or '-'}, new_id={tool_id})",
            existing_id=installed_id or "",
        )


async def cleanup_request_scoped_office_claw_mcp(
    adapter,
    registration: OfficeClawMcpRegistration | None,
) -> None:
    """Best-effort removal of tools installed for one Relay request."""

    if registration is None:
        return
    revoke_request_scoped_mcp_registration(registration)
    for registered_tool in registration.tool_instances:
        unregister_live_office_claw_tool_instance(registered_tool)
    for tool_id in registration.tool_ids:
        try:
            Runner.resource_mgr.remove_tool(tool_id)
        except Exception as exc:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] request-scoped OfficeClaw MCP resource cleanup failed: "
                "request_id=%s tool_id=%s error=%s",
                registration.request_id,
                tool_id,
                exc,
            )
    if adapter._instance is not None:
        ability_manager = adapter._instance.ability_manager
        getter = getattr(ability_manager, "get", None)
        owned_ids = frozenset(registration.tool_ids)
        for tool_name in registration.tool_names:
            try:
                existing = getter(tool_name) if callable(getter) else None
                existing_id = (
                    str(getattr(existing, "id", "") or "") if existing is not None else ""
                )
                # Only drop the short-name mapping when it still points at
                # this request's tool id — never delete another request's bind.
                if existing is None:
                    continue
                if existing_id and existing_id not in owned_ids:
                    logger.warning(
                        "[JiuWenSwarmDeepAdapter] skip OfficeClaw MCP ability cleanup; "
                        "short name rebound: request_id=%s tool=%s existing_id=%s",
                        registration.request_id,
                        tool_name,
                        existing_id,
                    )
                    continue
                ability_manager.remove(tool_name)
            except Exception as exc:
                logger.warning(
                    "[JiuWenSwarmDeepAdapter] request-scoped OfficeClaw MCP ability cleanup failed: "
                    "request_id=%s tool=%s error=%s",
                    registration.request_id,
                    tool_name,
                    exc,
                )
    if (
        adapter._active_office_claw_mcp is not None
        and adapter._active_office_claw_mcp.request_id == registration.request_id
    ):
        adapter._active_office_claw_mcp = None
        adapter._deepresearch_mcp_snapshot = None
        _sync_office_claw_allowlist_to_progressive_rail(adapter, None)
        # Only clear the shared AM allowlist when we are cleaning the *current*
        # active registration. A superseded request's cleanup must not wipe the
        # newer request's allowlist (ask_user resume race → active_id empty).
        clear_agent_office_claw_tool_ids(adapter._instance)
    elif adapter._active_office_claw_mcp is None:
        clear_agent_office_claw_tool_ids(adapter._instance)
        _sync_office_claw_allowlist_to_progressive_rail(adapter, None)
    else:
        logger.info(
            "[JiuWenSwarmDeepAdapter] cleanup skipped clearing active OfficeClaw "
            "allowlist: cleaned_request=%s active_request=%s",
            registration.request_id,
            adapter._active_office_claw_mcp.request_id,
        )
    revoke_live_office_claw_allowlist(registration.tool_ids)
    # 销毁本请求池化的长生命周期 stdio session（如 chrome-devtools-mcp 浏览器进程），
    # 以免其活过 chat.send。best-effort，不阻塞清理。
    try:
        await release_request_scoped_mcp_sessions(registration.request_id)
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] request-scoped MCP session release failed: "
            "request_id=%s error=%s",
            registration.request_id,
            exc,
        )
    logger.info(
        "[JiuWenSwarmDeepAdapter] request-scoped OfficeClaw MCP cleaned up: request_id=%s",
        registration.request_id,
)


def _sync_office_claw_allowlist_to_progressive_rail(
    adapter,
    tool_ids: tuple[str, ...] | list[str] | frozenset[str] | None,
    *,
    delivery_thread_id: str | None = None,
    invocation_id: str | None = None,
) -> None:
    """Keep ProgressiveToolRail's interaction-round allowlist in sync."""

    rail = getattr(adapter, "_progressive_tool_rail", None)
    setter = getattr(rail, "set_office_claw_active_tool_ids", None)
    if not callable(setter):
        return
    try:
        setter(
            tool_ids,
            delivery_thread_id=delivery_thread_id,
            invocation_id=invocation_id,
        )
    except TypeError:
        # Older rail signature without keyword binding metadata.
        setter(tool_ids)
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] failed to sync OfficeClaw allowlist to "
            "ProgressiveToolRail: %s",
            exc,
        )


async def _register_mcp_servers_from_config(
    adapter, config_base: dict[str, Any], *, tag: str = "agent.main"
) -> None:
    enabled_entries = extract_enabled_mcp_server_entries(config_base)
    for entry in enabled_entries:
        cfg = build_mcp_server_config(entry, server_id_scope="jiuwenswarm")
        if cfg is None:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] skip invalid mcp server entry: %s",
                entry.get("name", "<unknown>"),
            )
            continue
        await _register_mcp_server(adapter, cfg, tag=tag)


async def _sync_mcp_servers_for_runtime(
    adapter, config_base: dict[str, Any], *, tag: str = "agent.reload"
) -> None:
    if adapter._instance is None:
        return
    enabled_entries = extract_enabled_mcp_server_entries(config_base)
    desired_by_name: dict[str, McpServerConfig] = {}
    for entry in enabled_entries:
        cfg = build_mcp_server_config(entry, server_id_scope="jiuwenswarm")
        if cfg is None:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] skip invalid mcp server entry: %s",
                entry.get("name", "<unknown>"),
            )
            continue
        server_name = str(getattr(cfg, "server_name", "") or "").strip()
        if not server_name:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] skip mcp server without server_name: %s",
                entry.get("name", "<unknown>"),
            )
            continue
        desired_by_name[server_name] = cfg

    current_by_name: dict[str, tuple[str, McpServerConfig]] = {}
    for server_id, cfg in adapter._registered_mcp_servers.items():
        server_name = str(getattr(cfg, "server_name", "") or "").strip()
        if not server_name or server_name in current_by_name:
            continue
        current_by_name[server_name] = (server_id, cfg)

    current_names = set(current_by_name.keys())
    desired_names = set(desired_by_name.keys())
    to_remove = current_names - desired_names
    to_add = desired_names - current_names
    to_check = current_names & desired_names

    for server_name in to_remove:
        server_id = current_by_name[server_name][0]
        await _unregister_mcp_server(adapter, server_id)

    for server_name in to_add:
        await _register_mcp_server(adapter, desired_by_name[server_name], tag=tag)

    for server_name in to_check:
        server_id, current_cfg = current_by_name[server_name]
        desired_cfg = desired_by_name[server_name]
        current_sig = {
            "server_name": getattr(current_cfg, "server_name", None),
            "client_type": getattr(current_cfg, "client_type", None),
            "server_path": getattr(current_cfg, "server_path", None),
            "params": getattr(current_cfg, "params", None),
            "auth_headers": getattr(current_cfg, "auth_headers", None),
            "auth_query_params": getattr(current_cfg, "auth_query_params", None),
        }
        desired_sig = {
            "server_name": getattr(desired_cfg, "server_name", None),
            "client_type": getattr(desired_cfg, "client_type", None),
            "server_path": getattr(desired_cfg, "server_path", None),
            "params": getattr(desired_cfg, "params", None),
            "auth_headers": getattr(desired_cfg, "auth_headers", None),
            "auth_query_params": getattr(desired_cfg, "auth_query_params", None),
        }
        if json.dumps(current_sig, sort_keys=True, default=str) == json.dumps(
            desired_sig, sort_keys=True, default=str
        ):
            continue
        await _unregister_mcp_server(adapter, server_id)
        await _register_mcp_server(adapter, desired_cfg, tag=tag)
