# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Closed Ops proxies: callers supply arguments, never a bus or target event."""

from __future__ import annotations

from collections.abc import Mapping

from openjiuwen.core.events import EventHandlerOps, ExtensionEventBus, OpsMethod

from jiuwenswarm.extensions_v2.query_dispatch import dispatch_session_query


def session_query_method(bus: ExtensionEventBus, event_type: str) -> OpsMethod:
    """Capture platform routing without exposing overrideable partial keywords."""
    if event_type == "session.usage":
        async def usage(
            ops: EventHandlerOps,
            arguments: Mapping[str, object] | None = None,
            *,
            breakdown: str | None = None,
            columns: int | None = None,
        ) -> object:
            if arguments is not None and (breakdown is not None or columns is not None):
                raise TypeError("use one usage argument object or keyword arguments")
            if arguments is not None and not isinstance(arguments, Mapping):
                raise TypeError("session.usage requires an argument mapping or keywords")
            values = dict(arguments) if arguments is not None else {}
            if breakdown is not None:
                values["breakdown"] = breakdown
            if columns is not None:
                values["columns"] = columns
            return await dispatch_session_query(bus, ops, event_type, values)

        return usage

    async def query(ops: EventHandlerOps) -> object:
        return await dispatch_session_query(bus, ops, event_type, {})

    return query
