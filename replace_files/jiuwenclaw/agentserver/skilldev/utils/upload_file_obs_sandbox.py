# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from jiuwenclaw.utils import build_default_headers
import hashlib
import uuid
import logging
import time

import aiohttp
import requests

import os

logger = logging.getLogger(__name__)
OSML_SLB_URL = os.environ.get('SANDBOX_OSMS_SLB_URL', 'null')
logger.info("OSML_SLB_URL: %s", OSML_SLB_URL)
osms_prepare_url = OSML_SLB_URL.rstrip('/') + '/osms/v1/file/manager/prepare'
osms_complete_query_url = OSML_SLB_URL.rstrip('/') + '/osms/v1/file/manager/completeAndQuery'

SUCCESS_STATUS_CODE = 200
MAX_TIMES = 3  # 执行次数
connect_timeout = 15
read_timeout = 300


class UploadFileByOSMS(object):
    def __init__(self, session_id="default"):
        self.trace_id = str(uuid.uuid4())[:16]
        self.session_id = session_id

    async def upload_file(self, file_path: str, use_edge=False):
        try:
            upload_file_info = await self.upload_file_and_get_info(file_path, use_edge=use_edge)
            logger.info(f"[session={self.session_id}] [SandboxUploadFileByOSMS] upload_file_info: {upload_file_info}")
            if isinstance(upload_file_info, str) or not upload_file_info:
                return None
            return upload_file_info["url"]
        except Exception as e:
            logger.error(f"[session={self.session_id}] [SandboxUploadFileByOSMS] 上传文件失败: {e}", exc_info=True)

    async def upload_file_and_get_info(self, file_path: str, use_edge=False):
        if not file_path:
            logger.error(f"[session={self.session_id}] [SandboxUploadFileByOSMS] 文件路径为空，无法上传")
            return ""

        if not os.path.exists(file_path):
            logger.error(f"[session={self.session_id}] [SandboxUploadFileByOSMS] 文件{file_path}不存在")
            return "文件不存在"

        if not os.path.isfile(file_path):
            logger.error(f"[session={self.session_id}] [SandboxUploadFileByOSMS] {file_path}不是文件")
            return "路径不是文件"

        logger.info(f"[session={self.session_id}] [SandboxUploadFileByOSMS] 1.开始做上传文件准备: {file_path}")
        file_info = await self.invoking_osms_prepare(file_path)
        if not file_info:
            logger.error(f"[session={self.session_id}] [SandboxUploadFileByOSMS] {file_path} 上传OSMS失败")
            return "{}:文件上传失败OSMS".format(os.path.basename(file_path))

        logger.info(f"[session={self.session_id}] [SandboxUploadFileByOSMS] 2.开始上传: {file_path}")
        result = await self.read_file_as_bytes(file_path)
        if not result["success"]:
            return "{}:{}".format(result["file_name"], result["error"])
        upload_file_info = await self.upload_file_to_obs(file_info["uploadInfos"][0], result["bytes"])

        if not upload_file_info:
            logger.error(f"[session={self.session_id}] [SandboxUploadFileByOSMS] {file_path} 上传OBS失败")
            return "{}:上传文件到 obs 报错".format(os.path.basename(file_path))

        logger.info(f"[session={self.session_id}] [SandboxUploadFileByOSMS] 3.查看上传结果: {file_path}")
        file_detail_info = await self.invoking_osms_complete_and_query(file_info, use_edge=use_edge)
        if not file_detail_info:
            logger.error(f"[session={self.session_id}] [SandboxUploadFileByOSMS] 获取osms的cdn链接失败：{file_path}")

        return file_detail_info

    async def read_file_as_bytes(self, file_path):
        # 读取任意文件的原始二进制内容
        result = {
            "success": False,
            "file_name": os.path.basename(file_path),
            "bytes": None,
            "error": None
        }

        try:
            with open(file_path, 'rb') as f:
                byte_content = f.read()
                result.update({
                    "success": True,
                    "bytes": byte_content
                })

        except PermissionError:
            result["error"] = "没有读取权限"
        except Exception as e:
            logger.error(f"[session={self.session_id}] [SandboxUploadFileByOSMS] 读取文件失败: {e}", exc_info=True)
            result["error"] = f"读取失败: {str(e)}"

        return result

    async def upload_file_to_obs(self, file_info, file_bytes):
        retry_delay = 1  # 初始重试延迟(秒)
        retry_time = 1  # 当前块重试次数
        while True:
            try:
                response = requests.put(
                    file_info["url"],
                    data=file_bytes,
                    headers=file_info["headers"],
                    timeout=(connect_timeout, read_timeout)
                )
                logger.info(f"[session={self.session_id}] [SandboxUploadFileByOSMS] response:{response}")
                response.raise_for_status()
                return True

            except Exception as e:
                logger.error(
                    f"[session={self.session_id}] [SandboxUploadFileByOSMS] upload file to obs fail: {e}",
                    exc_info=True
                )
                retry_time += 1
                time.sleep(retry_delay * (2 ** retry_time))
                if retry_time > MAX_TIMES:
                    logger.error(
                        "[session={}] [SandboxUploadFileByOSMS] {}上传文件到 obs 报错{}"
                        .format(self.session_id, file_info["url"], str(e))
                    )
                    return False

    async def calculate_file_sha256(self, file_path):
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            while chunk := f.read(4096):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _build_headers(self):
        default_headers = build_default_headers()
        return {
            "Content-Type": "application/json",
            "x-sandbox-id": default_headers.get("x-sandbox-id", ""),
            "x-api-key": default_headers.get("x-api-key", ""),
            "x-hag-trace-id": self.session_id,
            "x-request-from": "jiuwenclaw",  # 不能修改
        }

    async def invoking_osms_prepare(self, file_path):
        headers = self._build_headers()
        body = {
            "objectType": "COMMON_PRIVATE",
            "fileName": os.path.basename(file_path),
            "fileSha256": await self.calculate_file_sha256(file_path),
            "fileSize": os.path.getsize(file_path),
            "fileOwnerInfo": {
                "uid": "jiuwenclaw",
                "teamId": "jiuwenclaw"
            },
            "useEdge": False
        }
        logger.info(f"Invoke the osms prepare interface, trace_id: {self.trace_id}")
        for times in range(MAX_TIMES):
            try:
                async with aiohttp.ClientSession() as session:

                    async with session.post(url=osms_prepare_url, headers=headers, json=body, ssl=True) as response:
                        if response.status != SUCCESS_STATUS_CODE:
                            text = await response.text()
                            raise RuntimeError(
                                f"Invoke prepare failed, SUCCESS_STATUS_CODE={SUCCESS_STATUS_CODE}, "
                                f"but status={response.status}, body={text}"
                            )
                        resp = await response.json()
                        logger.info(
                            f"[session={self.session_id}] [SandboxUploadFileByOSMS] "
                            f"prepare interface business resp: {resp}"
                        )
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
                logger.error(
                    f"[session={self.session_id}] [SandboxUploadFileByOSMS]"
                    f" {times + 1}st invoking osms prepare interface throws exception: {e}",
                    exc_info=True
                )
                if times == MAX_TIMES - 1:
                    raise RuntimeError(
                        f"osms prepare interface call failed after retry limit, type: {type(e).__name__}", e
                    ) from e
        return {}

    async def invoking_osms_complete_and_query(self, file_info, use_edge=False):
        headers = self._build_headers()
        body = {
            "objectId": file_info["objectId"],
            "draftId": file_info["draftId"],
            "expireTime": 3600,
            # 默认以原始文件名下载
            "useOriginNameAsDownloadName": True,
            # completeAndQuery 调skill_scan的话，就带useEdge=true
            "useEdge": use_edge
        }
        for times in range(MAX_TIMES):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                            url=osms_complete_query_url,
                            headers=headers,
                            json=body,
                            ssl=True
                    ) as response:
                        logger.info(
                            f"[session={self.session_id}] [SandboxUploadFileByOSMS] "
                            f"Invoke completeAndQuery, post to {osms_complete_query_url} response: {response}"
                        )
                        if response.status != SUCCESS_STATUS_CODE:
                            continue
                        resp = await response.json()
                        logger.info(
                            f"[session={self.session_id}] [SandboxUploadFileByOSMS] completeAndQuery "
                            f"interface business resp: {resp}"
                        )
                        if resp.get("code") != '0':
                            logger.error(
                                f"[session={self.session_id}] [SandboxUploadFileByOSMS] completeAndQuery "
                                f"interface business error: {resp}"
                            )
                            continue
                        file_detail = resp.get("fileDetailInfo")
                        if not file_detail or 'url' not in file_detail:
                            raise RuntimeError("the osms completeAndQuery interface fileDetailInfo.url is empty")
                        return file_detail
            except Exception as e:
                logger.error(
                    f"[session={self.session_id}] [SandboxUploadFileByOSMS] "
                    f"{times + 1}st invoking osms completeAndQuery interface throws exception：{e}",
                    exc_info=True
                )
                if times == MAX_TIMES - 1:
                    raise RuntimeError(
                        f"osms completeAndQuery interface call failed after retry limit, type: {type(e).__name__}",
                        e) from e
        return None
