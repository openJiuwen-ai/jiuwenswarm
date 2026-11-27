# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""XiaoYi Push Message Service - 主动推送消息服务."""

import logging
import base64
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp


logger = logging.getLogger(__name__)

PUSH_URL = "https://hag.cloud.huawei.com/open-ability-agent/v1/agent-webhook"


@dataclass
class PushConfig:
    """Push 消息配置."""
    mode: str = ""
    api_id: str = ""
    push_id: str = ""
    ak: str = ""
    sk: str = ""
    uid: str = ""
    api_key: str = ""
    push_url: str = ""


class XiaoYiPushService:
    """
    华为小艺主动推送服务.
    通过 HTTP Webhook API 向用户设备发送推送通知.
    """
    def __init__(self, config: PushConfig):
        self.config = config

    @staticmethod
    def _generate_uuid() -> str:
        """生成 UUID."""
        return str(uuid.uuid4())

    def _generate_signature(self, timestamp: str) -> str:
        """生成 HMAC-SHA256 签名 (Base64 编码)."""
        h = hmac.new(
            self.config.sk.encode("utf-8"),
            timestamp.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(h.digest()).decode("utf-8")

    async def send_push(self, text: str, push_text: str) -> bool:
        """
        发送推送通知.

        Args:
            text: 摘要文本 (如前30个字符)
            push_text: 推送通知文本 (如"任务已完成：xxx...")

        Returns:
            bool: 是否发送成功
        """

        try:
            timestamp = str(int(time.time() * 1000))
            message_id = self._generate_uuid()

            payload = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "id": self._generate_uuid(),
                    "apiId": self.config.api_id,
                    "pushId": self.config.push_id,
                    "pushText": text,
                    "kind": "task",
                    "artifacts": [{
                        "artifactId": self._generate_uuid(),
                        "parts": [{
                            "kind": "text",
                            "text": push_text,
                        }]
                    }],
                    "status": {"state": "completed"}
                }
            }

            logger.info(f"[PUSH] Sending push notification: {push_text}")
            if self.config.mode == "xiaoyi_claw":
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-hag-trace-id": self._generate_uuid(),
                    "x-uid": self.config.uid,
                    "x-api-key": self.config.api_key,
                    "x-request-from": "openclaw"
                } 
            else:
                signature = self._generate_signature(timestamp)
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "x-hag-trace-id": self._generate_uuid(),
                    "X-Access-Key": self.config.ak,
                    "X-Sign": signature,
                    "X-Ts": timestamp,
                }
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.config.push_url or PUSH_URL,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                ) as response:
                    if response.status == 200:
                        logger.info("[PUSH] Push notification sent successfully")
                        return True
                    else:
                        logger.error(f"[PUSH] Failed: HTTP {response.status}")
                        return False

        except aiohttp.ClientError as e:
            logger.error(f"[PUSH] Network error: {e}")
            return False
        except Exception as e:
            logger.error(f"[PUSH] Error: {e}")
            return False
