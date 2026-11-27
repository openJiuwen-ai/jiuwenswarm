# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AK/SK 鉴权模块 — VibeSkillChannel HTTP & WebSocket 通用鉴权。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from jiuwenclaw.gateway.status_code import StatusCode

logger = logging.getLogger(__name__)

# 尝试导入 JiuWenBaseException，如果不存在则用标准异常
try:
    from jiuwen.common.exception import JiuWenBaseException
except ImportError:
    JiuWenBaseException = Exception  # type: ignore

# Header/Query Key 常量

X_TS_KEY = "x-ts"
X_SIGN_KEY = "x-sign"
X_AK_KEY = "x-ak"


def is_valid_ts(ts: str) -> bool:
    """校验时间戳是否在当前时间 ±5 分钟内。

    Args:
        ts: 时间戳字符串（秒级整数）。

    Returns:
        True 如果在 5 分钟窗口内，否则 False。
    """
    if not ts.isdigit() or len(ts) < 10:
        return False
    ts = ts[:10] if len(ts) > 10 else ts
    now = datetime.now(timezone.utc)
    ts_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    time_difference = abs(now - ts_dt)
    return time_difference <= timedelta(minutes=5)


def get_sign_from_sk(string_to_sign: str) -> str:
    """使用 SK 对 string_to_sign 生成 HMAC-SHA256 签名。

    Args:
        string_to_sign: 待签名字符串。

    Returns:
        Base64 编码的签名字符串。
    """
    from jiuwenclaw.sts_service import sts_config
    config_name = (
        f"MicroService/{os.environ['STS_SERVICE']}/"
        f"{os.environ['STS_MICROSERVICE']}/{os.environ['HTTP_WS_AUTH_STS_SK_NAME']}/"
        f"{os.environ['HTTP_WS_AUTH_STS_SK_TAG']}"
    )
    origin_sk = sts_config.get_sts_config(config_name)
    hmac_signature = hmac.new(
        origin_sk.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    )
    return base64.b64encode(hmac_signature.digest()).decode("utf-8")


def is_valide_sign(ts: str, ak: str, sign: str) -> bool:
    """校验 AK/SK 签名是否合法。

    Args:
        ts: 时间戳字符串。
        ak: Access Key。
        sign: 请求签名。

    Returns:
        True 签名合法，否则 False。
    """
    logger.info("[Auth] is_valide_sign: ts=%s, ak=%s", ts, ak)
    if not is_valid_ts(ts):
        logger.error("[Auth] is_valide_sign fail: ts invalid")
        return False

    string_to_sign = f"{ak}_{ts}"
    logger.info("[Auth] string_to_sign=%s", string_to_sign)

    try:
        signd = get_sign_from_sk(string_to_sign)
    except JiuWenBaseException as e:  # type: ignore
        logger.error("[Auth] is_valide_sign fail: get_sign error=%s", getattr(e, "message", str(e)))
        return False

    if sign != signd:
        logger.error("[Auth] is_valide_sign fail: sign mismatch")
        return False
    logger.info("[Auth] is_valide_sign ok")
    return True


def check_auth(
        auth_enabled: bool,
        ak: str,
        ts: str,
        sign: str,
) -> tuple[bool, str]:
    """鉴权入口函数，供 HTTP/WS 共用。

    Args:
        auth_enabled: 是否开启鉴权。
        ak: Access Key。
        ts: 时间戳。
        sign: 签名。

    Returns:
        (ok, error_message) — ok=True 表示鉴权通过。
    """
    logger.info("[Auth] check_auth: auth_enabled=%s, ak=%s, ts=%s", auth_enabled, ak, ts)
    if not auth_enabled:
        return True, ""

    if not ak or not ts or not sign:
        logger.warning("[Auth] check_auth fail: missing params")
        return False, StatusCode.AUTHENTICATION_ERROR.value[1]

    if is_valide_sign(ts, ak, sign):
        logger.info("[Auth] check_auth ok")
        return True, ""
    logger.warning("[Auth] check_auth fail: sign invalid")
    return False, StatusCode.AUTHENTICATION_ERROR.value[1]


def check_ws_auth(auth_enabled: bool, headers: dict) -> tuple[bool, str]:
    """WebSocket 鉴权，从 headers 提取参数后调 check_auth。

    Args:
        auth_enabled: 是否开启鉴权。
        headers: HTTP headers 字典。

    Returns:
        (ok, error_message)
    """
    logger.info("[Auth] check_ws_auth: headers=%s", headers)
    ts = headers.get(X_TS_KEY, "")
    sign = headers.get(X_SIGN_KEY, "")
    ak = os.environ.get("HTTP_WS_AUTH_STS_AK", "")
    logger.info("[Auth] check_ws_auth: ts=%s, sign=%s, ak=%s", ts, sign, ak)
    return check_auth(auth_enabled, ak, ts, sign)


def check_http_auth(auth_enabled: bool, headers: dict) -> tuple[bool, str]:
    """HTTP 鉴权，从 headers 提取参数后调 check_auth。

    Args:
        auth_enabled: 是否开启鉴权。
        headers: HTTP headers 字典。

    Returns:
        (ok, error_message)
    """
    ts = headers.get(X_TS_KEY, "")
    sign = headers.get(X_SIGN_KEY, "")
    ak = os.environ.get("HTTP_WS_AUTH_STS_AK", "")
    return check_auth(auth_enabled, ak, ts, sign)
