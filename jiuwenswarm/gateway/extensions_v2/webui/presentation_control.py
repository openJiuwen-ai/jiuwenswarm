# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit local starts of already published application presentations."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from jiuwenswarm.gateway.extensions_v2.webui.models import RenderTarget, valid_instance_key

START_EVENT = "webui.presentation.start"
RESULT_METHOD = "webui.presentation.result"
RESULT_CODES = frozenset({"STARTED", "DESKTOP_REQUIRED", "TARGET_INACTIVE", "BUSY", "OPEN_FAILED"})


@dataclass
class PendingStart:
    """One exact connection, activation and render revision awaiting a receipt."""

    target: RenderTarget
    revision: int
    activation: Any
    result: asyncio.Future


class PresentationControl:
    """Use existing registry and transport; never interpret plugin layouts."""

    def __init__(self, runtime: Any, *, timeout: float = 30.0) -> None:
        self.runtime = runtime
        self.timeout = timeout
        self.pending: dict[str, PendingStart] = {}

    def _current(self, pending: PendingStart) -> bool:
        record = self.runtime.registry.get(pending.target)
        return (
            self.runtime.current_activation(pending.target.package_id) is pending.activation
            and pending.activation is not None
            and record is not None and record.root is not None
            and record.root.component == "ApplicationSurface"
            and record.revision == pending.revision
            and self.runtime.channel.ws_by_id(pending.target.connection_id) is not None
        )

    def query(self, params: dict[str, Any]) -> dict[str, Any]:
        """List only current published candidates, without conversation data."""
        if set(params) - {"package_id"} or (
            "package_id" in params and (not isinstance(params["package_id"], str) or not params["package_id"])
        ):
            return _result("INVALID_PARAMS")
        candidates = []
        for record in self.runtime.registry.snapshot():
            target = record.target
            activation = self.runtime.current_activation(target.package_id)
            if (
                target.slot_id != "application.presentation" or target.resolved_scope != "application"
                or record.root is None or record.root.component != "ApplicationSurface"
                or activation is None or self.runtime.channel.ws_by_id(target.connection_id) is None
                or ("package_id" in params and target.package_id != params["package_id"])
            ):
                continue
            candidates.append({
                "connection_id": target.connection_id, "package_id": target.package_id,
                "instance_key": target.instance_key, "revision": record.revision,
                "title": dict(record.root.props["title"]) if isinstance(record.root.props["title"], Mapping)
                else record.root.props["title"],
            })
        return {"ok": True, "payload": {"status": "queried", "candidates": candidates}}

    async def start(self, params: dict[str, Any]) -> dict[str, Any]:
        """Start an exact candidate once and wait for the native ready receipt."""
        if (
            set(params) != {"connection_id", "package_id", "instance_key", "revision"}
            or any(not isinstance(params.get(key), str) or not params[key] or len(params[key].encode("utf-8")) > 200
                   for key in ("connection_id", "package_id"))
            or not valid_instance_key(params.get("instance_key"))
            or type(params.get("revision")) is not int or not 0 < params["revision"] <= 2**53 - 1
        ):
            return _result("INVALID_PARAMS")
        target = RenderTarget(
            session_id="", slot_id="application.presentation", package_id=params["package_id"],
            instance_key=params["instance_key"], scope="application", connection_id=params["connection_id"],
        )
        pending = PendingStart(target, params["revision"], self.runtime.current_activation(target.package_id),
                               asyncio.get_running_loop().create_future())
        if not self._current(pending):
            return _result("TARGET_INACTIVE")
        if any(item.target.connection_id == target.connection_id for item in self.pending.values()):
            return _result("BUSY")
        request_id = uuid4().hex
        self.pending[request_id] = pending
        try:
            delivered = await self.runtime.webui.transport.send_presentation_start(target.connection_id, {
                "request_id": request_id, "target": target.to_payload(), "revision": pending.revision,
                "deadline": int((time.time() + self.timeout) * 1000),
            })
            if not delivered:
                return _result("DELIVERY_FAILED", unknown=True)
            try:
                code = await asyncio.wait_for(asyncio.shield(pending.result), self.timeout)
            except TimeoutError:
                return _result("RECEIPT_TIMEOUT", unknown=True)
            if code == "UNKNOWN" or not self._current(pending):
                return _result("TARGET_CHANGED", unknown=True)
            return _result(code)
        finally:
            self.pending.pop(request_id, None)
            if not pending.result.done():
                pending.result.cancel()

    def complete(self, connection_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Accept a result only from the exact connection that received the request."""
        if (
            set(params) != {"request_id", "code"} or not isinstance(params.get("request_id"), str)
            or not isinstance(params.get("code"), str) or params["code"] not in RESULT_CODES
        ):
            return {"accepted": False, "code": "INVALID_PARAMS"}
        pending = self.pending.get(params["request_id"])
        if pending is None or pending.target.connection_id != connection_id or pending.result.done():
            return {"accepted": False, "code": "TARGET_INACTIVE"}
        pending.result.set_result(params["code"] if self._current(pending) else "UNKNOWN")
        return {"accepted": True}

    def invalidate(self, *, connection_id: str | None = None, package_id: str | None = None) -> None:
        """Release all matching waiters when their connection or package ends."""
        for pending in self.pending.values():
            if (
                (connection_id is None or pending.target.connection_id == connection_id)
                and (package_id is None or pending.target.package_id == package_id)
                and not pending.result.done()
            ):
                pending.result.set_result("UNKNOWN")


def _result(code: str, *, unknown: bool = False) -> dict[str, Any]:
    return {"ok": code == "STARTED", "payload": {
        "status": "started" if code == "STARTED" else "unknown" if unknown else "rejected", "code": code,
    }}
