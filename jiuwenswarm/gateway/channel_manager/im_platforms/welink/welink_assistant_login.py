# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WeLink assistant QR login. The AK/SK is issued by WeLink, then stored for one colleague."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib import error, request

logger = logging.getLogger(__name__)

ASSISTANT_AUTH_API = "https://api.inner.welink.huawei.com/assistant-api/nologin/we-crew/im-register"
# WeLink currently accepts this channel label for the assistant QR.
ASSISTANT_CHANNEL = "GTS DevMind"

Fetch = Callable[[str, str, dict[str, str], bytes | None], tuple[int, Any]]


def mac_address() -> str:
    node = uuid.getnode()
    parts = [f"{(node >> shift) & 0xFF:02X}" for shift in range(40, -1, -8)]
    mac = ":".join(parts)
    if mac == "00:00:00:00:00:00":
        return ""
    return mac


def default_fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, Any]:
    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"message": exc.reason}
        return exc.code, payload


@dataclass
class AssistantLoginSession:
    session_id: str
    agent_id: str
    account: str
    status: str = "pending"
    qr_payload: str = ""
    error: str = ""
    saved: bool = False
    _qrcode: str = field(default="", repr=False)
    _access_token: str = field(default="", repr=False)
    _credential_id: str = field(default="", repr=False)
    _secret: str = field(default="", repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "status": self.status,
                "qr_payload": self.qr_payload,
                "error": self.error,
                "account": self.account,
                "kind": "assistant",
            }

    def credentials(self) -> tuple[str, str]:
        with self._lock:
            return self._credential_id, self._secret


class AssistantLoginManager:
    def __init__(self, fetch: Fetch | None = None) -> None:
        self._fetch = fetch or default_fetch
        self._sessions: dict[str, AssistantLoginSession] = {}
        self._lock = threading.Lock()

    def start(self, *, agent_id: str, account: str) -> AssistantLoginSession:
        with self._lock:
            for existing in list(self._sessions.values()):
                if existing.agent_id == agent_id and existing.account == account and existing.status == "pending":
                    return existing
            session = AssistantLoginSession(session_id=uuid.uuid4().hex, agent_id=agent_id, account=account)
            self._sessions[session.session_id] = session
        self._create(session)
        return session

    def get(self, session_id: str) -> AssistantLoginSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def refresh(self, session: AssistantLoginSession) -> dict[str, str]:
        with session._lock:
            if session.status != "pending" or not session._qrcode or not session._access_token:
                return session.snapshot()
            qrcode = session._qrcode
            token = session._access_token
        status, payload = self._fetch(
            "GET",
            f"{ASSISTANT_AUTH_API}/qrcode-detail/{qrcode}",
            {"qrcodeToken": token},
            None,
        )
        self._apply_status(session, status, payload)
        return session.snapshot()

    def _create(self, session: AssistantLoginSession) -> None:
        body = json.dumps({"channel": ASSISTANT_CHANNEL, "mac": mac_address()}).encode("utf-8")
        try:
            status, payload = self._fetch(
                "POST",
                f"{ASSISTANT_AUTH_API}/qrcode",
                {"Content-Type": "application/json"},
                body,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Welink] assistant qr create failed: %s", type(exc).__name__)
            session.status = "failed"
            session.error = "助手授权码申请失败"
            return
        if not _accepted(payload):
            session.status = "failed"
            session.error = _safe_message(payload) or f"助手授权码申请失败（HTTP {status}）"
            return
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        qrcode = str(data.get("qrcode") or "").strip()
        access_token = str(data.get("accessToken") or "").strip()
        we_url = str(data.get("weUrl") or "").strip()
        if not qrcode or not access_token or not we_url:
            session.status = "failed"
            session.error = "助手授权码返回不完整"
            return
        with session._lock:
            session._qrcode = qrcode
            session._access_token = access_token
            session.qr_payload = we_url
            session.status = "pending"
            session.error = ""
        logger.info("[Welink] assistant qr created for agent=%s", session.agent_id)

    def _apply_status(self, session: AssistantLoginSession, http_status: int, payload: Any) -> None:
        if not _accepted(payload):
            session.status = "failed"
            session.error = _safe_message(payload) or f"助手授权状态查询失败（HTTP {http_status}）"
            return
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if _expired(data.get("expired")):
            session.status = "failed"
            session.error = "授权码已过期，请再次点击弹出扫码登录"
            return
        state = data.get("status")
        if state == 0:
            session.status = "pending"
            return
        if state == 1:
            session.status = "pending"
            return
        if state == 3:
            session.status = "failed"
            session.error = "已取消助手授权"
            return
        if state == 2:
            credential_id = str(data.get("ak") or "").strip()
            secret = str(data.get("sk") or "").strip()
            if not credential_id or not secret:
                session.status = "failed"
                session.error = "扫码成功，但没有拿到助手凭证"
                return
            with session._lock:
                session._credential_id = credential_id
                session._secret = secret
                session.status = "success"
                session.error = ""
            logger.info("[Welink] assistant credential confirmed id_len=%s", len(credential_id))
            return
        session.status = "failed"
        session.error = "助手授权状态无法识别"


def _accepted(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    return code == 200 or str(code) == "200"


def _expired(value: Any) -> bool:
    if value is True or value == 1:
        return True
    return isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}


def _safe_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message") or payload.get("error") or ""
    text = str(message).strip()
    lowered = text.lower()
    if any(word in lowered for word in ("ak", "sk", "secret", "token")):
        return "助手授权接口返回失败"
    return text[:180]


ASSISTANT_LOGIN_MANAGER = AssistantLoginManager()
