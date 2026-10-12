# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""市场里的 MCP 细操作：浏览、安装、连接、断开、保存凭证。

和 /mcp 命令不是同一套入口。/mcp 改的是配置文件里的服务；
这里管的是市场目录里的安装和连接。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers.mcp import _mask_sensitive_fields

logger = logging.getLogger(__name__)

async def handle_mcp_list(ctx: RequestContext) -> None:
    """Handle ``mcp.list`` RPC: list marketplace MCPs (read-only).

    filter: builtin(预置目录) | local(已连接预置 + 全部自定义)。兜底 builtin。
    web 不转发此入口（网关本地已处理），保留只为语义对齐。
    """
    request = ctx.request
    try:
        from jiuwenswarm.server.runtime.mcp.marketplace import (
            list_mcps_with_hub,
        )
        params = request.params or {}
        filter_val = str(params.get("filter") or "builtin").strip().lower() or "builtin"
        if filter_val not in ("builtin", "local"):
            filter_val = "builtin"
        items = await list_mcps_with_hub(
            filter_val,
            cache_mode=params.get("cache_mode"),
            refresh=params.get("refresh") is True,
            query=str(params.get("query") or ""),
        )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"type": "list", "items": items, **({"cache": items.cache} if hasattr(items, "cache") else {})},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] mcp.list failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
        )
    await ctx.sink.send_response(resp)

async def handle_mcp_show(ctx: RequestContext) -> None:
    """Handle ``mcp.show`` RPC: return one MCP detail with tools."""
    request = ctx.request
    try:
        from jiuwenswarm.server.runtime.mcp.marketplace import show_mcp_with_hub
        params = request.params or {}
        name = str(params.get("id") or params.get("name") or "").strip()
        if not name:
            raise ValueError("mcp id or name is required")
        item = await show_mcp_with_hub(name)
        if item is None:
            raise KeyError(f"mcp '{name}' not found")
        # Connected MCP but ToolMgr returned no tools: fall back to a
        # temporary connection. Shared with the gateway-layer show so the
        # fallback logic lives in one place (fill_mcp_tools_fallback).
        from jiuwenswarm.common.mcp_config import fill_mcp_tools_fallback
        await fill_mcp_tools_fallback(item, name)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"type": "detail", "item": item},
        )
    except KeyError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "show_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
        )
    except ValueError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] mcp.show failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
        )
    await ctx.sink.send_response(resp)

async def handle_mcp_install(ctx: RequestContext) -> None:
    """Download, install, and connect a Hub MCP in one interaction."""
    request = ctx.request
    services = ctx.services
    try:
        from jiuwenswarm.server.runtime.mcp.marketplace import install_hub_mcp
        from jiuwenswarm.server.runtime.mcp.registry import CliConnectError

        asset_id = str((request.params or {}).get("id") or "").strip()
        item = await install_hub_mcp(asset_id)
        name = str(item.get("name") or "").strip()
        # Connect failure is non-fatal after a successful download.
        try:
            connect = await _run_mcp_connect_flow(services, 
                name, rollback_on_probe_failure=False
            )
        except CliConnectError as cli_exc:
            connect = {
                "type": "connect_failed",
                "error": str(cli_exc),
                "code": cli_exc.code,
                "runtime": cli_exc.runtime,
                "install_cmd": cli_exc.install_cmd,
                "name": name,
            }
        except Exception as connect_exc:  # noqa: BLE001
            logger.warning(
                "[AgentWebSocketServer] mcp.install connect step failed for '%s': %s",
                name, connect_exc,
            )
            connect = {
                "type": "connect_failed",
                "error": str(connect_exc),
                "code": "MCP_INTERNAL",
                "name": name,
            }
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"type": "installed", "item": item, "connect": connect},
        )
    except (KeyError, ValueError) as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "install_failed", "error": str(exc), "code": "MCP_BAD_REQUEST"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] mcp.install failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "install_failed", "error": str(exc), "code": "MCP_INTERNAL"},
        )
    await ctx.sink.send_response(resp)

