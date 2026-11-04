# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-side extension runtime entry points.

``WebChannel`` calls :func:`init_gateway_extensions` on start,
:func:`intercept_gateway_frame` for every inbound frame and
:func:`notify_gateway_disconnect` when a socket closes. The singleton itself
lives in :mod:`jiuwenswarm.gateway.extensions_v2.assembly`; this module keeps
the channel-facing surface stable.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from jiuwenswarm.gateway.extensions_v2.assembly import (
    install_gateway_extensions,
    shutdown_gateway_extensions,
)
from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime

logger = logging.getLogger(__name__)


async def init_gateway_extensions(
    channel: Any,
    config: GatewayExtensionConfig | Mapping[str, Any] | None = None,
) -> GatewayRuntime | None:
    """Create and start the runtime for ``channel``; idempotent."""

    assembly = await install_gateway_extensions(channel, config)
    return assembly.runtime if assembly is not None else None


async def intercept_gateway_frame(
    ws: Any,
    req_id: str,
    method: str,
    params: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    *,
    request_context: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Run one frame through ``gateway.request``.

    ``headers`` carries ``request_context.headers`` from the browser frame so the
    authentication gate and USER handlers can read ``authorization``.
    ``visited=True`` means the frame was answered by the extension runtime and
    the native dispatch must be skipped. Any failure fails open: the frame
    continues on the path it would have taken without extensions.
    """

    from jiuwenswarm.gateway.extensions_v2.assembly import get_gateway_runtime

    runtime = get_gateway_runtime()
    if runtime is None or not isinstance(params, Mapping):
        return False, dict(params) if isinstance(params, Mapping) else {}
    try:
        return await runtime.intercept(ws, req_id, method, dict(params), headers, request_context=request_context)
    except Exception as exc:  # noqa: BLE001 - fail open to the native path
        logger.warning("[GatewayExtensions] frame interception failed, forwarding: %s", exc)
        return False, dict(params)


async def notify_gateway_disconnect(ws: Any, session_ids: Any = None) -> None:
    """Release connection-scoped extension state."""

    from jiuwenswarm.gateway.extensions_v2.assembly import get_gateway_runtime

    runtime = get_gateway_runtime()
    if runtime is None:
        return
    try:
        await runtime.on_disconnect(ws, session_ids)
    except Exception as exc:  # noqa: BLE001 - disconnect cleanup must not raise
        logger.warning("[GatewayExtensions] disconnect cleanup failed: %s", exc)
