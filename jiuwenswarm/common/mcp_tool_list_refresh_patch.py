# coding: utf-8
"""Fix enterprise MCP tool-list refresh on openjiuwen ResourceMgr / ToolMgr.

Stock openjiuwen issues that block mid-session discovery of new remote tools:

1. ``ToolMgr._inner_refresh_mcp_tools`` adds tools without removing old ones,
   so re-list can raise ``already exist tool``.
2. ``ResourceMgr.refresh_mcp_server`` is a stub (``return []``).
3. ``get_mcp_tool_infos`` calls ``refresh_tool_server`` but never syncs new
   cards into ``_id_to_card``, so AbilityManager still exposes the freeze
   from the first ``add_mcp_server``.

This patch is applied once at process startup (from
``JiuWenSwarmDeepAdapter.__init__``), alongside ``apply_mcp_call_timeout_patch``.
Idempotent via ``_PATCHED``.
"""
from __future__ import annotations

import logging
import os
import time
from copy import deepcopy
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PATCHED = False

# Default TTL (seconds) when config / env does not set tool_list_ttl_s.
DEFAULT_MCP_TOOL_LIST_TTL_S = 60.0


def resolve_mcp_tool_list_ttl_s(config: dict[str, Any] | None = None) -> float:
    """Return tool-list TTL in seconds.

    Priority: ``MCP_TOOL_LIST_TTL_S`` env → ``config["mcp"]["tool_list_ttl_s"]``
    → default 60. ``0`` means "every user turn" (turn hook force-refreshes;
    ``add_mcp_server`` still gets no expiry because SDK rejects ``<= 0``).
    """
    raw_env = str(os.environ.get("MCP_TOOL_LIST_TTL_S", "") or "").strip()
    if raw_env:
        try:
            return max(0.0, float(raw_env))
        except ValueError as exc:
            logger.debug(
                "[mcp-tool-refresh] ignore invalid MCP_TOOL_LIST_TTL_S=%r: %s",
                raw_env,
                exc,
            )
    if isinstance(config, dict):
        mcp = config.get("mcp")
        if isinstance(mcp, dict) and "tool_list_ttl_s" in mcp:
            try:
                return max(0.0, float(mcp.get("tool_list_ttl_s")))
            except (TypeError, ValueError) as exc:
                logger.debug(
                    "[mcp-tool-refresh] ignore invalid mcp.tool_list_ttl_s=%r: %s",
                    mcp.get("tool_list_ttl_s"),
                    exc,
                )
    return DEFAULT_MCP_TOOL_LIST_TTL_S


def _sync_id_to_card(
    resource_mgr: Any,
    *,
    old_tool_ids: list[str],
    new_cards: list[Any],
    tag: Any = None,
) -> None:
    """Replace ResourceMgr tool cards for one MCP server after list_tools."""
    id_to_card_attr = "_id_to_card"
    id_to_card = getattr(resource_mgr, id_to_card_attr, None)
    if not isinstance(id_to_card, dict):
        return
    tag_mgr_attr = "_tag_mgr"
    tag_mgr = getattr(resource_mgr, tag_mgr_attr, None)
    for tool_id in old_tool_ids or []:
        id_to_card.pop(tool_id, None)
        if tag_mgr is not None:
            try:
                tag_mgr.remove_resource(tool_id)
            except Exception as exc:  # noqa: BLE001 — best-effort tag cleanup
                logger.debug(
                    "[mcp-tool-refresh] tag_mgr.remove_resource failed id=%s: %s",
                    tool_id,
                    exc,
                )
    for card in new_cards or []:
        card_id = getattr(card, "id", None)
        if not card_id:
            continue
        id_to_card[card_id] = card
        if tag_mgr is not None and tag is not None:
            try:
                tag_mgr.tag_resource(card_id, tag)
            except Exception as exc:  # noqa: BLE001 — best-effort tag attach
                logger.debug(
                    "[mcp-tool-refresh] tag_mgr.tag_resource failed id=%s: %s",
                    card_id,
                    exc,
                )


