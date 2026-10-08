# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""OBS 上传辅助：prepare、upload、completeAndQuery 获取公网 URL."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any

import aiohttp
import httpx

from jiuwenswarm.common.local_proxy_auth import with_local_proxy_bearer
from jiuwenswarm.common.np_transport import is_named_pipe_url, named_pipe_transport_for
from jiuwenswarm.common.utils import logger


@dataclass
class XiaoyiObsUploadConfig:
    """小艺 OBS 上传：服务端点与鉴权（对应 channels.xiaoyi 配置项）."""

    base_url: str
    api_key: str
    uid: str


async def _post_control_plane(
    session: aiohttp.ClientSession,
    pipe_client: httpx.AsyncClient | None,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    step: str,
) -> dict[str, Any]:
    """phase1/3 控制面 POST：np:// base 经 httpx + 命名管道 transport，否则走 aiohttp。

    打本地代理（np:// 管道 / loopback 直连形态）时补 Authorization: Bearer
    <uploadToken>（密钥包下发，取不到则不带，兼容旧版桌面零鉴权代理）。
    """
    headers = with_local_proxy_bearer(headers, url)
    if pipe_client is not None:
        resp = await pipe_client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"{step} failed: HTTP {resp.status_code}")
        return resp.json()
    async with session.post(url, json=payload, headers=headers) as resp:
        if not resp.ok:
            raise RuntimeError(f"{step} failed: HTTP {resp.status}")
        return await resp.json()


async def upload_local_file_public_url(
    session: aiohttp.ClientSession,
    config: XiaoyiObsUploadConfig,
    file_path: str,
    object_type: str = "TEMPORARY_MATERIAL_DOC",
    *,
    need_preview: bool = False,
    expire_time: int = 259200,
    file_content: bytes | None = None,
) -> str:
    """上传本地文件并通过 completeAndQuery 返回可公网访问的 URL.

    Args:
        file_content: Optional bytes already read by an authorized filesystem
            backend. When supplied (including empty bytes), never reopen file_path.
        need_preview: 为 True 时请求可预览 URL（对齐 openclaw
            ``uploadFileAndGetPreviewUrl`` 的 needPreview=true）。
        expire_time: 预览 URL 过期秒数；仅 need_preview=True 时写入请求体。

    Raises:
        RuntimeError: 任一步骤失败（prepare、上传、completeAndQuery 或缺少 url）。
    """
    base = config.base_url.rstrip("/")
    uid = config.uid
    # np:// base（桌面命名管道形态）：phase1/3 走管道 httpx；
    # phase2（OBS 签名 URL 直传，真实外网地址）仍用传入的 aiohttp session。
    pipe_client: httpx.AsyncClient | None = None
    if is_named_pipe_url(base):
        pipe_client = httpx.AsyncClient(
            transport=named_pipe_transport_for(base),
            timeout=httpx.Timeout(300.0, connect=10.0),
        )
    try:
        if file_content is None:
            with open(file_path, "rb") as f:
                file_content = f.read()
        file_name = os.path.basename(file_path)
        file_size = len(file_content)
        file_sha256 = hashlib.sha256(file_content).hexdigest()

        prepare_url = f"{base}/osms/v1/file/manager/prepare"
        prepare_data = {
            "objectType": object_type,
            "fileName": file_name,
            "fileSha256": file_sha256,
            "fileSize": file_size,
            "fileOwnerInfo": {"uid": uid, "teamId": uid},
            "useEdge": False,
        }
        headers = {
            "Content-Type": "application/json",
            "x-uid": uid,
            "x-api-key": config.api_key,
            "x-request-from": "openclaw",
        }
        prepare_resp = await _post_control_plane(
            session, pipe_client, prepare_url, prepare_data, headers, "Prepare"
        )
        if prepare_resp.get("code") != "0":
            raise RuntimeError(
                f"Prepare failed: {prepare_resp.get('desc', 'Unknown error')}"
            )
        object_id = prepare_resp.get("objectId")
        draft_id = prepare_resp.get("draftId")
        upload_infos = prepare_resp.get("uploadInfos", [])
        if not upload_infos:
            raise RuntimeError("No upload information returned")
        upload_info = upload_infos[0]
        async with session.request(
            upload_info.get("method", "PUT"),
            upload_info.get("url"),
            data=file_content,
            headers=upload_info.get("headers", {}),
        ) as resp:
            if not resp.ok:
                raise RuntimeError(f"Upload failed: HTTP {resp.status}")

        cq_url = f"{base}/osms/v1/file/manager/completeAndQuery"
        cq_data: dict = {"objectId": object_id, "draftId": draft_id}
        if need_preview:
            cq_data["needPreview"] = True
            cq_data["expireTime"] = int(expire_time)
        cq_resp = await _post_control_plane(
            session, pipe_client, cq_url, cq_data, headers, "completeAndQuery"
        )
        file_url = (cq_resp.get("fileDetailInfo") or {}).get("url") or ""
        if not file_url:
            raise RuntimeError("completeAndQuery 未返回 fileDetailInfo.url")
        return file_url
    except RuntimeError:
        raise
    except Exception as e:
        logger.error("[upload_local_file_public_url] %s", e)
        raise RuntimeError(f"OBS 上传失败: {e}") from e
    finally:
        if pipe_client is not None:
            await pipe_client.aclose()