async def handle_mcp_uninstall(ctx: RequestContext) -> None:
    """Disconnect and remove an installed Hub MCP package."""
    request = ctx.request
    services = ctx.services
    try:
        from jiuwenswarm.server.runtime.mcp.marketplace import uninstall_hub_mcp

        asset_id = str((request.params or {}).get("id") or "").strip()
        item = await asyncio.to_thread(uninstall_hub_mcp, asset_id)
        name = str(item.get("name") or "").strip()
        applied = True
        error_message = ""
        try:
            agent_manager = services.agent_manager
        except AttributeError:
            agent_manager = None
        if agent_manager is not None and name:
            try:
                await asyncio.wait_for(
                    asyncio.shield(
                        agent_manager.apply_mcp_change(name, "remove")
                    ),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                applied = False
                error_message = (
                    "MCP unregister timed out (package removed; server will "
                    "clear on next reload)"
                )
                logger.warning(
                    "[mcp] apply_mcp_change after Hub uninstall timed out for '%s'",
                    name,
                )
            except asyncio.CancelledError:
                logger.warning(
                    "[mcp] apply_mcp_change after Hub uninstall CancelledError "
                    "for '%s'",
                    name,
                )
                raise
            except Exception as reload_exc:  # noqa: BLE001
                applied = False
                error_message = str(reload_exc)
                logger.warning(
                    "[mcp] apply_mcp_change after Hub uninstall failed: %s",
                    reload_exc,
                )
            try:
                agent_manager.clear_mcp_credentials(name)
            except Exception as clear_exc:  # noqa: BLE001
                logger.warning(
                    "[mcp] clear_mcp_credentials after Hub uninstall failed: %s",
                    clear_exc,
                )
            try:
                await agent_manager.refresh_skill_rails()
            except Exception as skill_exc:  # noqa: BLE001
                logger.warning(
                    "[mcp] refresh_skill_rails after Hub uninstall failed: %s",
                    skill_exc,
                )
        payload = {"type": "uninstalled", "item": item, "applied": applied}
        if error_message:
            payload["error"] = error_message
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
    except (KeyError, ValueError) as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "uninstall_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] mcp.uninstall failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "uninstall_failed", "error": str(exc), "code": "MCP_INTERNAL"},
        )
    await ctx.sink.send_response(resp)

async def _run_mcp_connect_flow(
    services, name: str, *, rollback_on_probe_failure: bool = True
) -> dict[str, Any]:
    """Run the shared connect flow and return a frontend payload."""
    from jiuwenswarm.server.runtime.mcp.registry import (
        connect_mcp,
        was_connect_cancelled,
    )

    item = await asyncio.to_thread(connect_mcp, name)
    if was_connect_cancelled(name):
        # User cancelled while connect_mcp was still running (slow CLI
        # install / auth step); cancel_connect already killed the pending
        # auth proc and rolled back any connecting record.
        logger.info("[mcp] connect '%s' cancelled by user", name)
        return {"type": "cancelled", "name": name}
    if isinstance(item, dict) and item.get("auth_required"):
        return {"type": "auth_required", **_mask_sensitive_fields(item)}
    if isinstance(item, dict) and item.get("credentials_required"):
        # Required-token fields are metadata, not secrets.
        return {"type": "credentials_required", **item}
    # Confirm server-bearing MCPs are actually usable before connected.
    probe_ok, _probe_reason = await services.agent_manager.probe_mcp_live_connection(name)
    if not probe_ok:
        if rollback_on_probe_failure:
            try:
                from jiuwenswarm.server.runtime.mcp.registry import (
                    rollback_failed_connect,
                )
                rollback_failed_connect(name)
            except Exception as rollback_exc:  # noqa: BLE001
                logger.warning(
                    "[mcp] rollback failed-probe entry '%s' failed: %s",
                    name, rollback_exc,
                )
        return {
            "type": "connect_failed",
            "error": "MCP live-connect probe failed",
            "code": "MCP_UNREACHABLE",
            "name": name,
        }
    # Promote connecting state and sync skill-only credentials.
    try:
        from jiuwenswarm.server.runtime.mcp.state_store import (
            set_mcp_state,
        )
        set_mcp_state(name, state="connected")
    except Exception as flip_exc:  # noqa: BLE001
        logger.warning(
            "[mcp] flip connecting→connected after connect failed for '%s': %s",
            name, flip_exc,
        )
    try:
        services.agent_manager.sync_mcp_credentials()
    except Exception as sync_exc:  # noqa: BLE001
        logger.warning("[mcp] sync_mcp_credentials after connect failed: %s", sync_exc)
    return {
        "type": "connected",
        "name": name,
        "applied": True,
        "item": _mask_sensitive_fields(item),
    }

