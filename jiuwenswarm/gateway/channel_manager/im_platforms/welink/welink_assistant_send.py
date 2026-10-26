# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Send a group message as the configured assistant. The employee CLI stays read-only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import platform
import time
import uuid
from typing import Any, Callable

ASSISTANT_WS_URL = "ws://api.inner.welink.huawei.com/appstore-gateway/ws/agent"
ASSISTANT_TOOL_TYPE = "GTS DevMind"

Connect = Callable[..., Any]


class AssistantSendError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def build_auth_protocol(credential_id: str, secret: str, *, ts: str, nonce: str) -> str:
    sign = base64.b64encode(
        hmac.new(secret.encode(), f"{credential_id}{ts}{nonce}".encode(), hashlib.sha256).digest()
    ).decode()
    raw = base64.b64encode(
        json.dumps(
            {"ak": credential_id, "ts": ts, "nonce": nonce, "sign": sign},
            separators=(",", ":"),
        ).encode()
    ).decode()
    token = raw.replace("+", "-").replace("/", "_").rstrip("=")
    return f"auth.{token}"


def build_register_frame() -> dict[str, str]:
    return {
        "type": "register",
        "deviceName": "JiuwenSwarm",
        "os": platform.system() or "Windows",
        "toolType": ASSISTANT_TOOL_TYPE,
        "toolVersion": "1.0.0",
        "pluginVersion": "1.0.0",
        "sdkVersion": "1.0.0",
    }


def build_group_frames(
    group_id: str,
    text: str,
    *,
    suffix: str,
    trace_id: str,
    push_type: str = "active_push",
    tool_session_id: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    tool_session_id = tool_session_id.strip() or f"ses_{suffix}"
    message_id = f"msg_notify_{suffix}"
    part_id = f"prt_notify_{suffix}"
    envelope = {
        "type": "tool_event",
        "toolSessionId": tool_session_id,
        "traceId": trace_id,
        "businessPushType": push_type,
        "businessSessionDomain": "im",
        "businessType": "group",
        "businessId": group_id,
    }
    events = [
        {"protocol": "cloud", "type": "step.start", "properties": {"messageId": message_id}},
        {
            "protocol": "cloud",
            "type": "text.done",
            "properties": {"messageId": message_id, "partId": part_id, "content": text},
        },
        {"protocol": "cloud", "type": "step.done", "properties": {"messageId": message_id}},
    ]
    frames: list[dict[str, Any]] = [{**envelope, "event": event} for event in events]
    frames.append(
        {
            "type": "tool_done",
            "toolSessionId": tool_session_id,
            "traceId": trace_id,
            "businessPushType": push_type,
        }
    )
    return message_id, frames


def push_group_text(
    credential_id: str,
    secret: str,
    group_id: str,
    text: str,
    *,
    connect: Connect | None = None,
    timeout: float = 20,
) -> str:
    """Register the assistant and push one group text. Never uses the employee account."""
    if not credential_id.strip() or not secret.strip() or not group_id.strip() or not text:
        raise AssistantSendError("missing_assistant_credential")
    opener = connect or _open_socket
    ts = str(int(time.time()))
    nonce = str(uuid.uuid4())
    protocol = build_auth_protocol(credential_id, secret, ts=ts, nonce=nonce)
    try:
        socket = opener(ASSISTANT_WS_URL, subprotocols=[protocol], timeout=timeout)
    except AssistantSendError:
        raise
    except Exception as exc:
        raise AssistantSendError(type(exc).__name__) from None
    try:
        socket.send(json.dumps(build_register_frame(), separators=(",", ":")))
        registered = _read_control(socket, timeout, accept={"register_ok"}, reject={"register_rejected"})
        if registered is None:
            raise AssistantSendError("register_timeout")
        if registered.get("type") == "register_rejected":
            raise AssistantSendError(str(registered.get("reason") or "register_rejected"))
        suffix = uuid.uuid4().hex[:12]
        message_id, frames = build_group_frames(group_id, text, suffix=suffix, trace_id=uuid.uuid4().hex)
        for frame in frames:
            socket.send(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
        finished = _read_control(socket, 2, accept={"tool_done"}, reject={"tool_error", "register_rejected"})
        if finished is not None and finished.get("type") in {"tool_error", "register_rejected"}:
            raise AssistantSendError(str(finished.get("type")))
        return message_id
    except AssistantSendError:
        raise
    except Exception as exc:
        raise AssistantSendError(type(exc).__name__) from None
    finally:
        close = getattr(socket, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _open_socket(url: str, *, subprotocols: list[str], timeout: float) -> Any:
    import websocket

    return websocket.create_connection(url, subprotocols=subprotocols, timeout=timeout, suppress_origin=True)


def _read_control(socket: Any, timeout: float, *, accept: set[str], reject: set[str]) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(deadline - time.monotonic(), 0.1)
        settimeout = getattr(socket, "settimeout", None)
        if callable(settimeout):
            settimeout(remaining)
        try:
            raw = socket.recv()
        except Exception as exc:
            if exc.__class__.__name__ in {"WebSocketTimeoutException", "TimeoutError"} or isinstance(exc, TimeoutError):
                return None
            raise
        if not isinstance(raw, str) or not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        kind = str(payload.get("type") or "")
        if kind in accept or kind in reject:
            return payload
    return None
