# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enter config.set before the existing AgentServer configuration writer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
import logging

from openjiuwen.core.events import Event, SessionContext

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.config_extension.catalog import PROVIDER, public_fields, read_values
from jiuwenswarm.extensions_v2.config_extension.dispatch import ConfigFrame, SET_FRAME
from jiuwenswarm.extensions_v2.event_types.config import ConfigExtensionError
from jiuwenswarm.server.runtime.gateway_adapter.base import build_error_response

logger = logging.getLogger(__name__)
_ACTIVE_KEYS = ContextVar("config_native_active_keys", default=())


@dataclass(slots=True)
class _Batch:
    keys: tuple[str, ...]
    active: bool = True


async def route_config_request(
    request: AgentRequest,
    native: Callable[[AgentRequest], Awaitable[AgentResponse]],
) -> AgentResponse:
    """Join independent field hooks at one original save, retaining its receipt.

    A pre-save denial prevents the entire batch. Once the native save has
    returned, post-next rewrites cannot falsify or undo that receipt.
    """
    from jiuwenswarm.extensions_v2.assembly import get_extension_bus

    bus = get_extension_bus()
    if bus is None or not isinstance(request.params, dict):
        return await native(request)
    params = deepcopy(request.params)
    updates = params.get("config") if request.req_method == ReqMethod.CONFIG_SAVE_ALL else params
    if not isinstance(updates, dict):
        return await native(request)
    is_locale = request.req_method == ReqMethod.LOCALE_SET_CONF
    selected = [field for field in public_fields() if field.native_key in updates
                and (field.key == "preferred_language") == is_locale]
    if not selected:
        return await native(request)
    keys = tuple(field.key for field in selected)
    batches = tuple(batch for batch in _ACTIVE_KEYS.get() if batch.active)
    if any(key in batch.keys for batch in batches for key in keys):
        return build_error_response(request, "recursive configuration save", code="CONFIG_REENTRANT")
    batch = _Batch(keys)
    token = _ACTIVE_KEYS.set((*batches, batch))
    response = None
    tasks = []
    try:
        previous = await asyncio.to_thread(read_values, "effective")
        try:
            candidates = {field.key: field.candidate(updates[field.native_key]) for field in selected}
        except ValueError:
            raise ConfigExtensionError("INVALID_VALUE") from None
        context = SessionContext(session_id=request.session_id, metadata={
            "user_id": request.user_id or "", "channel_id": request.channel_id or "",
        }) if request.session_id else None

        decision = asyncio.get_running_loop().create_future()
        ready = set()
        denial_code = "CONFIG_SET_DENIED"

        def abort(reason, code="CONFIG_SET_DENIED"):
            nonlocal denial_code
            if not decision.done() and response is None:
                denial_code = code
                decision.set_result({"deny": reason})

        async def enter(field):
            original = {"key": field.key, "value": candidates[field.key], "previous": previous[field.key],
                        "provider": dict(PROVIDER), "origin": {"kind": "bridge"}, "scope": "user"}

            async def save(value):
                nonlocal response
                updates[field.native_key] = value
                ready.add(field.key)
                if len(ready) == len(selected) and not decision.done():
                    response = await native(replace(request, params=params))
                    decision.set_result(None if response.ok else {"deny": "native configuration save failed"})
                downstream = await asyncio.shield(decision)
                if downstream is not None:
                    return dict(downstream)
                return {"value": value}

            frame = ConfigFrame(original, context, asyncio.current_task(), continuation=save)
            frame_token = SET_FRAME.set(frame)
            try:
                output = await bus.trigger(Event("config.set", original), context)
                if response is not None:
                    if response.ok and output != frame.result:
                        logger.warning("[Config] ignored a post-commit config.set result rewrite for %s", field.key)
                    return frame.result
                if isinstance(output, Mapping) and "deny" in output:
                    abort(output["deny"])
                    return output
                abort("configuration hook did not continue to CORE", "UNCOMMITTED_RESULT")
                raise ConfigExtensionError("UNCOMMITTED_RESULT")
            except BaseException:
                abort("configuration hook did not complete", "CONFIG_EXTENSION_ERROR")
                raise
            finally:
                frame.active = False
                SET_FRAME.reset(frame_token)

        # Creating all tasks from the host frame preserves tenant ContextVars
        # without nesting an active config.set USER Handler inside itself.
        # Core intentionally skips active Handler IDs on reentry; nested field
        # dispatch would silently omit the same policy on subsequent fields.
        tasks = [asyncio.create_task(enter(field), name="config.set:" + field.key) for field in selected]
        await asyncio.gather(*tasks)
        if response is not None:
            return response
        return build_error_response(request, decision.result()["deny"], code=denial_code)
    except Exception as error:
        if response is not None:
            logger.warning("[Config] config.set hook failed after native completion (%s)", type(error).__name__)
            return response
        code = error.code if isinstance(error, ConfigExtensionError) else "CONFIG_EXTENSION_ERROR"
        logger.warning("[Config] rejected config.set before native completion (%s)", type(error).__name__)
        return build_error_response(request, "configuration extension rejected the save", code=code)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        batch.active = False
        _ACTIVE_KEYS.reset(token)