async def handle_mcp_connect(ctx: RequestContext) -> None:
    """Handle ``mcp.connect``; CLI OAuth may return ``auth_required``."""
    request = ctx.request
    services = ctx.services
    from jiuwenswarm.server.runtime.mcp.registry import CliConnectError
    try:
        params = request.params or {}
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("mcp name is required")
        payload = await _run_mcp_connect_flow(services, name)
        if payload.get("type") == "connect_failed":
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload=payload,
            )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload,
            )
    except KeyError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "connect_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
        )
    except CliConnectError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "type": "connect_failed",
                "error": str(exc),
                "code": exc.code,
                "runtime": exc.runtime,
                "install_cmd": exc.install_cmd,
                "name": name,
            },
        )
    except ValueError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] mcp.connect failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
        )
    await ctx.sink.send_response(resp)

async def handle_mcp_cancel_connect(ctx: RequestContext) -> None:
    """Handle ``mcp.cancel_connect``: abort an in-flight connect/auth flow.

    The user may mis-click or want to redo OAuth while a CLI MCP's
    ``mcp.wait_auth`` (or a slow ``mcp.connect``) is still holding the RPC
    open (up to 10 min). This marks the name cancelled so the poller /
    connect flow unwinds with a ``cancelled`` result, kills any pending
    authWaitForExit CLI proc, and rolls back the connecting state.json
    record. Idempotent — safe even when nothing is in flight. Because each
    incoming RPC is dispatched in its own task (the ws receive loop uses
    create_task), this runs concurrently with the hold-open wait_auth.
    """
    request = ctx.request
    from jiuwenswarm.server.runtime.mcp.registry import cancel_connect
    try:
        params = request.params or {}
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("mcp name is required")
        payload = await asyncio.to_thread(cancel_connect, name)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
    except ValueError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] mcp.cancel_connect failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
        )
    await ctx.sink.send_response(resp)

async def handle_mcp_wait_auth(ctx: RequestContext) -> None:
    """Handle ``mcp.wait_auth`` RPC: hold-open poll for CLI OAuth completion.

    After ``mcp.connect`` returned ``auth_required`` (the frontend opened
    the browser when ``auth_url`` was non-empty), the frontend sends this
    RPC which polls :func:`complete_cli_auth` (via :meth:`_await_cli_auth`)
    until the user finishes OAuth in the browser, then returns the single
    ``connected``/``auth_failed`` response. Holds the RPC open for minutes;
    the frontend shows a "connecting…" spinner while waiting.
    """
    request = ctx.request
    services = ctx.services
    from jiuwenswarm.server.runtime.mcp.registry import CliConnectError
    try:
        params = request.params or {}
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("mcp name is required")
        step_index = int(params.get("step_index", 0) or 0)
        result = await _await_cli_auth(services, name, step_index)
        if result.get("type") == "auth_failed":
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"type": "connect_failed", "name": name,
                         "error": str(result.get("error", "")), "code": "MCP_AUTH_FAILED"},
            )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=result,
            )
    except CliConnectError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "type": "connect_failed",
                "error": str(exc),
                "code": exc.code,
                "runtime": exc.runtime,
                "install_cmd": exc.install_cmd,
                "name": name,
            },
        )
    except ValueError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] mcp.wait_auth failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
        )
    await ctx.sink.send_response(resp)

