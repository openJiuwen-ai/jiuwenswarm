# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Plugin invoke transport: direct mcp/run (businessCredential handshake)."""

from __future__ import annotations

import os
import re
import uuid
from typing import Any
from urllib.parse import urlparse

# Direct plugin WS: full URL, do not concatenate.
# Example: wss://host:18449/agent-runtime-service-ws/v1/mcp/run
_MCP_RUN_ENV = "AGENT_RUNTIME_MCP_RUN"
_MCP_UPSTREAM_ENV = "AGENT_RUNTIME_MCP_UPSTREAM"
_AGENT_BASE_ENV = "AGENT_RUNTIME_BASEURL"
_UID_ENV = "AGENT_RUNTIME_UID"
_CLAW_UID_ENV = "CLAW_XIAOYI_UID"
_CREDENTIAL_ENV = "CLAW_BUSINESS_CREDENTIAL"
_DEVICE_ID_ENVS = ("AGENT_RUNTIME_DEVICE_ID", "X_DEVICE_ID")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}
_SECRET_HEADER_RE = re.compile(r"credential|authorization|token", re.IGNORECASE)


def is_mcp_run_url(url: str) -> bool:
    """True when URL is the Runtime mcp/run WS (including agent-runtime-service-ws)."""
    return "/mcp/run" in (url or "").rstrip("/")


def mask_secret(value: str | None) -> str:
    """Match desktop logger.maskSecret: empty → (空); else first 12 chars + …(len=N)."""
    text = "" if value is None else str(value)
    if not text:
        return "(空)"
    return f"{text[:12]}…(len={len(text)})"


def format_masked_headers(headers: Any) -> str:
    """Flatten handshake/response headers, masking credential/authorization/token values."""
    items: list[Any]
    if headers is None:
        return ""
    try:
        items = list(headers.items())
    except Exception:  # noqa: BLE001
        if isinstance(headers, (list, tuple)):
            items = list(headers)
        else:
            return ""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        name = str(item[0])
        value = str(item[1])
        if _SECRET_HEADER_RE.search(name):
            value = mask_secret(value)
        parts.append(f"{name}={value}")
    return " ".join(parts)


