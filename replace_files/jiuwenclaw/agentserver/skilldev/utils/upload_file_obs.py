# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
import base64
import hashlib
import hmac
import os
import pathlib
import time
import uuid
from datetime import datetime, UTC
from typing import Mapping, Any
import logging

import aiohttp
import requests
from tqdm import tqdm
from urllib.parse import urljoin

from .project_config import project_config
from jiuwenclaw.sts_service import sts_config
from .file import get_task_dir, copy_file, get_extension_split

logger = logging.getLogger(__name__)

ENV = project_config.ENV
# 项目配置参数
hag_osms_ak = os.environ['HAG_OSMS_AK']
hag_osms_sk = sts_config.get_sts_config(os.environ['OBS_TRANSFER_HAG_OSMS_SK_PATH'])
hag_osms_slb_url = os.environ['HAG_OSMS_SLB_URL']
osms_prepare_url = urljoin(hag_osms_slb_url, os.environ['OSMS_PREPARE_URL'])
osms_complete_url = urljoin(hag_osms_slb_url, os.environ['OSMS_COMPLETE_URL'])
osms_query_url = urljoin(hag_osms_slb_url, os.environ['OSMS_QUERY_URL'])
osms_delete_url = urljoin(hag_osms_slb_url, os.environ['OSMS_DELETE_URL'])
supported_file_type = os.environ['OBS_TRANSFER_PREVIEW_SUPPORTED_FILE_FORMAT']

SUCCESS_STATUS_CODE = 200
MAX_TIMES = 3
# 超时时间：连接超时15秒，上传/读取超时300秒（5分钟）
connect_timeout = 15
read_timeout = 300

AI_TAIL_MD = "内容由AI生成"

DEFAULT_AIGC = """AIGC:
  Label: 1
  ContentProducer: 001191320114777023172010000
  ProduceID: voiceassistant-c4f456c6-3854
  ReservedCode1: {"SecurityData":{"Type":"TC260PG","Version":1,"Bindings":[{"Type":"Hash","AlgID":"2.16.840.1.101.3.4.2.1","Value":"a3004d79dddf82134b6149b5974efda6e838b4c3f610b7b801db56ba4c4808c7"}],"PrivSD":[],"PubSD":[{"AlgID":"1.2.840.113549.1.1.10","Type":"DS","TBSData":[{"Type":"Bnd"},{"Type":"Md"}],"Signature":"5adb678cbd61eec977d31364d9b00ab13168dc3a7875648a367b48a5eded38db86ca1be576619ddb26baa23f2bb05f2412d28774d6878e14572d74cb1dbc2b2022930bb76ce642638c52e4632fbe90344a589ad330c4a5d28cc98fb41152a822b2118e5ddda8a2530b0c7811fa5e306cfc6f4eb95a4c620f522848055fe45a3868fdee74c7138d26edda01ecf7c9d544b2c1e2cd3a43b60e1a595a8d4228707f1b0e47a0cf4a74d749922c00c4e6e0939b3d792c068ceb417b79aa981f296ebf86e933dc787d2e43b0f6a80243ed8a1677c404e89236fa895f7948943c7ab015db02b73d2a0be635df1527e26be44d08769b8d6e962992eb8b6728f373e9af6219f6543484c964820cdd5b91b8a530f5bc212df4edf75283f7039181259a65ab57e6f3d8369e92dc231106fe2aa2f7a3ab559d272b9018658130ef10fe9feca60a7d49fbaf597ddc26e4570a32ce0cf5e883f968dde23f6e3abd695565da03d11e63b71fe5753307a96d60e1c1bc616c22080c614e9d0ea4d0ff40c0a5154240"}]}}
  ContentPropagator: 001191320114777023172010000
  PropagateID: voiceassistant-c4f456c6-3854"""

AIGC_KEY_ORDER = (
    "Label",
    "ContentProducer",
    "ProduceID",
    "ReservedCode1",
    "ContentPropagator",
    "PropagateID",
)


def build_aigc_watermark_text(aigc: Mapping[str, Any]) -> str:
    lines = ["AIGC:"]
    seen = set()

    for k in AIGC_KEY_ORDER:
        if k in aigc and aigc[k] is not None:
            lines.append(f"  {k}: {aigc[k]}")
            seen.add(k)

    for k, v in aigc.items():
        if k not in seen and v is not None:
            lines.append(f"  {k}: {v}")

    return "\n".join(lines)


def format_md_hidden_watermark(wm: str) -> str:
    """
    Convert watermark text into hidden comment format for md.
    """
    text = wm or ""
    if not text:
        return ""
    return f"---\n{text}\n---"