async def handle_mcp_disconnect(ctx: RequestContext) -> None:
    """Handle ``mcp.disconnect`` RPC: remove from config + reload."""
    request = ctx.request
    services = ctx.services
    try:
        from jiuwenswarm.server.runtime.mcp.registry import disconnect_mcp
        params = request.params or {}
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("mcp name is required")
        removed = await asyncio.to_thread(disconnect_mcp, name)
        applied = True
        error_message = ""
        try:
            # Phase-2: targeted unregister (no full reload). SSE/HTTP MCP
            # removal can raise "Attempted to exit cancel scope in a
            # different task" (openjiuwen closes the client TaskGroup from
            # the wrong task) — that propagates as CancelledError and tears
            # down this handler before it can reply. Shield + timeout so the
            # state.json change (already done above) still lands and the
            # frontend gets a response; the MCP server process is torn down
            # by the agent's next reload even if this call fails.
            await asyncio.wait_for(
                asyncio.shield(services.agent_manager.apply_mcp_change(name, "remove")),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            applied = False
            error_message = "MCP unregister timed out (state updated; server will clear on next reload)"
            logger.warning("[mcp] apply_mcp_change after disconnect timed out for '%s'", name)
        except asyncio.CancelledError:
            # apply_mcp_change runs inside asyncio.shield, so an inner
            # SSE cancel-scope error cannot surface here as a
            # CancelledError (shield absorbs it). The only way this
            # except fires is the *outer* request being cancelled (ws
            # disconnect) — propagate it so the request unwinds cleanly
            # instead of dragging through clear/refresh and sending a
            # response to a dead socket.
            logger.warning(
                "[mcp] apply_mcp_change after disconnect CancelledError for '%s'", name
            )
            raise
        except Exception as reload_exc:  # noqa: BLE001
            applied = False
            error_message = str(reload_exc)
            logger.warning("[mcp] apply_mcp_change after disconnect failed: %s", reload_exc)
        # Clear this MCP's token env vars from the agent process so a
        # disconnected skill-only MCP's token doesn't linger.
        try:
            services.agent_manager.clear_mcp_credentials(name)
        except Exception as clear_exc:  # noqa: BLE001
            logger.warning("[mcp] clear_mcp_credentials after disconnect failed: %s", clear_exc)
        # disconnect uninstalled the bundled skills + unregistered the dir;
        # reload SkillUseRail so the agent stops seeing those skills.
        try:
            await services.agent_manager.refresh_skill_rails()
        except Exception as skill_exc:  # noqa: BLE001
            logger.warning("[mcp] refresh_skill_rails after disconnect failed: %s", skill_exc)
        payload = {
            "type": "disconnected",
            "name": name,
            "applied": applied,
            "item": _mask_sensitive_fields(removed),
        }
        if error_message:
            payload["error"] = error_message
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
    except KeyError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "disconnect_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
        )
    except ValueError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] mcp.disconnect failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
        )
    await ctx.sink.send_response(resp)

async def _finalize_cli_auth(services, name: str, item: dict[str, Any]) -> dict[str, Any]:
    """Run the post-auth side-effects for a CLI MCP connect.

    Auth is done; skills were installed during registry ``_finalize_cli``.
    Syncs MCP tokens into os.environ so bundled skill scripts (BashTool
    inherits os.environ) see their token env vars, then promotes
    connecting → connected. The MCP is NOT loaded into the agent here —
    session-level enable is driven by the ``mcp`` field on chat.send (see
    reconcile_session_mcp). Returns a payload dict for the push signal.
    """
    try:
        from jiuwenswarm.server.runtime.mcp.state_store import (
            set_mcp_state,
        )
        set_mcp_state(name, state="connected")
    except Exception as flip_exc:  # noqa: BLE001
        logger.warning(
            "[mcp] flip connecting→connected after CLI auth failed for '%s': %s",
            name, flip_exc,
        )
    try:
        services.agent_manager.sync_mcp_credentials()
    except Exception as sync_exc:  # noqa: BLE001
        logger.warning("[mcp] sync_mcp_credentials after CLI auth failed: %s", sync_exc)
    return {
        "type": "connected",
        "name": name,
        "applied": True,
        "item": _mask_sensitive_fields(item),
    }

