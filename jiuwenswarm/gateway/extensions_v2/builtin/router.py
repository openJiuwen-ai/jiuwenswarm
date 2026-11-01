# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-local builtin methods used by the ``local`` route.

``local`` means "the Gateway answers this frame itself". The router owns a
small closed table of methods it can answer; an unknown method is rejected with
``ROUTE_UNSUPPORTED`` instead of being silently forwarded, so a package that
asked for a local answer learns that no builtin executor exists for it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from jiuwenswarm.gateway.extensions_v2.builtin.auth_gate import is_authenticated
from jiuwenswarm.gateway.extensions_v2.state import UiStateStore

CODE_ROUTE_UNSUPPORTED: Final[str] = "ROUTE_UNSUPPORTED"

#: Returns the authentication state of the calling connection.
METHOD_AUTH_STATUS: Final[str] = "auth.status"

LocalHandler = Callable[["LocalRequest"], Awaitable["LocalResponse"]]


def _empty_mapping() -> Mapping[str, Any]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class LocalRequest:
    """One frame the chain asked the Gateway to answer locally."""

    method: str
    params: Mapping[str, Any] = field(default_factory=_empty_mapping)
    connection_id: str = ""
    session_id: str | None = None
    user_id: str | None = None


@dataclass(frozen=True, slots=True)
class LocalResponse:
    """Builtin answer for one local frame."""

    ok: bool
    payload: Mapping[str, Any] = field(default_factory=_empty_mapping)
    error: Mapping[str, Any] = field(default_factory=_empty_mapping)


class GatewayLocalRouter:
    """Closed table of methods the Gateway can answer without AgentServer."""

    def __init__(self, store: UiStateStore) -> None:
        self._store = store
        self._methods: dict[str, LocalHandler] = {}
        self.register(METHOD_AUTH_STATUS, self._auth_status)

    def register(self, method: str, handler: LocalHandler) -> None:
        """Add one builtin method; the last registration wins."""

        name = method.strip() if isinstance(method, str) else ""
        if not name or not callable(handler):
            raise ValueError("a local method needs a name and a callable handler")
        self._methods[name] = handler

    def methods(self) -> frozenset[str]:
        """Return the registered local methods."""

        return frozenset(self._methods)

    async def execute(self, request: LocalRequest) -> LocalResponse:
        """Answer one local frame or reject an unknown method."""

        handler = self._methods.get(request.method)
        if handler is None:
            return LocalResponse(
                ok=False,
                error={
                    "code": CODE_ROUTE_UNSUPPORTED,
                    "message": f"no gateway builtin for method: {request.method}",
                },
            )
        return await handler(request)

    async def _auth_status(self, request: LocalRequest) -> LocalResponse:
        return LocalResponse(
            ok=True,
            payload={"authenticated": is_authenticated(self._store, request.connection_id)},
        )
