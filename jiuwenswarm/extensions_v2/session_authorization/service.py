# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-only credential binding; plugins receive scoped opaque handles."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import math
import secrets
import time
from typing import Literal

from openjiuwen.core.events import EventHandlerOps, ExtensionEventBus, PackageMetadata, SessionContext

from jiuwenswarm.extensions_v2.event_types.session_authorize import (
    SessionAuthorization, SessionAuthorizationError,
)
from jiuwenswarm.server.runtime.session.session_history_ops import require_session_id


_SUPPORTED_PROVIDERS = frozenset({"anthropic", "openai"})


@dataclass(frozen=True, slots=True)
class AuthorizationCaller:
    """Host-verified package activation, retaining metadata identity."""

    metadata: PackageMetadata
    generation: int

    @property
    def key(self) -> tuple:
        return self.metadata.package_id, self.generation, id(self.metadata)


@dataclass(frozen=True, slots=True)
class _Credential:
    provider: str
    kind: Literal["bearer", "api-key"]
    secret: str = field(repr=False)
    expires_at: float | None = None
    epoch: object = field(default_factory=object, repr=False)


@dataclass(frozen=True, slots=True)
class _Handle:
    caller: AuthorizationCaller
    session_id: str
    binding: _Credential = field(repr=False)
    expires_at: float


