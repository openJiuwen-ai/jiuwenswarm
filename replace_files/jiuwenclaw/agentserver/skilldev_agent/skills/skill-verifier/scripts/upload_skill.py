from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

import aiohttp
import requests

GATE_OBS_STATE_FILE = ".gate_obs_upload.json"
_GATE_OBS_MIME_TYPE = "application/zip"

logger = logging.getLogger(__name__)


def gate_obs_state_path(task_workspace: str | Path) -> Path:
    return Path(task_workspace) / GATE_OBS_STATE_FILE

OSMS_SLB_URL = os.environ.get('SANDBOX_OSMS_SLB_URL', 'null')
OSMS_PREPARE_URL = OSMS_SLB_URL.rstrip('/') + '/osms/v1/file/manager/prepare'
OSMS_COMPLETE_QUERY_URL = OSMS_SLB_URL.rstrip('/') + '/osms/v1/file/manager/completeAndQuery'

SUCCESS_STATUS_CODE = 200
MAX_TIMES = 3
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 300


def _build_headers() -> dict:
    from jiuwenclaw.utils import build_default_headers
    default_headers = build_default_headers()
    return {
        "Content-Type": "application/json",
        "x-sandbox-id": default_headers.get("x-sandbox-id", ""),
        "x-api-key": default_headers.get("x-api-key", ""),
        "x-hag-trace-id": default_headers.get("x-hag-trace-id", ""),
        "x-request-from": "jiuwenclaw",
    }


def _calculate_file_sha256(file_path: str) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        while chunk := f.read(4096):
            sha256.update(chunk)
    return sha256.hexdigest()


def _read_file_as_bytes(file_path: str) -> dict:
    result = {
        "success": False,
        "file_name": os.path.basename(file_path),
        "bytes": None,
        "error": None,
    }
    try:
        with open(file_path, 'rb') as f:
            result.update({"success": True, "bytes": f.read()})
    except PermissionError:
        result["error"] = "没有读取权限"
    except Exception as e:
        logger.error(f"读取文件失败: {e}", exc_info=True)
        result["error"] = f"读取失败: {str(e)}"
    return result


def _upload_file_to_obs(file_info: dict, file_bytes: bytes) -> bool:
    retry_delay = 1
    retry_time = 1
    while True:
        try:
            response = requests.put(
                file_info["url"], data=file_bytes,
                headers=file_info["headers"],
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"upload file to obs fail: {e}", exc_info=True)
            retry_time += 1
            time.sleep(retry_delay * (2 ** retry_time))
            if retry_time > MAX_TIMES:
                logger.error(f"{file_info['url']} 上传文件到 obs 报错 {e}")
                return False


async def _invoking_osms_prepare(file_path: str) -> dict:
    headers = _build_headers()
    body = {
        "objectType": "COMMON_PRIVATE",
        "fileName": os.path.basename(file_path),
        "fileSha256": _calculate_file_sha256(file_path),
        "fileSize": os.path.getsize(file_path),
        "fileOwnerInfo": {"uid": "jiuwenclaw", "teamId": "jiuwenclaw"},
        "useEdge": False,
    }
    for times in range(MAX_TIMES):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url=OSMS_PREPARE_URL, headers=headers, json=body, ssl=True) as response:
                    if response.status != SUCCESS_STATUS_CODE:
                        text = await response.text()
                        raise RuntimeError(
                            f"Invoke prepare failed, status={response.status}, body={text}")
                    resp = await response.json()
                    if resp.get("code") != '0':
                        raise RuntimeError(f"prepare interface business error: {resp}")
                    if 'objectId' not in resp or 'draftId' not in resp or 'uploadInfos' not in resp:
                        raise RuntimeError("The osms prepare interface returns an exception")
                    if not resp["uploadInfos"]:
                        raise RuntimeError("The osms prepare interface uploadInfos returns is empty")
                    if 'url' not in resp["uploadInfos"][0] or 'headers' not in resp["uploadInfos"][0]:
                        raise RuntimeError("The osms prepare interface url and headers for uploadInfos is empty")
                    return resp
        except Exception as e:
            logger.error(f"{times + 1}st invoking osms prepare throws exception: {e}", exc_info=True)
            if times == MAX_TIMES - 1:
                raise RuntimeError(
                    f"osms prepare interface call failed after retry limit, type: {type(e).__name__}", e) from e
    return {}


async def _invoking_osms_complete_and_query(file_info: dict, use_edge: bool = False) -> dict | None:
    headers = _build_headers()
    body = {
        "objectId": file_info["objectId"],
        "draftId": file_info["draftId"],
        "expireTime": 3600,
        "useEdge": use_edge,
    }
    for times in range(MAX_TIMES):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url=OSMS_COMPLETE_QUERY_URL, headers=headers, json=body, ssl=True) as response:
                    if response.status != SUCCESS_STATUS_CODE:
                        text = await response.text()
                        raise RuntimeError(
                            f"Invoke completeAndQuery failed, status={response.status}, body={text}")
                    resp = await response.json()
                    if resp.get("code") != '0':
                        raise RuntimeError(f"completeAndQuery interface business error: {resp}")
                    file_detail = resp.get("fileDetailInfo")
                    if not file_detail or 'url' not in file_detail:
                        raise RuntimeError("The osms completeAndQuery interface fileDetailInfo.url is empty")
                    return file_detail
        except Exception as e:
            logger.error(f"{times + 1}st invoking osms completeAndQuery throws exception: {e}", exc_info=True)
            if times == MAX_TIMES - 1:
                raise RuntimeError(
                    f"osms completeAndQuery call failed after retry limit, type: {type(e).__name__}", e) from e
    return None


async def upload_file(file_path: str) -> str | None:
    """Upload file to OBS and return CDN URL; None on failure."""
    if not file_path:
        logger.error("文件路径为空，无法上传")
        return None
    if not os.path.exists(file_path):
        logger.error(f"文件 {file_path} 不存在")
        return None
    if not os.path.isfile(file_path):
        logger.error(f"{file_path} 不是文件")
        return None

    try:
        file_info = await _invoking_osms_prepare(file_path)
        if not file_info:
            logger.error(f"{file_path} 上传 OSMS 失败")
            return None

        result = _read_file_as_bytes(file_path)
        if not result["success"]:
            logger.error(f"{result['file_name']}: {result['error']}")
            return None

        ok = _upload_file_to_obs(file_info["uploadInfos"][0], result["bytes"])
        if not ok:
            logger.error(f"{file_path} 上传 OBS 失败")
            return None

        file_detail = await _invoking_osms_complete_and_query(file_info)
        if not file_detail:
            logger.error(f"获取 osms 的 cdn 链接失败: {file_path}")
            return None
        return file_detail["url"]
    except Exception as e:
        logger.error(f"上传文件失败: {e}", exc_info=True)
        return None


def record_gate_obs_upload(
    task_workspace: str | Path,
    url: str,
    packaged_path: str | Path,
) -> None:
    """Write the gate upload OBS URL to a hidden file under the workspace root."""
    obs_url = str(url or "").strip()
    if not obs_url:
        return

    packaged = Path(packaged_path)
    path = gate_obs_state_path(task_workspace)
    payload = {
        "url": obs_url,
        "filename": packaged.name,
        "mimeType": _GATE_OBS_MIME_TYPE,
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("[upload_skill] failed to write %s: %s", path, exc)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python3 -m scripts.upload_skill <packaged_path>")
        return 2

    url = asyncio.run(upload_file(argv[1]))
    if url is None:
        print("Upload failed.")
        return 1

    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
