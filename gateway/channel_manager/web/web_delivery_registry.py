# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""HTTP request-scoped outbound registry (not WebSocket connection table)."""

from __future__ import annotations

import inspect
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class WebDeliveryRegistry:
    """Tracks ``HttpJsonOutbound`` / ``HttpSseOutbound`` for routing Agent replies."""

    def __init__(self) -> None:
        self._request_outbounds: dict[str, Any] = {}
        self._session_request_outbounds: dict[str, set[str]] = {}

    @property
    def request_outbounds(self) -> dict[str, Any]:
        return self._request_outbounds

    @property
    def session_request_outbounds(self) -> dict[str, set[str]]:
        return self._session_request_outbounds

    def register(self, outbound: Any) -> str:
        """Register a request-scoped HTTP Outbound (no ``register_ws``)."""
        oid = str(
            getattr(outbound, "outbound_id", None)
            or getattr(outbound, "_jiuwen_ws_id", None)
            or uuid.uuid4().hex
        ).strip()
        setattr(outbound, "outbound_id", oid)
        setattr(outbound, "_jiuwen_ws_id", oid)
        self._request_outbounds[oid] = outbound
        sid = str(getattr(outbound, "session_id", "") or "").strip()
        if sid:
            bucket = self._session_request_outbounds.setdefault(sid, set())
            bucket.add(oid)
        return oid

    async def unregister(self, outbound: Any) -> None:
        """Remove request Outbound from routing tables and close it."""
        oid = str(
            getattr(outbound, "outbound_id", None)
            or getattr(outbound, "_jiuwen_ws_id", None)
            or "",
        ).strip()
        if oid:
            self._request_outbounds.pop(oid, None)
        sid = str(getattr(outbound, "session_id", "") or "").strip()
        if sid and oid:
            bucket = self._session_request_outbounds.get(sid)
            if bucket is not None:
                bucket.discard(oid)
                if not bucket:
                    self._session_request_outbounds.pop(sid, None)
        close = getattr(outbound, "close", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.debug("[WebDelivery] outbound close failed", exc_info=True)

    async def clear(self) -> None:
        """Close and drop all HTTP request Outbounds (channel stop / shutdown)."""
        outs = list(self._request_outbounds.values())
        self._request_outbounds.clear()
        self._session_request_outbounds.clear()
        for outbound in outs:
            close = getattr(outbound, "close", None)
            if not callable(close):
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.debug("[WebDelivery] outbound clear close failed", exc_info=True)

    def lookup(self, outbound_id: str) -> Any | None:
        """Return a live HTTP outbound by id, or None."""
        oid = str(outbound_id or "").strip()
        if not oid:
            return None
        out = self._request_outbounds.get(oid)
        if out is None:
            return None
        if getattr(out, "closed", False):
            self._prune_outbound(oid, out)
            return None
        return out

    def _prune_outbound(self, oid: str, outbound: Any) -> None:
        self._request_outbounds.pop(oid, None)
        sid = str(getattr(outbound, "session_id", "") or "").strip()
        if not sid:
            return
        bucket = self._session_request_outbounds.get(sid)
        if bucket is None:
            return
        bucket.discard(oid)
        if not bucket:
            self._session_request_outbounds.pop(sid, None)

    def peers_for_session(self, session_id: str) -> set[Any]:
        """HTTP outbounds bound to ``session_id`` (WS clients added by transport)."""
        peers: set[Any] = set()
        sid = str(session_id or "").strip()
        if not sid:
            return peers
        for oid in list(self._session_request_outbounds.get(sid, ())):
            out = self._request_outbounds.get(oid)
            if out is not None and not getattr(out, "closed", False):
                peers.add(out)
        return peers