class SessionAuthorizationService:
    """Hold explicit first-party bindings without collecting or sending secrets.

    Args:
        bus: Worker-owned EventBus.
        caller_for_ops: Host callback verifying the current managed invocation.
        caller_is_live: Host callback checking the package activation generation.
        supported: Whether this build can verify active Ops invocations.
        clock: UTC epoch-seconds clock used for lazy expiration.
        handle_ttl: Maximum handle lifetime in seconds, default 300.
        max_handles: Bound on outstanding handles, default 1024.
    """

    def __init__(
        self, bus: ExtensionEventBus, *,
        caller_for_ops: Callable[[EventHandlerOps], AuthorizationCaller | None],
        caller_is_live: Callable[[AuthorizationCaller], bool],
        supported: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = time.time,
        handle_ttl: float = 300,
        max_handles: int = 1024,
    ) -> None:
        if type(handle_ttl) not in (int, float) or not math.isfinite(handle_ttl) or handle_ttl <= 0:
            raise ValueError("handle_ttl must be a positive finite duration")
        if type(max_handles) is not int or max_handles <= 0:
            raise ValueError("max_handles must be a positive integer")
        self.bus = bus
        self._caller_for_ops = caller_for_ops
        self._caller_is_live = caller_is_live
        self._supported = supported
        self._clock = clock
        self._handle_ttl = handle_ttl
        self._max_handles = max_handles
        self._credentials: dict[str, _Credential] = {}
        self._handles: dict[str, _Handle] = {}
        self._closed = False

    def bind(
        self, context: SessionContext, *, provider: str,
        credential: str, kind: Literal["bearer", "api-key"],
        expires_at: float | None = None,
    ) -> bool:
        """Bind a trusted host credential, replacing the previous session epoch.

        The host must call bind/unbind when its actual credential/session changes.
        No production collector or network request is installed by this service.
        Unsupported providers clear any older binding and return False.
        """
        self._require_open()
        session_id = require_session_id(context.session_id)
        if not isinstance(provider, str):
            raise TypeError("provider must be a string")
        provider_name = provider.strip().casefold()
        if provider_name not in _SUPPORTED_PROVIDERS:
            self.unbind(session_id)
            return False
        if kind not in ("bearer", "api-key"):
            raise ValueError("credential kind must be bearer or api-key")
        if not isinstance(credential, str) or not credential.strip() or len(credential) > 16384 or any(ord(char) < 32 or ord(char) == 127 for char in credential):
            raise ValueError("credential must be a non-empty header-safe string")
        if expires_at is not None and (type(expires_at) not in (int, float) or not math.isfinite(expires_at)):
            raise ValueError("expires_at must be finite UTC epoch seconds or None")
        self.unbind(session_id)
        if expires_at is not None and expires_at <= self._clock():
            return False
        self._credentials[session_id] = _Credential(provider_name, kind, credential, expires_at)
        return True

    def unbind(self, session_id: str) -> None:
        """Drop one host binding and all handles issued from its old epoch."""
        require_session_id(session_id)
        self._credentials.pop(session_id, None)
        for handle, value in tuple(self._handles.items()):
            if value.session_id == session_id:
                self._handles.pop(handle, None)

    def revoke_package(self, package_id: str, generation: int) -> None:
        """Revoke handles for the package activation being unloaded/reloaded."""
        for handle, value in tuple(self._handles.items()):
            if (value.caller.metadata.package_id, value.caller.generation) == (package_id, generation):
                self._handles.pop(handle, None)

    def revoke_handle(self, handle: str) -> None:
        self._handles.pop(handle, None)

    def prune(self) -> None:
        """Remove expired credentials and handles whose package generation ended."""
        self._prune()

    def close(self) -> None:
        """Invalidate all references and release in-memory credential bindings."""
        self._closed = True
        self._handles.clear()
        self._credentials.clear()

    async def authorize(self, ops: EventHandlerOps) -> SessionAuthorization | None:
        """Return a scoped handle through the same-name event, or None."""
        from jiuwenswarm.extensions_v2.session_authorization.dispatch import dispatch_authorization
        self._require_open()
        if not self._supported():
            return None
        caller = self._caller_for_ops(ops)
        if caller is None or ops.session_context is None or not self._caller_is_live(caller):
            raise SessionAuthorizationError("UNBOUND_INVOCATION")
        require_session_id(ops.session_context.session_id)
        return await dispatch_authorization(self, ops, caller)

    def caller_is_current(self, ops: EventHandlerOps, caller: AuthorizationCaller) -> bool:
        actual = self._caller_for_ops(ops)
        return actual is not None and actual.key == caller.key and self._caller_is_live(caller)

    def issue(self, caller: AuthorizationCaller, session_id: str) -> SessionAuthorization | None:
        """Issue a single opaque reference; used only by the trusted CORE frame."""
        from jiuwenswarm.extensions_v2.session_authorization.dispatch import current_frame
        frame = current_frame()
        if frame is None or frame.service is not self or frame.caller is not caller or frame.context.session_id != session_id:
            raise SessionAuthorizationError("UNBOUND_INVOCATION")
        self._require_open()
        self._prune()
        binding = self._credentials.get(session_id)
        if binding is None:
            return None
        if not self._caller_is_live(caller):
            raise SessionAuthorizationError("UNBOUND_INVOCATION")
        if len(self._handles) >= self._max_handles:
            raise SessionAuthorizationError("HANDLE_CAPACITY_EXCEEDED")
        handle = "sa_" + secrets.token_urlsafe(32)
        expires_at = self._clock() + self._handle_ttl
        if binding.expires_at is not None:
            expires_at = min(expires_at, binding.expires_at)
        self._handles[handle] = _Handle(caller, session_id, binding, expires_at)
        return {"handle": handle, "kind": binding.kind}

    def valid_result(self, frame, result: Mapping) -> bool:
        if not frame.core_called or frame.issued is None or dict(result) != frame.issued:
            return False
        value = self._handles.get(result.get("handle"))
        return bool(
            not self._closed and value is not None
            and value.session_id == frame.context.session_id
            and value.caller.key == frame.caller.key
            and self._caller_is_live(value.caller)
            and self._credentials.get(value.session_id) is value.binding
            and value.expires_at > self._clock()
            and (value.binding.expires_at is None or value.binding.expires_at > self._clock())
        )

    def _prune(self) -> None:
        now = self._clock()
        for session_id, value in tuple(self._credentials.items()):
            if value.expires_at is not None and value.expires_at <= now:
                self.unbind(session_id)
        for handle, value in tuple(self._handles.items()):
            if value.expires_at <= now or not self._caller_is_live(value.caller):
                self._handles.pop(handle, None)

    def _require_open(self) -> None:
        if self._closed:
            raise SessionAuthorizationError("SERVICE_CLOSED")
