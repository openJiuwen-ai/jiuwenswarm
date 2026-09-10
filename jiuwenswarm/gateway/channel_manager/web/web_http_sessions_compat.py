# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Enterprise bypass history HTTP: ``GET /api/sessions*`` (compat, not versioned ``/api/v1``).

Reads the same ``ChatHistoryStore`` written by Gateway ``WebChannel`` Listen
(and Web HTTP chat frames). JSON shape matches ``app_web._handle_history_api_get``.
"""

from __future__ import annotations
from jiuwenswarm.edition import is_enterprise

import asyncio
import logging
import os
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def register_sessions_compat_routes(app: FastAPI) -> None:
    """Mount ``GET /api/sessions`` and ``GET /api/sessions/{session_id}``.

    Enterprise only：``/api/sessions*``（无 ``/api/v1`` 前缀）走 ``ChatHistoryStore``；
    个人版不注册（``/api/v1/sessions`` 走 ``session_metadata`` JSON 文件方案）。
    """
    if not is_enterprise():
        logger.debug(
            "[WebHTTP] skip enterprise history compat routes (not enterprise edition)"
        )
        return

    @app.get(
        "/api/sessions",
        tags=["enterprise history"],
        summary="企业旁路会话列表（ChatHistoryStore；兼容 Web Pod）",
        include_in_schema=True,
    )
    async def sessions_list(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        user: str | None = Query(None),
    ) -> JSONResponse:
        from jiuwenswarm.channels.web.history_store import list_sessions_sync

        user_q = (user or "").strip() or None
        if user_q is None:
            hdr = request.headers.get("x-user-id")
            if hdr and str(hdr).strip():
                user_q = str(hdr).strip()
        sessions = await asyncio.to_thread(
            list_sessions_sync,
            None,
            limit=limit,
            offset=offset,
            user=user_q,
        )
        return JSONResponse({"sessions": sessions})

    @app.get(
        "/api/sessions/{session_id}",
        tags=["enterprise history"],
        summary="企业旁路会话详情（瘦对白 messages）",
        include_in_schema=True,
    )
    async def sessions_detail(
        request: Request,
        session_id: str,
        user: str | None = Query(None),
    ) -> JSONResponse:
        from jiuwenswarm.channels.web.history_store import get_session_detail_sync

        sid = str(session_id or "").strip()
        if not sid:
            return JSONResponse({"error": "missing_session_id"}, status_code=400)
        user_q = (user or "").strip() or None
        if user_q is None:
            hdr = request.headers.get("x-user-id")
            if hdr and str(hdr).strip():
                user_q = str(hdr).strip()
        detail = await asyncio.to_thread(
            get_session_detail_sync,
            sid,
            None,
            user=user_q,
        )
        if detail is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(detail)

    logger.info(
        "[WebHTTP] registered enterprise history compat routes "
        "GET /api/sessions , GET /api/sessions/{session_id} "
        "(enterprise=%s)",
        is_enterprise(),
    )


def catalog_sessions_compat_entries() -> list[dict[str, Any]]:
    return [
        {
            "http_method": "GET",
            "path": "/api/sessions",
            "rpc_method": None,
            "phase": "compat",
            "note": "企业旁路历史列表 → ChatHistoryStore（非 RPC session.list）",
        },
        {
            "http_method": "GET",
            "path": "/api/sessions/{session_id}",
            "rpc_method": None,
            "phase": "compat",
            "note": "企业旁路历史详情 → ChatHistoryStore",
        },
    ]
