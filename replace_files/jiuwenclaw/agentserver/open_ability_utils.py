# Copyright (c) Huawei Technologies, Co., Ltd. 2025. All rights reserved.
"""OpenAbility消息处理、鉴权工具类"""
import os
import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_SANDBOX_ID = None
_API_KEY = None
HOSTNAME = "HOSTNAME"
USE_SIDECAR = "USE_SIDECAR"


def generate_random_key():
    import secrets
    import string
    chars = string.ascii_uppercase + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(32))
    return f"SK-{random_part}"


async def oa_wait_connection_ack(ws: Any, timeout: float = 10.0) -> bool:
    """等待 OA 返回第一条建连成功消息。

    成功消息格式: {"code": "0", "desc": "success"}

    Returns:
        bool: 是否成功建立连接
    """
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        data = json.loads(raw)

        # 检查成功响应格式
        if data.get("code") == "0" and data.get("desc") == "success":
            logger.info("[AgentWebSocketServer] 收到 OpenAbility 建连成功响应: %s", data)
            return True
        else:
            logger.error("[AgentWebSocketServer] OpenAbility 建连失败，响应: %s", data)
            return False
    except Exception as e:
        logger.exception("[AgentWebSocketServer] 等待 OpenAbility 建连响应异常: %s", e)
        return False


def get_sandbox_init_data():
    """
    获取沙箱创建时 gw 持久化的 apikey 和 sandboxId
    """
    global _SANDBOX_ID, _API_KEY
    # 环境变量中获取沙箱Id
    sandbox_id = os.getenv(HOSTNAME)
    if sandbox_id:
        _SANDBOX_ID = sandbox_id
    # 是否开启伴生容器
    if (os.getenv(USE_SIDECAR) or "").lower() == "true":
        _API_KEY = generate_random_key()

    if _SANDBOX_ID and _API_KEY:
        return
    init_path: str = os.getenv("SANDBOX_INIT_DATA_PATH", "").strip()
    if not os.path.exists(init_path):
        logger.warning("[SandboxInitDataPath] 沙箱中不存在初始化数据路径：%s", init_path)
        return
    try:
        with open(init_path, "r", encoding="utf-8") as file:
            init_data = json.load(file)
            _SANDBOX_ID = init_data.get("sandboxId")
            _API_KEY = init_data.get("apiKey")
    except Exception as e:
        logger.error("[SandboxInitData] 初始化数据获取失败：%s", e)


def get_api_key():
    get_sandbox_init_data()
    return _API_KEY


def get_sandbox_id():
    get_sandbox_init_data()
    return _SANDBOX_ID


def init_oa_message(msg_type, data=None):
    if msg_type not in ["INIT", "HEARTBEAT", "MESSAGE"]:
        raise Exception(f"Invalid msg_type: '{msg_type}'. Allowed values: INIT, HEARTBEAT, MESSAGE")
    data = data or {}
    return {
        "msgType": msg_type,
        "sessionId": data.get("service_id"),
        "taskId": data.get("request_id"),
        "msgDetail": "{}" if not data else json.dumps(data, ensure_ascii=False),
    }


def get_oa_auth_headers(
        retry_interval: float = 3.0, session_id: str = ""
) -> dict[str, str]:
    """从环境变量获取 OA 鉴权 headers，无限轮询直到获取全。

    注意：此函数会阻塞直到获取到所有必需的鉴权信息。

    支持的配置：
    - x-api-key
    - x-sandbox-id
    - x-request-from
    - x-hag-trace-id

    Args:
        retry_interval: 重试间隔秒数，默认3秒
        session_id: 默认 sandbox_id

    Returns:
        包含鉴权信息的 headers 字典
    """
    headers: dict[str, str] = {}
    attempt = 0

    while True:
        api_key = get_api_key()
        sandbox_id = get_sandbox_id()

        if api_key and sandbox_id:
            # 获取到所有必需的 headers
            headers["x-api-key"] = api_key
            headers["x-sandbox-id"] = sandbox_id
            headers["x-request-from"] = "jiuwenclaw"
            headers["x-hag-trace-id"] = session_id if session_id else sandbox_id
            if attempt > 0:
                logger.info(
                    "[AgentWebSocketServer] 成功获取 OpenAbility 鉴权 headers (等待 %d 秒)",
                    attempt * retry_interval
                )
            return headers

        # 记录缺少的配置
        missing = []
        if not api_key:
            missing.append("x-api-key")
        if not sandbox_id:
            missing.append("x-sandbox-id")

        if attempt == 0:
            logger.warning(
                "[AgentWebSocketServer] OpenAbility 鉴权信息不完整，开始轮询等待: %s",
                ", ".join(missing)
            )
        else:
            # 每10秒记录一次日志
            if attempt % 10 == 0:
                logger.info(
                    "[AgentWebSocketServer] 等待 OpenAbility 鉴权信息中 (%d 秒): %s",
                    attempt * retry_interval,
                    ", ".join(missing)
                )

        # 等待后重试
        time.sleep(retry_interval)
        attempt += 1