async def concrete_file_res(file_path: str) -> dict | None:
    try:
        file_name = os.path.basename(file_path)
        file_path = os.path.join(get_task_dir(), file_name)
        path = pathlib.Path(file_path)
        if not path.exists():
            logger.info(f"file_name：{file_name}， file_path:{file_path}, path:{path} not exists")
            return None
        logger.info(f"path:{file_name}, 开始获取obs链接")

        backup_file = file_path + '.bak'
        # 拷贝没有添加水印的文件
        copy_file(file_path, backup_file)

        # 上传OBS文件增加水印

        file_detail_infos = await UploadFileOSMS().upload_file_and_get_info(file_path)

        # 上传完OBS后删除水印
        copy_file(backup_file, file_path)
        os.remove(backup_file)

        downloadUrl = file_detail_infos["url"]
        if not downloadUrl:
            logger.error(f"upload fail: downloadUrl is empty")
        logger.info(f"upload success: {file_name}")
        return {
            "name": file_name,
            "downloadUrl": file_detail_infos["url"],
            "fileSize": file_detail_infos["fileSize"],
            "filePath": file_path,
            "fileId": file_detail_infos["objectId"]
        }
    except Exception as e:
        logger.error(f"upload fail: Exception: {e}", exc_info=True)
        return None


