# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Skill 上传 HTTP 端点（企业 gateway WebChannel FastAPI app）。

企业版前端 ``SkillPanel`` 上传 .zip 走两步：
1. ``POST /file-api/skills/upload-temp`` 暂存 ZIP（本模块）；
2. WebSocket RPC ``skills.import_upload`` 安装（经 ``invoke.py`` 白名单放行后
   转发到 ``SkillManager.handle_skills_import_upload``，含暂存文件归属校验
   与上传资格复查）。

本模块在企业 gateway 注册 Skill 写端点的 HTTP 路由，并接入
``skill_upload_permission`` 门禁（调 manager 授权表，fail-closed）。
container file 的 AgentOSRouterClient 认证中间件对这些路径放行
（见 ``container_file_http._SKILL_UPLOAD_HTTP_PATHS``），由本模块独立做资格校验，
避免双重 token 校验冲突。

注册的端点：
- ``POST /file-api/skills/upload-temp``（前端两步上传的第 1 步；前端使用）
- ``POST /file-api/skills/import``（一步导入；前端未使用，WS RPC 走
  ``skills.import_upload``。保留以兼容可能的客户端）
- ``POST /file-api/skills/create-from-knowledge``（知识转技能；前端未使用，
  WS RPC 走 ``skills.create_from_knowledge``。保留以兼容可能的客户端）
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from jiuwenswarm.channels.web.skill_upload_permission import (
    SkillUploadPermissionError,
    check_skill_upload_permission,
)
from jiuwenswarm.server.runtime.skill.skills_multipart_http import (
    handle_skills_create_from_knowledge_http,
    handle_skills_import_http,
    handle_skills_uninstall_http,
    handle_skills_upload_temp_http,
)

if TYPE_CHECKING:
    from gateway.channel_manager.web.web_connect import WebChannel

logger = logging.getLogger(__name__)

_ACCESS_COOKIE = "openjiuwen_access_token"
_JIUWENCLAW_ID_COOKIE = "jiuwenclaw_id"
# 与 skills_multipart_http 的临时上传对齐：multipart body 上限 50 MiB。
_MAX_UPLOAD_BODY_BYTES = 50 * 1024 * 1024
# create-from-knowledge 可能附较大文档；与 skills_multipart_http 的 RPC timeout 对齐。
_MAX_KNOWLEDGE_BODY_BYTES = 50 * 1024 * 1024


def _json(status: int, payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status, content=payload)