async def _await_cli_auth(
    services, name: str, step_index: int,
    *, max_attempts: int = 200, delay: float = 3.0,
) -> dict[str, Any]:
    """Poll a CLI MCP's auth status until authenticated, returning the result.

    Called inline by :meth:`_handle_mcp_connect` after ``connect_mcp``
    returns an ``auth_required`` sentinel (the CLI process already opened
    the browser). This holds the RPC open — the frontend shows a
    "connecting…" spinner — and loops :func:`complete_cli_auth` until the
    user finishes OAuth, then runs the post-auth side-effects and returns
    a ``connected`` payload (or ``auth_failed`` on timeout/error) for the
    handler to send as the single RPC response. No push channel needed.

    Multi-step CLIs (e.g. feishu: config init → auth login): when a step
    completes and the next needs user action, ``complete_cli_auth`` returns
    ``auth_required=True`` with the new ``step_index``; we adopt it as
    ``cur_step`` so the next poll queries the new step — otherwise the
    poller would re-query the old step forever (a real dead-loop on
    multi-step CLIs).

    Only CLI MCPs reach here (form A/B/D return ``auth_required=False`` or
    ``credentials_required`` from :func:`connect_mcp`; only
    ``itype == "cli"`` calls ``_connect_cli`` which can return the
    ``auth_required`` sentinel). No form A/B/D path triggers this loop.
    """
    from jiuwenswarm.server.runtime.mcp.registry import (
        CliConnectError,
        complete_cli_auth,
        was_connect_cancelled,
    )

    cur_step = max(0, int(step_index))
    last_output = ""
    try:
        for attempt in range(max_attempts):
            if was_connect_cancelled(name):
                # User clicked cancel on the auth modal: unwind the
                # hold-open RPC with a cancelled result instead of polling
                # until the 10-min timeout. cancel_connect already killed
                # the pending auth proc and rolled back any connecting
                # record.
                logger.info("[mcp] _await_cli_auth '%s' cancelled by user", name)
                return {"type": "cancelled", "name": name}
            item = await asyncio.to_thread(complete_cli_auth, name, cur_step)
            if not isinstance(item, dict):
                raise ValueError(f"complete_cli_auth returned non-dict: {item!r}")
            if item.get("auth_required"):
                # Still pending — either the user hasn't finished in the
                # browser, or a step just completed and the next step needs
                # action. Adopt the authoritative step_index so we don't
                # loop on a stale step (dead-loop guard).
                new_step = item.get("step_index")
                if isinstance(new_step, (int, float)):
                    cur_step = max(cur_step, int(new_step))
                last_output = str(item.get("output") or item.get("matched") or "")[:300]
                logger.debug(
                    "[mcp] _await_cli_auth '%s' still pending (attempt %d/%d, step %d): %s",
                    name, attempt + 1, max_attempts, cur_step, last_output[:200],
                )
                await asyncio.sleep(delay)
                continue
            # Authenticated — re-check the cancel flag: the user may have
            # cancelled between this poll and the auth completing, or the
            # auth proc finished at the same moment the cancel landed.
            if was_connect_cancelled(name):
                logger.info(
                    "[mcp] _await_cli_auth '%s' cancelled after auth completed", name,
                )
                return {"type": "cancelled", "name": name}
            # Finalize and return the connected payload.
            return await _finalize_cli_auth(services, name, item)
        # Exhausted retries (~10 min) — return a failure so the handler can
        # surface it. Include the last status output so a misaligned
        # statusMatch (e.g. dingtalk's CLI status JSON not matching
        # cli.json's statusMatch keys) is diagnosable from the error.
        logger.warning(
            "[mcp] _await_cli_auth '%s' timed out after %d attempts (~%.0f min). "
            "last status output: %s",
            name, max_attempts, max_attempts * delay / 60, last_output,
        )
        timeout_error = "Authorization timed out. Retry connect."
        if last_output:
            timeout_error = f"{timeout_error} (last status: {last_output})"
        return {"type": "auth_failed", "name": name, "error": timeout_error}
    except CliConnectError:
        # Re-raise so _handle_mcp_wait_auth's except CliConnectError can
        # surface the structured code; otherwise the generic except below
        # flattens it into an auth_failed string, dropping code/runtime.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] _await_cli_auth '%s' failed: %s", name, exc)
        return {"type": "auth_failed", "name": name, "error": str(exc)}

async def handle_mcp_register_custom(ctx: RequestContext) -> None:
    """Handle ``mcp.register_custom`` RPC: write a user-defined MCP server config.

    Only persists the definition to state.json (state=registered) and returns
    immediately; the frontend follows up with ``mcp.connect`` to actually
    activate it. Editing an already-connected custom MCP removes the old live
    instance so the new config takes effect on connect.
    """
    request = ctx.request
    services = ctx.services
    from jiuwenswarm.server.runtime.mcp.registry import McpRegistryError
    try:
        from jiuwenswarm.server.runtime.mcp.registry import register_custom_mcp
        params = request.params or {}
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("mcp name is required")
        config = {k: v for k, v in params.items() if k != "name"}
        entry = await asyncio.to_thread(register_custom_mcp, name, config)
        was_connected = bool(entry.pop("was_connected", False))
        # Edit of a connected instance: remove the old live server so the
        # new config (just persisted as state=registered) takes effect on
        # the frontend's follow-up mcp.connect. Failure here is non-fatal —
        # the old instance also drops on the next agent reload.
        if was_connected:
            try:
                await services.agent_manager.apply_mcp_change(name, "remove")
            except Exception as reload_exc:  # noqa: BLE001
                logger.warning(
                    "[mcp] remove old instance after register_custom '%s' failed: %s",
                    name, reload_exc,
                )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "type": "registered",
                "name": name,
                "item": _mask_sensitive_fields(entry),
            },
        )
    except McpRegistryError as exc:
        # Classifiable registry failure (e.g. name collides with a builtin
        # package). Surface code so the frontend shows an actionable hint
        # ("name taken by a builtin, rename") instead of a generic bad_request.
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "bad_request", "error": str(exc), "code": exc.code, "name": name},
        )
    except ValueError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] mcp.register_custom failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
        )
    await ctx.sink.send_response(resp)

