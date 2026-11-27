# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""OBS 上传辅助：prepare、upload、completeAndQuery 获取公网 URL."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

import aiohttp

from jiuwenclaw.utils import logger


@dataclass
class XiaoyiObsUploadConfig:
    """小艺 OBS 上传：服务端点与鉴权（对应 channels.xiaoyi 配置项）."""

    base_url: str
    api_key: str
    uid: str


async def upload_local_file_public_url(
    session: aiohttp.ClientSession,
    config: XiaoyiObsUploadConfig,
    file_path: str,
    object_type: str = "TEMPORARY_MATERIAL_DOC",
) -> str:
    """上传本地文件并通过 completeAndQuery 返回可公网访问的 URL.

    Raises:
        RuntimeError: 任一步骤失败（prepare、上传、completeAndQuery 或缺少 url）。
    """
    base = config.base_url.rstrip("/")
    uid = config.uid
    try:
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
        async with session.post(prepare_url, json=prepare_data, headers=headers) as resp:
            if not resp.ok:
                raise RuntimeError(f"Prepare failed: HTTP {resp.status}")
            prepare_resp = await resp.json()
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
        cq_data = {"objectId": object_id, "draftId": draft_id}
        async with session.post(cq_url, json=cq_data, headers=headers) as resp:
            if not resp.ok:
                raise RuntimeError(f"completeAndQuery failed: HTTP {resp.status}")
            cq_resp = await resp.json()
        file_url = (cq_resp.get("fileDetailInfo") or {}).get("url") or ""
        if not file_url:
            raise RuntimeError("completeAndQuery 未返回 fileDetailInfo.url")
        return file_url
    except RuntimeError:
        raise
    except Exception as e:
        logger.error("[upload_local_file_public_url] %s", e)
        raise RuntimeError(f"OBS 上传失败: {e}") from e
