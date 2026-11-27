# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
import base64
import hashlib
import hmac
import os
import uuid
from datetime import datetime, UTC
import logging

import aiohttp
import requests
from tqdm import tqdm
from urllib.parse import urljoin

from jiuwenclaw.agentserver.skilldev.utils.project_config import project_config
from jiuwenclaw.agentserver.skilldev.utils.file import get_task_dir, copy_file, get_extension_split
from jiuwenclaw.gateway.sts_password import get_sts_dcs_password

logger = logging.getLogger(__name__)

ENV = project_config.ENV
# 项目配置参数
hag_osms_ak = os.environ['HAG_OSMS_AK']
hag_osms_sk = get_sts_dcs_password()
hag_osms_slb_url = os.environ['HAG_OSMS_SLB_URL']
osms_query_url = urljoin(hag_osms_slb_url, os.environ['OSMS_QUERY_URL'])
osms_delete_url = urljoin(hag_osms_slb_url, os.environ['OSMS_DELETE_URL'])

SUCCESS_STATUS_CODE = 200
MAX_TIMES = 3
# 超时时间：连接超时15秒，上传/读取超时300秒（5分钟）
connect_timeout = 15
read_timeout = 300


class QueryUrlOSMS(object):
    def __init__(self):
        self.trace_id = str(uuid.uuid4())[:16]
        self.isPermanentStoragePublic = False

    async def format_date(self):
        time_stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        time_stamp = time_stamp[:len(time_stamp) - 3]
        return time_stamp

    async def generate_cipher_text(self, time_stamp):
        message = (time_stamp + hag_osms_ak).encode('utf-8')
        secret = hag_osms_sk.encode('utf-8')
        signature = hmac.new(secret, message, hashlib.sha256).hexdigest()
        return base64.b64encode(bytes.fromhex(signature)).decode('utf-8')

    async def build_osms_headers(self) -> tuple[str, dict]:
        time_stamp = await self.format_date()
        headers = {
            "Content-Type": "application/json",
            "x-access-key": hag_osms_ak,
            "x-sign": await self.generate_cipher_text(time_stamp),
            "x-ts": time_stamp,
            "x-hag-trace-id": self.trace_id,
        }
        return time_stamp, headers

    async def invoking_osms_query(self, file_info):
        result = ""
        for times in range(0, MAX_TIMES):
            time_stamp, headers = await self.build_osms_headers()
            body = {
                "detectUrl4Station": False,
                "useEdge": project_config.obs_use_edge,
                "objectInfos": [
                    {
                        "objectId": file_info["objectId"],
                        "expireTime": project_config.obs_expire_time  # 设置有效期
                    }
                ],
                "permissionMode": "ALL"
            }

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url=osms_query_url, headers=headers, json=body, ssl=True) as response:
                        if response.status != SUCCESS_STATUS_CODE:
                            continue
                        resp = await response.json()
                        if 'fileDetailInfos' not in resp.keys():
                            error = "the hag osms query interface fileDetailInfos returns is empty"
                            raise RuntimeError(error)

                        if 'url' not in resp["fileDetailInfos"][0].keys():
                            error = "the hag osms query interface cdn url for fileDetailInfos returns is empty!"
                            raise RuntimeError(error)

                        return resp["fileDetailInfos"][0]
            except Exception as e:
                logger.error(f"Invoking osms query fail:{e}", exc_info=True)
                logger.error("Invoking the hag osms query interface throws the exception: {}.".format(times))
                if times == MAX_TIMES - 1:
                    logger.info("三次重试过后仍旧失败！！！")
                    raise RuntimeError(f"osms query interface call failed after retry limit reached, "
                                       f"type: {type(e).__name__}", e) from e
        return result

    def validate_osms_query_resp(self, resp: dict) -> None:
        if 'fileDetailInfos' not in resp.keys():
            error = "the hag osms query interface fileDetailInfos returns is empty"
            raise RuntimeError(error)

        if resp["fileDetailInfos"] is None or len(resp["fileDetailInfos"]) == 0:
            error = "the hag osms query interface fileDetailInfos returns is empty"
            raise RuntimeError(error)

        if 'url' not in resp["fileDetailInfos"][0].keys():
            error = "the hag osms query interface cdn url for fileDetailInfos returns is empty!"
            raise RuntimeError(error)

    async def invoking_osms_delete(self, file_url):
        try:
            time_stamp = await self.format_date()
            headers = {
                "x-access-key": hag_osms_ak,
                "x-sign": await self.generate_cipher_text(time_stamp),
                "x-ts": time_stamp,
            }

            body = {
                "url": file_url,
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(url=osms_delete_url, headers=headers, json=body, ssl=True) as response:
                    if response.status != 200:  # 假设 200 是成功的状态码
                        response.raise_for_status()
                    logger.info("Delete file successfully, file url: {}".format(file_url))
                    return True
        except Exception as e:
            logger.error(f"Invoking osms delete fail:{e}", exc_info=True)
            logger.error("Failed to invoke the hag osms delete interface throws the exception: {}.".format(str(e)))
            return False

    async def invoking_osms_query_by_url(self, file_url, file_path=None):
        """ 新的url替换旧的url，且判断是否增加可预览字段 """
        result = ""
        for times in range(0, MAX_TIMES):
            time_stamp = await self.format_date()
            headers = {
                "Content-Type": "application/json",
                "x-access-key": hag_osms_ak,
                "x-sign": await self.generate_cipher_text(time_stamp),
                "x-ts": time_stamp,
                "x-hag-trace-id": "superAgent_" + time_stamp,
            }

            body = {
                "detectUrl4Station": False,
                "useEdge": project_config.obs_use_edge,
                "objectInfos": [
                    {
                        "url": file_url,
                    }
                ],
                "permissionMode": "ALL"
            }

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url=osms_query_url, headers=headers, json=body, ssl=True) as response:
                        if response.status != SUCCESS_STATUS_CODE:
                            continue
                        resp = await response.json()
                        self.validate_osms_query_resp(resp)

                        result = resp["fileDetailInfos"][0]['url']
                        if not result:
                            logger.error("The hag osms query interface cdn url for fileDetailInfos returns is empty!")
                            error = "the hag osms query interface cdn url for fileDetailInfos returns is empty!"
                            raise RuntimeError(error)

                        return result
            except Exception as e:
                logger.error(f"Invoking osms query by url fail:{e}", exc_info=True)
                logger.error("Invoking the hag osms query interface throws the exception: {}.".format(times))
                if times == MAX_TIMES - 1:
                    raise RuntimeError(f"osms query interface call failed after retry limit reached, "
                                       f"type: {type(e).__name__}", e) from e
        return result

    async def get_latest_obs_url(self, old_url) -> str:
        new_url = await self.invoking_osms_query_by_url(old_url)
        logger.info("Exchanging OBS url, old_url: %s, new_url: %s", old_url, new_url)
        return new_url