async def handle_mcp_delete_custom(ctx: RequestContext) -> None:
    """Handle ``mcp.delete_custom``: terminal teardown of a custom MCP."""
    request = ctx.request
    services = ctx.services
    try:
        from jiuwenswarm.server.runtime.mcp.registry import delete_custom_mcp
        params = request.params or {}
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("mcp name is required")
        # Clear env vars first — delete_custom_mcp wipes the credential
        # file + state record, and after that _mcp_env_keys returns [] so
        # the token would leak in os.environ. Clearing BEFORE the wipe
        # means the CredentialStore still has the keys to read.
        try:
            services.agent_manager.clear_mcp_credentials(name)
        except Exception as clear_exc:  # noqa: BLE001
            logger.warning("[mcp] clear_mcp_credentials before delete failed: %s", clear_exc)
        removed = await asyncio.to_thread(delete_custom_mcp, name)
        was_connected = bool(removed.get("was_connected", False))
        applied = True
        error_message = ""
        if was_connected:
            try:
                await asyncio.wait_for(
                    asyncio.shield(services.agent_manager.apply_mcp_change(name, "remove")),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                applied = False
                error_message = "MCP unregister timed out (state deleted; server will clear on next reload)"
                logger.warning("[mcp] apply_mcp_change after delete timed out for '%s'", name)
            except asyncio.CancelledError:
                logger.warning("[mcp] apply_mcp_change after delete CancelledError for '%s'", name)
                raise  # outer ws-disconnect — don't reply to a dead socket
            except Exception as reload_exc:  # noqa: BLE001
                applied = False
                error_message = str(reload_exc)
                logger.warning("[mcp] apply_mcp_change after delete failed: %s", reload_exc)
        try:
            await services.agent_manager.refresh_skill_rails()
        except Exception as skill_exc:  # noqa: BLE001
            logger.warning("[mcp] refresh_skill_rails after delete failed: %s", skill_exc)
        payload = {
            "type": "deleted",
            "name": name,
            "applied": applied,
            "item": _mask_sensitive_fields(removed),
        }
        if error_message:
            payload["error"] = error_message
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
    except KeyError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "delete_failed", "error": str(exc), "code": "MCP_NOT_FOUND"},
        )
    except ValueError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] mcp.delete_custom failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
        )
    await ctx.sink.send_response(resp)

async def handle_mcp_save_credentials(ctx: RequestContext) -> None:
    """Handle ``mcp.save_credentials`` RPC: persist user-supplied tokens.

    Form B connectors (tianyancha/gildata/gmail/jira...) declare ``${VAR}``
    placeholders in mcp.json; the user fills the values via a frontend
    prompt, which calls this RPC. Tokens are stored in the local
    CredentialStore (never returned in the response). Does NOT reload —
    the caller follows up with ``mcp.connect`` to actually register.
    """
    request = ctx.request
    try:
        from jiuwenswarm.server.runtime.mcp.registry import save_mcp_credentials
        params = request.params or {}
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("mcp name is required")
        tokens = params.get("tokens") or {}
        if not isinstance(tokens, dict) or not tokens:
            raise ValueError("tokens (non-empty dict) is required")
        result = await asyncio.to_thread(save_mcp_credentials, name, tokens)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"type": "credentials_saved", "name": name, "saved_keys": result.get("saved_keys", [])},
        )
    except ValueError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "bad_request", "error": str(exc), "code": "MCP_BAD_REQUEST"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] mcp.save_credentials failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"type": "internal_error", "error": str(exc), "code": "MCP_INTERNAL"},
        )
    await ctx.sink.send_response(resp)
