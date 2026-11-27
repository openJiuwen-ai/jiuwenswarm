# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Action executor API client for user-uploaded external tools (REST / WebSocket gateway)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_DEFAULT_ACCESS_KEY = "10001"
_DEFAULT_TIMEOUT = 30


def _ensure_env_loaded() -> None:
    try:
        from jiuwenclaw.utils import get_env_file

        env_path = get_env_file()
    except Exception:
        env_path = Path(__file__).resolve().parents[3] / "config" / ".env"
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path)


def generate_timestamp() -> str:
    """Generate timestamp: yyyyMMddHHmmssSSS (UTC)."""
    now = datetime.now(timezone.utc)
    base_time = now.strftime("%Y%m%d%H%M%S")
    milliseconds = f"{now.microsecond // 1000:03d}"
    return base_time + milliseconds


def generate_signature(timestamp: str, access_key: str, secret_key: str) -> str:
    message = timestamp + access_key
    signature = hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    signature_bytes = bytes.fromhex(signature)
    return base64.b64encode(signature_bytes).decode("utf-8")


def _build_request_body(
    plugin_id: str,
    action_name: str,
    params: dict[str, Any],
    *,
    session_id: str = "sessionId002",
    interaction_id: str = "interactionId001",
) -> dict[str, Any]:
    return {
        "session": {
            "sessionId": session_id,
            "isNew": True,
            "interactionId": interaction_id,
        },
        "endpoint": {
            "countryCode": "CN",
            "locale": "zh-CN",
            "device": {
                "deviceType": 0,
                "phoneType": "F810",
                "city": {"province": "江苏", "city": "南京"},
                "timezone": "",
                "sysVer": "8.0.0",
                "deviceId": "1231",
                "manufacturer": "HUAWEI",
                "sysDisplayVer": "1.1.1.1",
                "prdVer": "4.0.0.0",
                "ohosVersion": "",
                "net": "4G",
                "ohosApiVersion": "",
            },
        },
        "version": "V4",
        "actions": [
            {
                "actionSn": "1",
                "actionExecutorTask": {
                    "pluginId": plugin_id,
                    "realMachineTest": "1",
                    "content": params,
                    "actionName": action_name,
                },
            }
        ],
        "utterance": {"original": ""},
    }


def call_action_executor_api(
    plugin_id: str,
    action_name: str,
    params: dict[str, Any],
    *,
    protocol: str = "REST",
    session_id: str = "sessionId002",
    interaction_id: str = "interactionId001",
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Invoke the action executor gateway and return a structured result dict."""
    _ensure_env_loaded()
    secret_key = os.getenv("PLUGIN_SECRET_KEY") or ""
    if not secret_key:
        return {
            "success": False,
            "error": "缺少环境变量 PLUGIN_SECRET_KEY，无法调用外部工具",
        }

    proto = (protocol or "REST").upper()
    if proto == "REST":
        url = os.getenv("PLUGIN_RESTFUL_URL") or ""
        env_hint = "PLUGIN_RESTFUL_URL"
    else:
        url = os.getenv("PLUGIN_WS_URL") or ""
        env_hint = "PLUGIN_WS_URL"

    if not url:
        return {
            "success": False,
            "error": f"缺少环境变量 {env_hint}，无法调用外部工具（protocol={proto}）",
        }

    access_key = _DEFAULT_ACCESS_KEY
    timestamp = generate_timestamp()
    signature = generate_signature(timestamp, access_key, secret_key)
    headers = {
        "Content-Type": "application/json",
        "x-access-key": access_key,
        "x-sign": signature,
        "x-ts": timestamp,
        "x-hag-trace-id": "rytest99527",
        "x-prd-pkg-name": "com.huawei.vassistant",
    }
    body = _build_request_body(
        plugin_id,
        action_name,
        params,
        session_id=session_id,
        interaction_id=interaction_id,
    )

    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.post(
            url,
            headers=headers,
            json=body,
            verify=False,
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        return {"success": False, "error": "请求超时"}
    except requests.exceptions.RequestException as exc:
        logger.warning("[action_executor] request failed: %s", exc)
        return {"success": False, "error": f"请求错误: {exc}"}

    result: dict[str, Any] = {
        "success": response.status_code == 200,
        "status_code": response.status_code,
        "tool_name": action_name,
        "plugin_id": plugin_id,
    }
    try:
        result["data"] = response.json()
    except Exception:
        result["data"] = response.text
    if response.status_code != 200:
        result["error"] = f"HTTP {response.status_code}"
    return result


def format_call_result_for_model(result: dict[str, Any]) -> str:
    try:
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:
        return str(result)
