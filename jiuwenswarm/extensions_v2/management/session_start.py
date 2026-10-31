# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Serialize at-most-once start notifications for current Worker activations."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass

from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext
from openjiuwen.core.extension import ActivePackageRecord, Runtime

from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.extensions_v2.event_types.session_start import (
    SESSION_START_EVENT,
    SessionStartInput,
    SessionStartOutput,
    bind_start_invocation,
)


@dataclass
class _StartScope:
    gate: SessionStartGate
    active: bool = True


_STARTING: ContextVar[_StartScope | None] = ContextVar("session_start_gate", default=None)


class SessionStartGate:
    """Own start-attempt facts for one Manager/Runtime lifetime."""

    def __init__(self, bus: ExtensionEventBus, runtime: Runtime) -> None:
        self._bus = bus
        self._runtime = runtime
        self._lock = asyncio.Lock()
        self._attempted: dict[str, tuple[ActivePackageRecord, tuple[str, ...]]] = {}

    async def ensure_started(
        self,
        context: SessionContext,
        *,
        request_id: str,
        channel_id: str,
        is_interactive: bool,
    ) -> tuple[SessionStartOutput, ...]:
        """Attempt each new activation once before the caller runs native input.

        Args:
            context: Trusted session facts after model and directory binding.
            request_id: Native input request identifier.
            channel_id: Existing platform channel identifier.
            is_interactive: Bound runtime's user-interaction capability.

        Returns:
            Receipts from newly attempted activations during this call.

        Raises:
            ValueError: The host supplied an invalid session or field type.
            RuntimeError: Re-entry or a notification framework failure.
            asyncio.CancelledError: Cancellation; started attempts are not replayed.
        """
        if not is_valid_session_id(context.session_id):
            raise ValueError("session.start requires a valid session_id")
        if not isinstance(request_id, str) or not isinstance(channel_id, str) or not isinstance(is_interactive, bool):
            raise ValueError("session.start requires typed host input facts")
        parent = _STARTING.get()
        if parent is not None and parent.active and parent.gate is self:
            raise RuntimeError("session.start cannot re-enter its initialization gate")
        async with self._lock:
            scope = _StartScope(self)
            token = _STARTING.set(scope)
            receipts = []
            try:
                while True:
                    active = self._runtime.active_packages()
                    current_ids = {record.package_id for record in active}
                    self._attempted = {key: value for key, value in self._attempted.items() if key in current_ids}
                    pending = []
                    for record in active:
                        ids = self._runtime.published_handlers(record.package_id)
                        previous = self._attempted.get(record.package_id)
                        # Reinstall can reuse generation 1, even with no handlers.
                        # Keep the public activation record, not only scalar facts.
                        if previous is None or previous[0] is not record or previous[1] != ids:
                            pending.append((record, ids, (record, ids)))
                    if not pending:
                        return tuple(receipts)
                    for record, ids, key in pending:
                        if (
                            self._runtime.get_active(record.package_id) is not record
                            or self._runtime.published_handlers(record.package_id) != ids
                        ):
                            continue
                        self._attempted[record.package_id] = key
                        root = context.metadata.get("project_root")
                        value = SessionStartInput(
                            record.package_id, record.generation, context.session_id, request_id, channel_id,
                            context.cwd, root if isinstance(root, str) else None, is_interactive,
                        )
                        self._runtime.begin_calls((record.package_id,))
                        try:
                            with bind_start_invocation(value) as invocation:
                                await self._bus.trigger(Event(SESSION_START_EVENT, value), context, handler_ids=ids)
                                if invocation.receipt is None:
                                    raise RuntimeError("session.start returned without its CORE receipt")
                                receipts.append(invocation.receipt)
                        finally:
                            self._runtime.end_calls((record.package_id,))
            finally:
                scope.active = False
                _STARTING.reset(token)
