# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only configuration capabilities, contributed through existing ops.build."""

import asyncio
from contextvars import ContextVar

from openjiuwen.core.events import Event, EventHandlerOps, EventHandlerOpsBuilder, EventNext, ExtensionEventBus, HandlerLevel

from jiuwenswarm.extensions_v2.config_extension.catalog import FIELDS, PROVIDER, project_snapshot, read_values
from jiuwenswarm.extensions_v2.config_extension.dispatch import ConfigFrame, DESCRIBE_FRAME
from jiuwenswarm.extensions_v2.event_types.config import ConfigExtensionError, ConfigRow, valid_describe_result

_ITEMS_ACTIVE = ContextVar("config_items_active", default=None)


def register_config_ops(bus: ExtensionEventBus) -> None:
    async def value(ops: EventHandlerOps, key: str, scope: str = "effective") -> bool | str | None:
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise ConfigExtensionError("INVALID_KEY")
        return (await asyncio.to_thread(read_values, scope)).get(key)

    async def snapshot(ops: EventHandlerOps) -> dict:
        return project_snapshot(await asyncio.to_thread(read_values, "effective"))

    async def items(ops: EventHandlerOps, scope: str = "effective") -> list[ConfigRow]:
        previous_frame = _ITEMS_ACTIVE.get()
        if previous_frame is not None and previous_frame.active:
            raise ConfigExtensionError("CONFIG_REENTRANT")
        active_frame = ConfigFrame({}, ops.session_context, asyncio.current_task())
        active_token = _ITEMS_ACTIVE.set(active_frame)
        try:
            values = await asyncio.to_thread(read_values, scope)
            rows = []
            for field in FIELDS:
                if field.key not in values:
                    continue
                original = {"key": field.key, "label": field.label, "description": field.description,
                            "isHidden": False, "provider": dict(PROVIDER), "scope": scope}
                frame = ConfigFrame(original, ops.session_context, asyncio.current_task())
                frame_token = DESCRIBE_FRAME.set(frame)
                try:
                    result = await bus.trigger(Event("config.describe", original), frame.context)
                finally:
                    frame.active = False
                    DESCRIBE_FRAME.reset(frame_token)
                if not valid_describe_result(result):
                    raise ConfigExtensionError("INVALID_RESULT")
                if result["isHidden"]:
                    continue
                row = {"key": field.key, "label": result["label"], "value": values[field.key],
                       "kind": "choice" if field.options else "boolean", "provider": dict(PROVIDER), "isLocked": False}
                if "description" in result:
                    row["description"] = result["description"]
                if field.options:
                    row["options"] = list(field.options)
                rows.append(row)
            return rows
        finally:
            active_frame.active = False
            _ITEMS_ACTIVE.reset(active_token)

    async def build(event: Event, ops: EventHandlerOps | None, next_handler: EventNext) -> EventHandlerOpsBuilder:
        builder = await next_handler(event)
        builder.add_namespace("config")
        builder.add_method("config", "value", value)
        builder.add_method("config", "items", items)
        builder.add_method("config", "snapshot", snapshot)
        return builder

    bus.register_event_handler("ops.build", build, {}, HandlerLevel.BUILTIN)