def apply_mcp_tool_list_refresh_patch() -> None:
    """Monkeypatch ToolMgr / ResourceMgr for MCP tool-list refresh. Idempotent."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    from openjiuwen.core.common.exception.codes import StatusCode
    from openjiuwen.core.common.exception.errors import build_error
    from openjiuwen.core.common.logging import LogEventType
    from openjiuwen.core.common.logging import runner_logger as oj_logger
    from openjiuwen.core.foundation.tool import MCPTool
    from openjiuwen.core.runner.resources_manager.base import (
        GLOBAL,
        Error,
        Ok,
        TagMatchStrategy,
    )
    from openjiuwen.core.runner.resources_manager.resource_manager import ResourceMgr
    from openjiuwen.core.runner.resources_manager.tool_manager import (
        McpServerResource,
        ToolMgr,
    )

    # ---- ToolMgr: list_tools first, then replace local tools -----------------
    _orig_inner = ToolMgr._inner_refresh_mcp_tools  # noqa: SLF001 — monkeypatch target

    async def _inner_refresh_mcp_tools_safe(self, client, server_config, expiry_time):
        # List remote tools *before* removing locals so a failed list_tools
        # leaves the previous snapshot intact.
        existing = self._mcp_server_resources.get(server_config.server_id)
        old_ids = list(existing.tool_ids) if existing is not None and existing.tool_ids else []
        mcp_cards = await client.list_tools()
        mcp_cards = mcp_cards if mcp_cards else []
        if old_ids:
            self._inner_remove_mcp_tools(old_ids)
        for card in mcp_cards:
            card.id = self.generate_mcp_tool_id(
                server_config.server_id, server_config.server_name, card.name
            )
            # Idempotent overwrite if a race left a stale entry.
            self._tools[card.id] = MCPTool(mcp_client=client, tool_info=deepcopy(card))
        mcp_ids = [card.id for card in mcp_cards]
        self._mcp_server_resources[server_config.server_id] = McpServerResource(
            config=server_config,
            expiry_time=expiry_time,
            client=client,
            last_update_time=time.time(),
            tool_ids=deepcopy(mcp_ids),
        )
        return mcp_cards

    setattr(ToolMgr, "_inner_refresh_mcp_tools_unpatched", _orig_inner)
    setattr(ToolMgr, "_inner_refresh_mcp_tools", _inner_refresh_mcp_tools_safe)

    # ---- ToolMgr.refresh_tool_server: None = skipped (not empty refresh) -----
    async def _refresh_tool_server_safe(
        self,
        server_id: str,
        skip_not_exist: bool = False,
        force: bool = False,
    ):
        """Like stock, but return None when TTL says skip (vs [] after empty list)."""
        mcp_resource = self._mcp_server_resources.get(server_id)
        if not mcp_resource:
            if not skip_not_exist:
                raise build_error(
                    StatusCode.RESOURCE_MCP_SERVER_REFRESH_ERROR,
                    server_id=server_id,
                    reason="server is not exist",
                )
            return None
        need_refresh = force
        if not force:
            expiry = mcp_resource.expiry_time
            if expiry and time.time() - mcp_resource.last_update_time >= expiry:
                need_refresh = True
        if not need_refresh:
            return None
        return await self._inner_refresh_mcp_tools(
            mcp_resource.client, mcp_resource.config, mcp_resource.expiry_time
        )

    setattr(ToolMgr, "refresh_tool_server", _refresh_tool_server_safe)

    # ---- ResourceMgr.refresh_mcp_server: real implementation -----------------
    async def _refresh_mcp_server(
        self,
        server_id: Optional[str | list[str]] = None,
        *,
        server_name: Optional[str | list[str]] = None,
        tag=None,
        tag_match_strategy: TagMatchStrategy = TagMatchStrategy.ALL,
        ignore_exception: bool = False,
        skip_if_tag_not_exists: bool = False,
        force: bool = True,
    ):
        server_ids, _exact = self._inner_get_server_ids(
            server_id,
            server_name,
            tag,
            tag_match_strategy,
            skip_if_tag_not_exists,
            StatusCode.RESOURCE_MCP_SERVER_REFRESH_ERROR,
        )
        results = []
        tool_mgr = self._resource_registry.tool()
        for mcp_server_id in server_ids:
            try:
                old_ids = list(tool_mgr.get_mcp_tool_ids(mcp_server_id) or [])
                cards = await tool_mgr.refresh_tool_server(
                    mcp_server_id, skip_not_exist=True, force=force
                )
                # None → TTL skip (leave _id_to_card alone).
                # list (possibly empty) → refresh ran; always rewrite cards.
                if cards is not None:
                    _sync_id_to_card(
                        self,
                        old_tool_ids=old_ids,
                        new_cards=list(cards),
                        tag=tag if tag else GLOBAL,
                    )
                    oj_logger.info(
                        "refresh mcp server succeed",
                        event_type=LogEventType.RESOURCE_MGR_ADD_RESOURCE_SERVER,
                        resource_id=mcp_server_id,
                        resource_type="mcp server",
                        metadata={
                            "tools": [getattr(c, "name", "") for c in cards],
                            "force": force,
                        },
                    )
                results.append(Ok(mcp_server_id))
            except Exception as exc:  # noqa: BLE001
                oj_logger.error(
                    "refresh mcp server failed",
                    event_type=LogEventType.RESOURCE_MGR_ADD_RESOURCE_SERVER,
                    resource_id=mcp_server_id,
                    resource_type="mcp server",
                    exception=exc,
                )
                if not ignore_exception:
                    raise
                results.append(Error(exc))
        return results

    setattr(ResourceMgr, "refresh_mcp_server", _refresh_mcp_server)

    # ---- get_mcp_tool_infos: sync _id_to_card when expiry refresh fires -------
    _orig_get_infos = ResourceMgr.get_mcp_tool_infos

    async def _get_mcp_tool_infos(self, name=None, server_id=None, **kwargs):
        ignore_exception = bool(kwargs.get("ignore_exception", False))
        server_ids, exact_match = self._inner_get_server_ids(
            server_id,
            kwargs.get("server_name"),
            kwargs.get("tag"),
            kwargs.get("tag_match_strategy", TagMatchStrategy.ALL),
            kwargs.get("skip_if_tag_not_exists", False),
            StatusCode.RESOURCE_MCP_TOOL_GET_ERROR,
        )
        tool_names = [name] if isinstance(name, str) else name
        tool_mgr = self._resource_registry.tool()
        results = []
        for mcp_server_id in server_ids:
            try:
                old_ids = list(tool_mgr.get_mcp_tool_ids(mcp_server_id) or [])
                cards = await tool_mgr.refresh_tool_server(
                    mcp_server_id, skip_not_exist=True, force=False
                )
                if cards is not None:
                    _sync_id_to_card(
                        self,
                        old_tool_ids=old_ids,
                        new_cards=list(cards),
                        tag=kwargs.get("tag") or GLOBAL,
                    )
            except Exception as exc:  # noqa: BLE001
                if not ignore_exception:
                    raise
                logger.debug(
                    "[mcp-tool-refresh] get_mcp_tool_infos refresh skipped: %s",
                    exc,
                )
            tool_ids: list[str] = []
            if tool_names is None:
                server_tool_ids = tool_mgr.get_mcp_tool_id(mcp_server_id)
                if server_tool_ids:
                    tool_ids = list(server_tool_ids)
            else:
                for tool_name in tool_names:
                    tool_id = tool_mgr.get_mcp_tool_id(mcp_server_id, tool_name)
                    if tool_id:
                        tool_ids.append(tool_id)
            for tool_id in tool_ids:
                tool_card = self._id_to_card.get(tool_id) if tool_id else None
                if exact_match:
                    results.append(tool_card.tool_info() if tool_card else None)
                elif tool_card:
                    results.append(tool_card.tool_info())
        return results

    setattr(ResourceMgr, "get_mcp_tool_infos_unpatched", _orig_get_infos)
    setattr(ResourceMgr, "get_mcp_tool_infos", _get_mcp_tool_infos)

    logger.info("[mcp-tool-refresh] patch applied (ToolMgr + ResourceMgr)")


async def refresh_registered_mcp_tool_lists(
    *,
    server_ids: list[str],
    ttl_s: float,
    force: bool | None = None,
) -> bool:
    """Refresh tool lists for registered MCP servers when TTL elapsed.

    Returns True if any server's tool-id set changed (caller should invalidate
    progressive-tool-rail cache).
    """
    from openjiuwen.core.runner import Runner

    if not server_ids:
        return False

    registry_attr = "_resource_registry"
    resources_attr = "_mcp_server_resources"
    tool_mgr = getattr(Runner.resource_mgr, registry_attr).tool()
    changed = False
    now = time.time()
    # force=None → auto: always when ttl_s==0, else only when stale
    for server_id in server_ids:
        resource = getattr(tool_mgr, resources_attr).get(server_id)
        if resource is None:
            continue
        do_force = True if force is True else False if force is False else (ttl_s <= 0)
        if not do_force:
            last = float(resource.last_update_time or 0)
            expiry = resource.expiry_time
            # Prefer explicit expiry on the resource; fall back to ttl_s.
            window = float(expiry) if expiry else float(ttl_s)
            if window > 0 and (now - last) < window:
                continue
            do_force = True

        old_ids = set(tool_mgr.get_mcp_tool_ids(server_id) or [])
        try:
            await Runner.resource_mgr.refresh_mcp_server(
                server_id=server_id, force=do_force, ignore_exception=True
            )
        except TypeError:
            # Older patched signature without force kw — call force path via tool_mgr
            cards = await tool_mgr.refresh_tool_server(
                server_id, skip_not_exist=True, force=True
            )
            if cards is not None:
                _sync_id_to_card(
                    Runner.resource_mgr,
                    old_tool_ids=list(old_ids),
                    new_cards=list(cards),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[mcp-tool-refresh] refresh failed server_id=%s: %s",
                server_id,
                exc,
            )
            continue
        new_ids = set(tool_mgr.get_mcp_tool_ids(server_id) or [])
        if new_ids != old_ids:
            changed = True
            logger.info(
                "[mcp-tool-refresh] tools changed server_id=%s old=%s new=%s",
                server_id,
                sorted(old_ids),
                sorted(new_ids),
            )
    return changed


__all__ = [
    "DEFAULT_MCP_TOOL_LIST_TTL_S",
    "apply_mcp_tool_list_refresh_patch",
    "refresh_registered_mcp_tool_lists",
    "resolve_mcp_tool_list_ttl_s",
]