def handshake_reject_status_and_headers(exc: BaseException) -> tuple[int | None, Any]:
    """Extract InvalidStatusCode / InvalidStatus status and headers from a handshake error."""
    status = getattr(exc, "status_code", None)
    headers = getattr(exc, "headers", None)
    response = getattr(exc, "response", None)
    if response is not None:
        if status is None:
            status = getattr(response, "status_code", None)
        if headers is None:
            headers = getattr(response, "headers", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    return status_int, headers


def is_handshake_reject(exc: BaseException) -> bool:
    """True when the exception is an HTTP handshake rejection (non-101)."""
    name = type(exc).__name__
    if "InvalidStatus" in name:
        return True
    status, _headers = handshake_reject_status_and_headers(exc)
    return status is not None and "handshake" in name.lower()


def is_desktop_plugin_ws_proxy(url: str | None = None) -> bool:
    """True when mcp/run points at the desktop loopback inject proxy (127.0.0.1:19694)."""
    raw = (url if url is not None else resolve_plugin_runtime_url()).strip()
    if not is_mcp_run_url(raw):
        return False
    try:
        host = (urlparse(raw).hostname or "").lower().strip("[]")
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


def resolve_plugin_runtime_url() -> str:
    """Return AGENT_RUNTIME_MCP_RUN (full URL, no path concat). Empty if unset."""
    return (os.environ.get(_MCP_RUN_ENV) or "").strip()


def resolve_plugin_runtime_upstream_url() -> str:
    """Return AGENT_RUNTIME_MCP_UPSTREAM when set; else AGENT_RUNTIME_MCP_RUN.

    Desktop spawn points MCP_RUN at the loopback inject proxy and puts the real
    现网/蓝绿 URL in MCP_UPSTREAM so zone helpers can still tell them apart.
    """
    upstream = (os.environ.get(_MCP_UPSTREAM_ENV) or "").strip()
    return upstream or resolve_plugin_runtime_url()


def resolve_agent_runtime_baseurl() -> str:
    """Return Agent Runtime base URL if set (agent_as_a_tool path)."""
    return (os.environ.get(_AGENT_BASE_ENV) or "").strip()


def _xiaoyi_channel() -> dict[str, Any]:
    try:
        from jiuwenswarm.common.config import get_config

        channels = get_config().get("channels") or {}
        xiaoyi = channels.get("xiaoyi") or {}
        return xiaoyi if isinstance(xiaoyi, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def resolve_runtime_uid() -> str:
    for key in (_CLAW_UID_ENV, _UID_ENV):
        env_uid = (os.environ.get(key) or "").strip()
        if env_uid:
            return env_uid
    return str(_xiaoyi_channel().get("uid") or "").strip()


def resolve_business_credential() -> str:
    """Product mcp/run handshake credential (lab / old packets).

    桌面目标态不再经密钥包下发 businessCredential：mcp/run 打本机代理，
    由主进程按次注入 locker 票。本函数仍读 env / 旧密钥包，供实验室直连
    远端 mcp/run。env（CLAW_BUSINESS_CREDENTIAL）仅为实验室/旧形态兜底。
    """
    env_value = (os.environ.get(_CREDENTIAL_ENV) or "").strip()
    if env_value:
        return env_value
    try:
        from jiuwenswarm.common.secrets_bootstrap import get_secret
    except Exception:  # noqa: BLE001
        return ""
    value = get_secret("businessCredential")
    return str(value).strip() if value else ""


def resolve_plugin_ws_token() -> str:
    """Desktop plugin WS proxy token from stdin secrets (pluginWsToken)."""
    try:
        from jiuwenswarm.common.secrets_bootstrap import get_secret
    except Exception:  # noqa: BLE001
        return ""
    value = get_secret("pluginWsToken")
    return str(value).strip() if value else ""


def handshake_cred_source(url: str | None = None) -> str:
    """Where the mcp/run handshake cred is expected to come from (log field credSrc)."""
    if is_desktop_plugin_ws_proxy(url):
        return "desktop-proxy"
    if (os.environ.get(_CREDENTIAL_ENV) or "").strip():
        return "env"
    try:
        from jiuwenswarm.common.secrets_bootstrap import get_secret
    except Exception:  # noqa: BLE001
        return "empty"
    value = get_secret("businessCredential")
    if str(value).strip() if value else "":
        return "vault"
    return "empty"


def resolve_runtime_device_id() -> str:
    for key in _DEVICE_ID_ENVS:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    try:
        from jiuwenswarm.common.invocation_context.runtime import get_current_invocation_context
        from jiuwenswarm.server.xiaoyi_invocation import get_xiaoyi_invocation_extension

        invocation = get_current_invocation_context()
        if invocation is None:
            return ""
        extension = get_xiaoyi_invocation_extension(invocation)
        return str((extension.device_id if extension else None) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def resolve_device_hostname() -> str:
    """PC hostname from desktop getDeviceInfo (CLAW_DEVICE_HOSTNAME)."""
    return (os.environ.get("CLAW_DEVICE_HOSTNAME") or "").strip()


def resolve_device_sandbox_system() -> str:
    """OS label from desktop getDeviceInfo (windows/macos/…)."""
    return (os.environ.get("CLAW_DEVICE_SANDBOX_SYSTEM") or "").strip()


def build_product_mcp_headers(
    *,
    plugin_session_id: str = "",
    extra: dict[str, str] | None = None,
    url: str | None = None,
) -> dict[str, str]:
    """Handshake headers for mcp/run.

    Desktop loopback proxy: Authorization Bearer pluginWsToken, no businessCredential
    (desktop injects the locker ticket on the upstream hop). Remote/lab: businessCredential.
    """
    target = (url if url is not None else resolve_plugin_runtime_url()).strip()
    uid = resolve_runtime_uid()
    device_id = resolve_runtime_device_id()
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "x-request-from": "xiaoyiWork",
        "x-hag-trace-id": uuid.uuid4().hex,
    }
    if is_desktop_plugin_ws_proxy(target):
        token = resolve_plugin_ws_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    else:
        headers["businessCredential"] = resolve_business_credential()
    if uid:
        headers["x-uid"] = uid
    if device_id:
        headers["x-device-id"] = device_id
    if plugin_session_id:
        headers["x-plugin-session-id"] = plugin_session_id
    if extra:
        headers.update({k: v for k, v in extra.items() if v})
    if is_desktop_plugin_ws_proxy(target):
        headers.pop("businessCredential", None)
        headers.pop("businesscredential", None)
    return headers


def build_runtime_headers(*, extra: dict[str, str] | None = None, url: str | None = None) -> dict[str, str]:
    """Handshake headers for mcp/run: proxy token or businessCredential + uid/device/trace."""
    plugin_session_id = ""
    if extra:
        plugin_session_id = str(extra.get("x-plugin-session-id") or "")
    return build_product_mcp_headers(plugin_session_id=plugin_session_id, extra=extra, url=url)


def build_plugin_skill_extra_info(
    *,
    session_id: str | None = None,
    interaction_id: int = 0,
) -> dict[str, Any]:
    """Build extraInfo for PluginSkillExec mcp/run.

    uid 与握手 x-uid 同源（CLAW_XIAOYI_UID / AGENT_RUNTIME_UID / 渠道 uid）。
    有桌面 CLAW_DEVICE_* / device id 时用桌面设备信息；否则用 PC 缺省（sandbox_pc）。
    """
    uid = resolve_runtime_uid()
    device_id = resolve_runtime_device_id()
    hostname = resolve_device_hostname()
    sandbox_system = resolve_device_sandbox_system()

    resolved_session = session_id or ""
    try:
        from jiuwenswarm.common.invocation_context.runtime import get_current_invocation_context
        from jiuwenswarm.server.xiaoyi_invocation import get_xiaoyi_invocation_extension

        invocation = get_current_invocation_context()
        if invocation is not None:
            extension = get_xiaoyi_invocation_extension(invocation)
            resolved_session = (
                (extension.root_session_id if extension else None)
                or (extension.params_session_id if extension else None)
                or session_id
                or invocation.session_id
                or ""
            )
            if not device_id:
                device_id = str((extension.device_id if extension else None) or "")
            if not interaction_id and invocation.trace and invocation.trace.interaction_id:
                try:
                    interaction_id = int(str(invocation.trace.interaction_id).strip() or "0")
                except ValueError:
                    interaction_id = 0
    except Exception:  # noqa: BLE001
        pass

    device_info = {
        "deviceName": hostname or "sandbox_pc",
        "ohosApiVersion": 0,
        "romVersion": "",
        "sysVersion": sandbox_system or "",
        "x-device-id": device_id,
        "x-device-type": sandbox_system or "pc",
    }
    session_device_id = device_id

    return {
        "context": {
            "deviceInfo": device_info,
            "userInfo": {
                "uid": uid,
            },
        },
        "session": {
            "sessionId": str(resolved_session),
            "interactionId": int(interaction_id or 0),
            "deviceId": session_device_id,
        },
    }


def build_cloud_plugin_context(
    *,
    session_id: str | None = None,
    agent_id: str | None = None,
) -> Any:
    """Compatibility wrapper: return CloudPluginContext when possible."""
    _ = agent_id
    from jiuwenswarm.agents.harness.common.tools.invoke_meta.cloud_plugin_client import (
        CloudPluginContext,
    )

    extra = build_plugin_skill_extra_info(session_id=session_id)
    session = extra.get("session") or {}
    device = (extra.get("context") or {}).get("deviceInfo") or {}
    return CloudPluginContext(
        session_id=str(session.get("sessionId") or ""),
        interaction_id=int(session.get("interactionId") or 0),
        device_id=str(session.get("deviceId") or ""),
        device_name=str(device.get("deviceName") or ""),
        device_type=str(device.get("x-device-type") or ""),
        sys_version=str(device.get("sysVersion") or ""),
    )


def missing_plugin_url_error(*, plugin_id: str = "", tool_name: str = "") -> dict[str, Any]:
    return {
        "success": False,
        "error": (
            "缺少插件 WS 地址：需 AGENT_RUNTIME_MCP_RUN（桌面 spawn 注入，或实验室写入环境）"
        ),
        "pluginId": plugin_id,
        "toolName": tool_name,
    }


def missing_credential_error(*, plugin_id: str = "", tool_name: str = "") -> dict[str, Any]:
    return {
        "success": False,
        "error": (
            "缺少插件握手凭证：密钥包缺少 businessCredential"
            "（桌面登录后 spawn 下发，或实验室写入 CLAW_BUSINESS_CREDENTIAL）"
        ),
        "pluginId": plugin_id,
        "toolName": tool_name,
    }


def missing_plugin_ws_token_error(*, plugin_id: str = "", tool_name: str = "") -> dict[str, Any]:
    return {
        "success": False,
        "error": (
            "缺少插件 WS 代理令牌：密钥包缺少 pluginWsToken（桌面 spawn 下发）"
        ),
        "pluginId": plugin_id,
        "toolName": tool_name,
    }


def missing_agent_baseurl_error() -> dict[str, Any]:
    return {
        "success": False,
        "error": (
            "缺少 Agent Runtime 地址：请配置环境变量 AGENT_RUNTIME_BASEURL"
        ),
    }


__all__ = [
    "build_cloud_plugin_context",
    "build_product_mcp_headers",
    "build_plugin_skill_extra_info",
    "build_runtime_headers",
    "format_masked_headers",
    "handshake_cred_source",
    "handshake_reject_status_and_headers",
    "is_desktop_plugin_ws_proxy",
    "is_handshake_reject",
    "is_mcp_run_url",
    "mask_secret",
    "missing_agent_baseurl_error",
    "missing_credential_error",
    "missing_plugin_url_error",
    "missing_plugin_ws_token_error",
    "resolve_agent_runtime_baseurl",
    "resolve_business_credential",
    "resolve_device_hostname",
    "resolve_device_sandbox_system",
    "resolve_plugin_runtime_upstream_url",
    "resolve_plugin_runtime_url",
    "resolve_plugin_ws_token",
    "resolve_runtime_device_id",
    "resolve_runtime_uid",
]
