# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""分享长图任务查询。挂在 Gateway 的 Web HTTP 上，供 /share-api 转发。

没有进行中的任务时返回 200 和 ``{"job": null}``。页面打开会话就会查一次，
不能用「路由不存在」的 404 表示没有任务。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response


def register_share_job_routes(app: FastAPI) -> None:
    """注册 ``/share-api/jobs`` 查询。"""

    @app.get("/share-api/jobs", tags=["share"], summary="当前会话的分享任务")
    async def share_job_for_session(request: Request) -> JSONResponse:
        session_id = str(request.query_params.get("session_id") or "").strip()
        if not session_id:
            return JSONResponse({"error": "missing_session_id"}, status_code=400)
        status = _manager().get_active_status(session_id)
        if status is None:
            return JSONResponse({"job": None})
        return JSONResponse(status)

    @app.get("/share-api/jobs/{job_id}", tags=["share"], summary="分享任务状态")
    async def share_job_status(job_id: str) -> JSONResponse:
        status = _manager().get_status(job_id)
        if status is None:
            return JSONResponse({"error": "job_not_found"}, status_code=404)
        return JSONResponse(status)

    @app.get("/share-api/jobs/{job_id}/snapshot", tags=["share"], summary="分享快照")
    async def share_job_snapshot(job_id: str) -> Response:
        path = _manager().get_snapshot_path(job_id)
        if path is None:
            return JSONResponse({"error": "job_file_not_found"}, status_code=404)
        return FileResponse(path, media_type="application/json", filename="snapshot.json")

    @app.get("/share-api/jobs/{job_id}/download", tags=["share"], summary="下载分享图")
    async def share_job_download(job_id: str) -> Response:
        result = _manager().get_result(job_id)
        if result is None:
            status = _manager().get_status(job_id)
            code = 404 if status is None else 409
            error = "job_not_found" if status is None else "job_not_completed"
            return JSONResponse({"error": error}, status_code=code)
        path, filename = result
        if not path.is_file():
            return JSONResponse({"error": "job_file_not_found"}, status_code=404)
        media = "application/zip" if filename.lower().endswith(".zip") else "image/png"
        return FileResponse(path, media_type=media, filename=filename)


def _manager():
    from jiuwenswarm.channels.web.app_web import _get_share_image_export_manager

    return _get_share_image_export_manager()
