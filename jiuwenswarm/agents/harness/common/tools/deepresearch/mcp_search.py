"""Search-source authorization and request-owned MCP endpoint for DeepResearch."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import math
import secrets
import socket
from contextlib import asynccontextmanager, nullcontext
from typing import Any
from urllib.parse import parse_qsl, urlsplit

# Native SDK persists a redacted copy of its inputs. Only these authentication
# forms are covered by that sanitizer; do not silently strip unknown headers.
REDACTED_HEADERS = frozenset({
    "authorization", "proxy-authorization", "x-api-key", "api-key", "apikey",
    "x-auth-token", "x-custom-token", "x-auth", "bearer-token", "x-secret", "secret",
    "x-goog-api-key", "x-goog-cloud-api-key", "anthropic-api-key", "x-deepseek-api-key",
    "cookie", "set-cookie",
})
# Only PR !438's documented Tavily URL credential is currently supported.
# A redaction hint such as "auth_scope" is not proof of a rotatable credential.
CREDENTIAL_QUERY_KEYS = frozenset({"tavilyApiKey"})


def source_identity(server: dict) -> str:
    """Pin source and scope, allowing rotation of SDK-redacted credentials."""
    parsed = urlsplit(server["server_url"])
    query = sorted((key, "" if key in CREDENTIAL_QUERY_KEYS else value)
                   for key, value in parse_qsl(parsed.query, keep_blank_values=True))
    identity = [server["server_name"], parsed.scheme.lower(), parsed.netloc.lower(),
                parsed.path.rstrip("/"), query, server["transport_type"],
                sorted(key.lower() for key in server["headers"])]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()


def fully_admitted_servers(servers: list[dict], discovered: dict[str, list[str]],
                           installed: list[tuple[str, str]]) -> list[dict]:
    """Require discovery and successful installation to agree before binding."""
    admitted = {}
    for server_name, tool_name in installed:
        admitted.setdefault(server_name, []).append(tool_name)
    result = []
    for server in servers:
        name = server["server_name"]
        if discovered.get(name) and sorted(discovered[name]) == sorted(admitted.get(name, [])):
            result.append(server)
    return result


def project_search_server(name: str, config: dict, params: dict) -> dict | None:
    """Map admitted host MCP to SDK's default search purpose, without classification.

    Host ``type`` describes transport; SDK ``type`` describes purpose and
    defaults to ``search``. Tool selection and authorization stay with the host.
    """
    if not name or config.get("auth"):
        return None
    transport = params.get("_mcp_client_type")
    if transport not in {"sse", "streamable-http"} or params.get("auth_query_params"):
        return None
    url = params.get("server_path")
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password or parsed.fragment:
            return None
        if any(key not in CREDENTIAL_QUERY_KEYS
               for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            return None
    except ValueError:
        return None
    headers = params.get("auth_headers") or {}
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and k.lower() in REDACTED_HEADERS and isinstance(v, str)
        and not any(c in k + v for c in "\r\n") for k, v in headers.items()
    ):
        return None
    if any(v.startswith(("SDK-HMAC-", "V11-HMAC-")) for v in headers.values()):
        return None
    timeout = params.get("timeout_s", 30.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return None
    if not math.isfinite(timeout) or timeout <= 0:
        return None
    return {"server_name": name, "server_url": url,
            "transport_type": "sse" if transport == "sse" else "streamable_http",
            "headers": dict(headers), "timeout": float(timeout), "type": "search"}


async def get_search_mcp_context(adapter, *, registry_provider, owner_policy_provider,
                                request_state, resolve_permission_config) -> list[dict]:
    """Bind only current-request tools to the private research MCP endpoint."""
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import TOOL_PERMISSION_CONTEXT
    from jiuwenswarm.common.config import resolve_string_or_list_config
    from jiuwenswarm.common.mcp_config import bind_active_office_claw_mcp_tools
    snapshot, active, route = request_state()
    permission_context = TOOL_PERMISSION_CONTEXT.get()
    if not snapshot or active is not snapshot[0]:
        return []
    registry_names = snapshot[2] if len(snapshot) > 2 else frozenset()

    def current_request():
        current_snapshot, current_active, current_route = request_state()
        return (snapshot is current_snapshot and active is current_active
                and route is current_route
                and active.request_id == str(route.get("request_id") or "")
                and active.session_id == str(route.get("session_id") or ""))

    async def allowed(source, name, arguments):
        try:
            if not current_request():
                return False
            if source["server_name"] in registry_names:
                config, definitions, params = await registry_provider().snapshot_for_deepresearch(
                    source["server_name"])
                latest = project_search_server(source["server_name"], config, params)
                if (not latest or source_identity(latest) != source_identity(source)
                        or name not in {tool.get("name") for tool in definitions}):
                    return False
            # Recheck after the last await, including actual call arguments.
            if not current_request():
                return False
            disabled = set(getattr(getattr(adapter, "_disabled_tools_rail", None), "_disabled_tools", ()) or ())
            disabled.update(resolve_string_or_list_config(
                (getattr(adapter, "_config_cache", None) or {}).get("disabled_tools")))
            if name in disabled:
                return False
            policy = resolve_permission_config(session_id=active.session_id)
            if permission_context is not None:
                # Avatar approvals use a separate scene hook. Do not bypass
                # that interactive boundary through a background SDK call.
                if permission_context.scene == "group_digital_avatar":
                    return False
                principal = permission_context.principal_user_id
                channel = permission_context.channel_id
                if principal and channel:
                    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
                        _resolve_owner_scope_level,
                    )
                    for owner_policy in (policy, owner_policy_provider()):
                        scopes = owner_policy.get("owner_scopes") or {}
                        scope = (scopes.get(channel) or {}).get(principal)
                        if _resolve_owner_scope_level(scope, name, arguments) not in {None, "allow"}:
                            return False
            from openjiuwen.harness.security.skill_authorization import (
                get_skill_authorization_context, get_skill_grant_store,
            )
            authorization = get_skill_authorization_context()
            if authorization is not None:
                from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization_permission_rail import (
                    _compose_active_skill_permissions,
                )
                policy = _compose_active_skill_permissions(
                    policy, authorization.session_id, authorization.agent_scope_id, get_skill_grant_store())
            enabled = policy.get("enabled") if isinstance(policy, dict) else None
            if enabled is False:
                return True
            engine = getattr(getattr(adapter, "_permission_rail", None), "_engine", None)
            if enabled is not True or engine is None:
                return False
            from openjiuwen.harness.security.models import PermissionLevel
            from openjiuwen.harness.security.core import PermissionEngine
            fresh = PermissionEngine(config=copy.deepcopy(policy),
                                     workspace_root=getattr(engine, "_workspace_root", None),
                                     trusted_dirs=getattr(engine, "_trusted_dirs", None))
            if fresh.check_tool_permission_directly(name, arguments)[0] != PermissionLevel.ALLOW:
                return False
            # Without an explicit scope retain any tightening already applied
            # by the host's active Skill; never mutate its shared engine.
            return (authorization is not None
                    or engine.check_tool_permission_directly(name, arguments)[0] == PermissionLevel.ALLOW)
        except Exception:
            return False

    async def invoke(tool, arguments):
        with bind_active_office_claw_mcp_tools(active.tool_ids):
            return await tool.invoke(arguments)

    result = []
    for source in snapshot[1]:
        if source["server_name"] in registry_names:
            try:
                config, _, params = await registry_provider().snapshot_for_deepresearch(source["server_name"])
                latest = project_search_server(source["server_name"], config, params)
                if not latest or source_identity(latest) != source_identity(source):
                    continue
                source = latest
            except Exception:
                # 不记录可能含凭据的异常；刷新失败时仍拒绝委派该来源。
                logging.getLogger(__name__).warning("[DeepResearch] MCP source refresh unavailable")
                continue
        tools = [tool for tool in active.tool_instances if tool.server_name == source["server_name"]]
        grant = SearchMcpServer(source, tools,
                                lambda name, args, source=source: allowed(source, name, args), invoke)
        if any([await grant.allowed(name, {}) for name in grant.tools]):
            result.append(grant)
    return result


class SearchMcpServer(dict):
    """Native source identity plus runtime-only, host-authorized tool bindings."""

    def __init__(self, config, tools, allowed, invoke):
        super().__init__(copy.deepcopy(config))
        self.tools = {tool.card.name: tool for tool in tools}
        self._allowed = allowed
        self._invoke = invoke

    async def allowed(self, name: str, arguments: dict) -> bool:
        return name in self.tools and await self._allowed(name, arguments)

    async def call(self, name: str, arguments: dict) -> Any:
        if not await self.allowed(name, arguments):
            raise PermissionError('mcp_access_unavailable')
        return await self._invoke(self.tools[name], arguments)


@asynccontextmanager
async def search_mcp_gateway(servers):
    """Expose a frozen tool set; recheck the host before every list and call."""
    if not servers:
        yield []
        return
    if not all(isinstance(server, SearchMcpServer) for server in servers):
        raise ValueError('mcp_authorization_missing')
    from mcp.server.fastmcp import FastMCP
    from mcp import types
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response

    bindings = {}
    call_budgets = []
    for server in servers:
        call_budgets.append(float(server.get('timeout', 300)))
        for name in server.tools:
            params = getattr(server.tools[name], '_params', {})
            call_budgets.append(float(params.get('timeout_s') or 300))
            identity = server['server_name'] + '\0' + name
            alias = 't' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
            if alias in bindings:
                raise ValueError('mcp_tool_identity_collision')
            bindings[alias] = (server, name)

    class SearchGateway(FastMCP):
        @property
        def protocol_server(self):
            # 动态列表需直接注册低层处理器；FastMCP 没有对应公开注册接口。
            return self._mcp_server

    mcp = SearchGateway('deepresearch-search', stateless_http=True, json_response=True)

    @mcp.protocol_server.list_tools()
    async def list_tools():
        tools = []
        for alias, (server, name) in bindings.items():
            if await server.allowed(name, {}):
                card = server.tools[name].card
                tools.append(types.Tool(name=alias, description=f'{name}: {card.description or ""}',
                                        inputSchema=card.input_params or {'type': 'object'}))
        return tools

    @mcp.protocol_server.call_tool()
    async def call_tool(name, arguments):
        try:
            server, original = bindings[name]
            result = await server.call(original, arguments or {})
            # RequestScopedOfficeClawMcpTool returns the remote TextContent in result.
            value = result.get('result') if isinstance(result, dict) and set(result) == {'result'} else result
            content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            return [types.TextContent(type='text', text=content)]
        except Exception:
            # Remote errors may include credentials or URLs. Keep the boundary opaque.
            return types.CallToolResult(isError=True, content=[types.TextContent(
                type='text', text='mcp_access_unavailable')])

    app = mcp.streamable_http_app()
    credential = 'Bearer ' + secrets.token_urlsafe(32)

    async def authenticate(request, call_next):
        if not hmac.compare_digest(
            request.headers.get('authorization', '').encode('utf-8'), credential.encode('ascii')
        ):
            return Response(status_code=401)
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=authenticate)

    sock = socket.socket()
    task = None
    server = None
    try:
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        sock.setblocking(False)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, log_config=None, access_log=False, log_level='warning',
            timeout_graceful_shutdown=1, lifespan='on'))
        # An embedded task must not replace the host service's signal handlers.
        def capture_signals():
            return nullcontext()

        def install_signal_handlers():
            return None

        server.capture_signals = capture_signals
        server.install_signal_handlers = install_signal_handlers
        task = asyncio.create_task(server.serve(sockets=[sock]))

        async def started():
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError('mcp_gateway_start_failed')
                await asyncio.sleep(.01)
        await asyncio.wait_for(started(), timeout=5)
        yield [{'server_name': 'research_search', 'server_url': f'http://127.0.0.1:{port}/mcp',
                'transport_type': 'streamable_http', 'headers': {'Authorization': credential},
                # Host MCP invoke may reconnect and retry once. Keep the local
                # transport alive for that budget without changing upstream timeouts.
                'timeout': 2 * max(call_budgets) + 5, 'type': 'search'}]
    finally:
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3)
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        sock.close()
