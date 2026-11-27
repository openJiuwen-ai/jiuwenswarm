# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Default configuration descriptions and the trusted native save bridge."""

from openjiuwen.core.events import Event, EventHandlerOps, EventNext


async def config_describe_core(event: Event, ops: EventHandlerOps | None, next_handler: EventNext) -> dict:
    from jiuwenswarm.extensions_v2.config_extension.dispatch import describe_core

    return describe_core(event, ops)


async def config_set_core(event: Event, ops: EventHandlerOps | None, next_handler: EventNext) -> dict:
    from jiuwenswarm.extensions_v2.config_extension.dispatch import set_core

    return await set_core(event, ops)