class UploadFileOSMS(object):
    def __init__(self):
        self.trace_id = str(uuid.uuid4())[:16]
        self.isPermanentStoragePublic = False

    async def upload_file(self, file_path: str, record_file_path=r"obs_files_to_be_deleted.json",
                          isPermanentStoragePublic=False):
        try:
            self.isPermanentStoragePublic = isPermanentStoragePublic
            updload_file_info = await self.upload_file_and_get_info(file_path)
            logger.info(f"upload_file_info:{updload_file_info}")
            if isinstance(updload_file_info, str):
                return None
            return updload_file_info["url"]
        except Exception as e:
            logger.error(f"上传文件到osms失败:{e}", exc_info=True)

    async def upload_file_and_get_info(self, file_path: str, record_file_path=r"obs_files_to_be_deleted.json"):
        if not file_path:
            logger.error("文件路径为空")
            return ""

        # 验证文件存在性
        if not os.path.exists(file_path):
            logger.error(f"文件{file_path}不存在")
            return "文件不存在"

        # 验证是否为文件
        if not os.path.isfile(file_path):
            logger.error(f"{file_path}不是文件")
            return "路径不是文件"

        # 调取osms的prepare接口获取文件信息
        file_info = await self.invoking_osms_prepare(file_path)
        logger.info(f"待上传file_path: {file_path}")
        if not file_info:
            logger.error(f"{file_path}上传OSMS失败")
            return "{}:文件上传失败OSMS".format(os.path.basename(file_path))

        logger.info(f"file_info:{file_info}")

        # 基于osms微服务完成文件上传到obs桶
        result = await self.read_file_as_bytes(file_path)
        if not result["success"]:
            return "{}:{}".format(result["file_name"], result["error"])

        updload_file_info = await self.upload_file_to_obs(file_info["uploadInfos"][0], result["bytes"])
        if not updload_file_info:
            logger.error(f"{file_path}上传OBS失败")
            return "{}:上传文件到 obs 报错".format(os.path.basename(file_path))

        # 更新osms微服务中文件上传的信息
        await self.invoking_osms_complete(file_info)

        # 调取osms的query接口获取文件cdn下载链接
        fileDetailInfos = await self.invoking_osms_query(file_info)
        if not fileDetailInfos:
            logger.error(f"{file_path}获取osms的cdn链接失败")

        return fileDetailInfos

    async def format_date(self):
        time_stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        time_stamp = time_stamp[:len(time_stamp) - 3]
        return time_stamp

    async def generate_cipher_text(self, time_stamp):
        message = (time_stamp + hag_osms_ak).encode('utf-8')
        secret = hag_osms_sk.encode('utf-8')
        signature = hmac.new(secret, message, hashlib.sha256).hexdigest()
        return base64.b64encode(bytes.fromhex(signature)).decode('utf-8')

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
            logger.error(f"Read file as bytes fail:{e}", exc_info=True)
            result["error"] = f"读取失败: {str(e)}"

        return result

    async def upload_file_to_obs(self, file_info, file_bytes):
        retry_delay = 1  # 初始重试延迟(秒)
        retry_time = 1  # 当前块重试次数

        while True:
            try:
                response = requests.put(file_info["url"], data=file_bytes, headers=file_info["headers"],
                                        timeout=(connect_timeout, read_timeout))
                logger.info(f"response:{response}")
                response.raise_for_status()
                return True
            except Exception as e:
                logger.error(f"Upload file to obs fail:{e}", exc_info=True)
                retry_time += 1
                time.sleep(retry_delay * (2 ** retry_time))
                if retry_time > MAX_TIMES:
                    logger.error("{}上传文件到 obs 报错{}".format(file_info["url"], str(e)))
                    return False

    async def calculate_file_sha256(self, file_path):
        sha256 = hashlib.sha256()
        file_size = os.path.getsize(file_path)

        with open(file_path, 'rb') as f, tqdm(total=file_size, unit='B', unit_scale=True,
                                              desc="Calculating SHA256") as pbar:
            while chunk := f.read(4096):  # 分块4kb
                sha256.update(chunk)
                pbar.update(len(chunk))

        return sha256.hexdigest()

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

    async def invoking_osms_prepare(self, file_path):
        """ 上传 osms prepare """
        logger.info(f"Invoke the osms prepare interface, trace_id: {self.trace_id}")
        for times in range(0, MAX_TIMES):
            time_stamp, headers = await self.build_osms_headers()
            body = {
                "useEdge": project_config.obs_use_edge,
                "objectType": "COMMON_PRIVATE",
                "fileName": os.path.basename(file_path),
                "fileSha256": await self.calculate_file_sha256(file_path),
                "fileSize": os.path.getsize(file_path),
                "fileOwnerInfo": {
                    "uid": "superAgent",
                    "teamId": "superAgent"
                }
            }

            if self.isPermanentStoragePublic:  # 只用于保存结果件中的图片链接
                body["objectType"] = "COMMON_PUBLIC"

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url=osms_prepare_url, headers=headers, json=body, ssl=True) as response:
                        logger.info(f"Invoke prepare, post to {osms_prepare_url} response:{response}")
                        if response.status != SUCCESS_STATUS_CODE:
                            continue
                        resp = await response.json()
                        if 'objectId' not in resp.keys() \
                                or 'draftId' not in resp.keys() or 'uploadInfos' not in resp.keys():
                            error = "The hag osms prepare interface returns an exception"
                            raise RuntimeError(error)
                        if not resp["uploadInfos"]:
                            error = "The hag osms prepare interface uploadInfos returns is empty"
                            raise RuntimeError(error)
                        if 'url' not in resp["uploadInfos"][0].keys() or \
                                'headers' not in resp["uploadInfos"][0].keys():
                            error = ("The hag osms prepare interface url and headers for uploadInfos "
                                     "map returns is empty")
                            raise RuntimeError(error)

                        return resp
            except Exception as e:
                logger.error(f"Invoking osms prepare fail:{e}", exc_info=True)
                logger.error(
                    "{}st invoking Hag_OSMS_Prepare interface throws the exception: {}.".format(times + 1, str(e)))
                if times == MAX_TIMES - 1:
                    raise RuntimeError(f"osms prepare interface call failed after retry limit reached, "
                                       f"type: {type(e).__name__}", e) from e
        return {}

    async def invoking_osms_complete(self, file_info):
        for times in range(0, MAX_TIMES):
            time_stamp = await self.format_date()
            cipher_text = await self.generate_cipher_text(time_stamp)
            headers = {
                "Content-Type": "application/json",
                "x-access-key": hag_osms_ak,
                "x-sign": cipher_text,
                "x-ts": time_stamp,
            }

            body = {
                "objectId": file_info["objectId"],
                "draftId": file_info["draftId"],
            }

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url=osms_complete_url, headers=headers, json=body, ssl=True) as response:
                        if response.status != SUCCESS_STATUS_CODE:
                            continue
                        return True
            except Exception as e:
                logger.error(f"Invoking osms complete fail:{e}", exc_info=True)
                logger.error(
                    f"{times + 1}st invoking Hag_OSMS_Prepare interface throws the exception: {str(e)}.")
                if times == MAX_TIMES - 1:
                    raise RuntimeError(f"osms prepare interface call failed after retry limit reached, "
                                       f"type: {type(e).__name__}", e) from e
        return False

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
                file_path = file_info["uploadInfos"][0]["url"]
                if file_path:
                    file_extension = get_extension_split(str(file_path))
                    need_preview = (project_config.obs_need_preview and
                                    file_extension in supported_file_type)
                    body["objectInfos"][0]["needPreview"] = need_preview  # 是否开启可预览字段

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
                        "expireTime": project_config.obs_expire_time,  # 设置有效期
                    }
                ],
                "permissionMode": "ALL"
            }

            if file_path:
                file_extension = get_extension_split(str(file_path))
                need_preview = (project_config.obs_need_preview and
                                file_extension in supported_file_type)
                body["objectInfos"][0]["needPreview"] = need_preview  # 是否开启可预览字段

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url=osms_query_url, headers=headers, json=body, ssl=True) as response:
                        if response.status != SUCCESS_STATUS_CODE:
                            continue
                        resp = await response.json()
                        self.validate_osms_query_resp(resp)

                        result = resp["fileDetailInfos"][0]['url'] if file_path else resp["fileDetailInfos"][0]
                        if not result:
                            logger.error("The hag osms query interface cdn url for fileDetailInfos returns is empty!")
                            error = "the hag osms query interface cdn url for fileDetailInfos returns is empty!"
                            raise RuntimeError(error)

                        return result
            except Exception as e:
                logger.error(f"Invoking osms query by url fail:{e}", exc_info=True)
                logger.error("Invoking the hag osms query interface throws the exception: {}.".format(times))
                if times == MAX_TIMES - 1:
                    logger.info("三次重试过后仍旧失败！！！")
                    raise RuntimeError(f"osms query interface call failed after retry limit reached, "
                                       f"type: {type(e).__name__}", e) from e
        return result

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
                    return True
        except Exception as e:
            logger.error(f"Invoking osms delete fail:{e}", exc_info=True)
            logger.error("Failed to invoke the hag osms delete interface throws the exception: {}.".format(str(e)))
            return False