def _filter_user_skills(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从扫描结果中筛出用户自上传的 skill(source_type=user 或 source=local)。"""
    result: list[dict[str, Any]] = []
    for s in skills:
        source_type = str(s.get("source_type") or "").strip()
        if source_type == "user" or s.get("source") == "local":
            result.append(s)
    return result


async def _read_body(request: Request, limit: int) -> tuple[bytes | None, JSONResponse | None]:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None, _json(
                413,
                {
                    "code": "SKILL_FILE_TOO_LARGE",
                    "message": "上传文件过大",
                    "error": "upload too large",
                },
            )
    return bytes(body), None


def _guard(request: Request) -> JSONResponse | None:
    authorization = request.headers.get("Authorization", "")
    access_cookie = request.cookies.get(_ACCESS_COOKIE)
    jiuwenclaw_id = request.cookies.get(_JIUWENCLAW_ID_COOKIE) or ""
    # 兜底：部分桌面端把 jiuwenclaw_id 放 header 而非 cookie。
    if not jiuwenclaw_id:
        jiuwenclaw_id = request.headers.get("X-Cluster-Id") or ""
    try:
        check_skill_upload_permission(
            authorization=authorization,
            access_cookie=access_cookie,
            jiuwenclaw_id=jiuwenclaw_id,
        )
    except SkillUploadPermissionError as exc:
        return _json(exc.status, exc.payload)
    return None


def attach_skill_upload_routes(app: FastAPI, channel: WebChannel) -> None:  # noqa: ARG001
    """挂载 Skill 上传 HTTP 端点。

    无论企业/个人版都挂载；资格强制校验由 ``SKILL_UPLOAD_PERMISSION_ENFORCE``
    开关控制（默认关闭，企业部署显式打开）。
    """

    @app.post("/file-api/skills/upload-temp")
    async def upload_skill_temp(request: Request) -> JSONResponse:
        denied = _guard(request)
        if denied is not None:
            return denied
        content_type = request.headers.get("Content-Type", "")
        body, too_large = await _read_body(request, _MAX_UPLOAD_BODY_BYTES)
        if too_large is not None:
            return too_large
        caller_id = str(request.headers.get("X-User-Id") or "").strip()
        status, payload = await asyncio.to_thread(
            handle_skills_upload_temp_http,
            content_type=content_type,
            body=body,
            caller_id=caller_id,
        )
        return _json(status, payload)

    @app.post("/file-api/skills/import")
    async def import_skill(request: Request) -> JSONResponse:
        """一步 ZIP 导入（前端未使用；兼容可能的客户端）。

        走 ``handle_skills_import_http``：内部调 WS RPC ``skills.import_upload``
        完成安装，安装前在 handler 内做暂存归属校验 + 上传资格复查。
        ``X-User-Id`` / ``X-Cluster-Id`` 头用于让 handler 内的复查能拿到身份
        （HTTP 端点不经 AgentServer 的 interface.py 注入）。
        """
        denied = _guard(request)
        if denied is not None:
            return denied
        content_type = request.headers.get("Content-Type", "")
        body, too_large = await _read_body(request, _MAX_UPLOAD_BODY_BYTES)
        if too_large is not None:
            return too_large
        caller_id = str(request.headers.get("X-User-Id") or "").strip()
        cluster_id = str(request.headers.get("X-Cluster-Id") or "").strip()
        # 企业 gateway 进程同机/同 pod 可访问 SkillManager；走本地 manager 避免二次 WS 跳。
        status, payload = await asyncio.to_thread(
            handle_skills_import_http,
            content_type=content_type,
            body=body,
            use_local_manager=True,
            caller_id=caller_id,
            cluster_id=cluster_id,
        )
        return _json(status, payload)

    @app.post("/file-api/skills/create-from-knowledge")
    async def create_skill_from_knowledge(request: Request) -> JSONResponse:
        """知识转技能（前端未使用；兼容可能的客户端）。

        走 ``handle_skills_create_from_knowledge_http``：内部调 WS RPC
        ``skills.create_from_knowledge`` 完成生成。
        """
        denied = _guard(request)
        if denied is not None:
            return denied
        content_type = request.headers.get("Content-Type", "")
        body, too_large = await _read_body(request, _MAX_KNOWLEDGE_BODY_BYTES)
        if too_large is not None:
            return too_large
        status, payload = await asyncio.to_thread(
            handle_skills_create_from_knowledge_http,
            content_type=content_type,
            body=body,
            use_local_manager=True,
        )
        return _json(status, payload)

    @app.get("/file-api/skills/list")
    async def list_local_skills(request: Request) -> JSONResponse:
        """返回 gateway 本地 workspace 的 skill 列表（含用户上传的 skill）。

        企业版 E2A 转发的 ``skills.list`` 可能因 workspace 路由差异遗漏
        ``source_type=user`` 的 skill；此端点用 gateway 本地 ``SkillManager``
        扫描，确保用户上传的 skill 始终可见。前端在 ``fetchSkills`` 后合并
        此端点返回的 user skill 到列表。
        """
        denied = _guard(request)
        if denied is not None:
            return denied

        def _scan() -> list[dict[str, Any]]:
            from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

            mgr = SkillManager()
            mgr.reload_state()
            return mgr.get_local_skills()

        skills = await asyncio.to_thread(_scan)
        user_skills = _filter_user_skills(skills)
        return _json(200, {"skills": user_skills})

    @app.post("/file-api/skills/uninstall")
    async def uninstall_skill(request: Request) -> JSONResponse:
        """卸载用户上传的 skill（gateway 本地 workspace）。

        企业版 WS RPC ``skills.uninstall`` 经 E2A 转发到 AgentServer，但
        用户上传的 skill 只在 gateway 本地安装；此端点直接用本地
        ``SkillManager`` 卸载，prebuilt/builtin 由 handler 内部保护拒绝。
        """
        denied = _guard(request)
        if denied is not None:
            return denied
        try:
            body_text = (await request.body()).decode("utf-8", errors="replace")
            params = json.loads(body_text) if body_text else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json(400, {"code": "SKILL_INVALID_PACKAGE", "message": "请求体不是合法 JSON"})
        name = str(params.get("name") or "").strip()
        if not name:
            return _json(400, {"code": "SKILL_INVALID_PACKAGE", "message": "缺少 name 参数"})
        origin = str(params.get("origin") or "").strip()
        status, payload = await asyncio.to_thread(
            handle_skills_uninstall_http,
            name=name,
            origin=origin,
        )
        return _json(status, payload)


