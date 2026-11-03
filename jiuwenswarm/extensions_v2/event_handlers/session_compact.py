# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The CORE tail prepares a candidate; the native engine installs it later."""

from jiuwenswarm.extensions_v2.session_compaction.dispatch import prepare_compact


async def session_compact_core(event, ops, next_handler):
    """Validate the host binding and run the native default continuation."""
    del next_handler
    return await prepare_compact(event, ops)
