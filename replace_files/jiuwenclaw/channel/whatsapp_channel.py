"""WhatsApp channel implemented through a local Baileys bridge."""

from __future__ import annotations

import logging
import asyncio
import inspect
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jiuwenclaw.channel.base import BaseChannel, ChannelMetadata, RobotMessageRouter
from jiuwenclaw.schema.message import EventType, Message, ReqMethod


logger = logging.getLogger(__name__)


@dataclass
class WhatsAppChannelConfig:
    enabled: bool = False
    enable_streaming: bool = True
    bridge_ws_url: str = "ws://127.0.0.1:19600/ws"
    allow_from: list[str] = field(default_factory=list)
    default_jid: str = ""
    auto_start_bridge: bool = False
    bridge_command: str = "node scripts/whatsapp-bridge.js"
    bridge_workdir: str = ""
    bridge_env: dict[str, str] = field(default_factory=dict)


class WhatsAppChannel(BaseChannel):
    """WhatsApp channel that exchanges JSON frames with a Baileys bridge over WebSocket."""

    name = "whatsapp"

    def __init__(self, config: WhatsAppChannelConfig, router: RobotMessageRouter):
        super().__init__(config, router)
        self.config: WhatsAppChannelConfig = config
        self._ws: Any = None
        self._connect_task: asyncio.Task | None = None
        self._bridge_process: asyncio.subprocess.Process | None = None
        self._on_message_cb: Callable[[Message], Any] | None = None
        self._send_lock = asyncio.Lock()
        self._bridge_ws_connected = False
        self._whatsapp_connected = False
        self._bridge_state = "stopped"
        self._qr_pending = False
        self._last_status_ts_ms: int | None = None
        self._last_status_code: int | None = None

    @property
    def channel_id(self) -> str:
        return self.name

    @property
    def clients(self) -> set[Any]:
        return set()

    def on_message(self, callback: Callable[[Message], None]) -> None:
        self._on_message_cb = callback

    async def start(self) -> None:
        if self._running:
            logger.warning("WhatsAppChannel is already running")
            return
        if not self.config.enabled:
            logger.warning("WhatsAppChannel is disabled (enabled=false)")
            return
        if not self.config.bridge_ws_url.strip():
            logger.error("WhatsAppChannel missing bridge_ws_url")
            return

        self._running = True
        if self.config.auto_start_bridge:
            await self._start_bridge_process()

        self._connect_task = asyncio.create_task(self._reconnect_loop(), name="whatsapp-channel-connect")
        logger.info("WhatsAppChannel started")

    async def stop(self) -> None:
        self._running = False

        if self._connect_task is not None:
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
            self._connect_task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("WhatsAppChannel close websocket failed: %s", exc)
            self._ws = None

        await self._stop_bridge_process()
        self._set_connection_state("stopped", bridge_ws_connected=False, whatsapp_connected=False, qr_pending=False)
        logger.info("WhatsAppChannel stopped")

    async def send(self, msg: Message) -> None:
        if self._ws is None:
            return
        if not self._whatsapp_connected:
            logger.warning("WhatsAppChannel send skipped: WhatsApp not connected (state=%s)", self._bridge_state)
            return
        if (
            not self.config.enable_streaming
            and msg.type == "event"
            and msg.event_type == EventType.CHAT_DELTA
        ):
            return

        text = self._extract_outgoing_text(msg)
        if not text.strip():
            return

        target_jid = self._extract_target_jid(msg)
        if not target_jid:
            logger.warning("WhatsAppChannel send skipped: no target jid")
            return

        frame = {
            "type": "send",
            "jid": target_jid,
            "text": text,
            "request_id": msg.id,
        }
        await self._send_frame(frame)

    def get_metadata(self) -> ChannelMetadata:
        return ChannelMetadata(
            channel_id=self.channel_id,
            source="websocket",
            extra={
                "bridge_ws_url": self.config.bridge_ws_url,
                "auto_start_bridge": self.config.auto_start_bridge,
                "bridge_state": self._bridge_state,
                "bridge_ws_connected": self._bridge_ws_connected,
                "whatsapp_connected": self._whatsapp_connected,
                "qr_pending": self._qr_pending,
                "last_status_ts_ms": self._last_status_ts_ms,
                "last_status_code": self._last_status_code,
            },
        )

    async def _start_bridge_process(self) -> None:
        if self._bridge_process is not None and self._bridge_process.returncode is None:
            return

        command = (self.config.bridge_command or "").strip()
        if not command:
            logger.warning("WhatsAppChannel auto_start_bridge enabled but bridge_command is empty")
            return

        workdir = (self.config.bridge_workdir or "").strip()
        if not workdir:
            workdir = str(Path(__file__).resolve().parents[1])

        env = os.environ.copy()
        for key, value in (self.config.bridge_env or {}).items():
            env[str(key)] = str(value)

        try:
            self._bridge_process = await asyncio.create_subprocess_shell(
                command,
                cwd=workdir,
                env=env,
            )
            logger.info("WhatsApp bridge process started: pid=%s", self._bridge_process.pid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("WhatsApp bridge process start failed: %s", exc)
            self._bridge_process = None

    async def _stop_bridge_process(self) -> None:
        process = self._bridge_process
        self._bridge_process = None
        if process is None:
            return
        if process.returncode is not None:
            return

        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            process.kill()
            try:
                await process.wait()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    async def _reconnect_loop(self) -> None:
        while self._running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("WhatsAppChannel connect failed: %s", exc)
            await asyncio.sleep(5)

    async def _connect_once(self) -> None:
        import websockets

        async with websockets.connect(
            self.config.bridge_ws_url,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            self._set_connection_state("bridge_connected", bridge_ws_connected=True)
            logger.info("WhatsAppChannel connected to bridge: %s", self.config.bridge_ws_url)
            try:
                async for raw in ws:
                    await self._handle_raw_message(raw)
            finally:
                self._ws = None
                next_state = "stopped" if not self._running else "bridge_disconnected"
                self._set_connection_state(
                    next_state,
                    bridge_ws_connected=False,
                    whatsapp_connected=False,
                    qr_pending=False,
                )
                logger.info("WhatsAppChannel bridge disconnected")

    async def _send_frame(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            return
        data = json.dumps(frame, ensure_ascii=False)
        async with self._send_lock:
            await ws.send(data)

    async def _handle_raw_message(self, raw: str | bytes) -> None:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            logger.warning("WhatsAppChannel bridge message decode failed")
            return

        if not isinstance(data, dict):
            return

        msg_type = str(data.get("type") or "").strip().lower()
        if msg_type == "inbound":
            await self._handle_inbound_message(data)
            return
        if msg_type == "status":
            self._handle_status_message(data)
            return
        if msg_type == "qr":
            self._handle_qr_message(data)
            return
        if msg_type == "send_result":
            self._handle_send_result(data)
            return
        if msg_type == "pong":
            return

        logger.debug("WhatsAppChannel ignored bridge message type: %s", msg_type)

    async def _handle_inbound_message(self, data: dict[str, Any]) -> None:
        text = str(data.get("text") or "").strip()
        if not text:
            return

        jid = str(data.get("jid") or "").strip()
        sender = str(data.get("sender") or jid).strip()
        sender_number = sender.split("@", 1)[0] if "@" in sender else sender
        allow_sender_hint = f"{sender}|{sender_number}" if sender_number else sender
        if not self.is_allowed(allow_sender_hint):
            logger.warning("WhatsAppChannel sender not allowed: %s", sender)
            return

        message_id = str(data.get("message_id") or f"wa-{int(time.time() * 1000)}")
        msg = Message(
            id=message_id,
            type="req",
            channel_id=self.channel_id,
            session_id=jid or sender,
            params={"content": text, "query": text},
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.CHAT_SEND,
            metadata={
                "whatsapp_jid": jid,
                "whatsapp_sender": sender,
                "whatsapp_push_name": str(data.get("push_name") or ""),
                "whatsapp_message_id": message_id,
            },
        )

        try:
            from jiuwenclaw.config import update_channel_in_config

            update_channel_in_config(
                "whatsapp",
                {
                    "last_jid": jid or "",
                    "last_sender": sender or "",
                    "last_message_id": message_id,
                },
            )
        except Exception:
            pass

        handled = False
        if self._on_message_cb is not None:
            result = self._on_message_cb(msg)
            if inspect.isawaitable(result):
                result = await result
            handled = bool(result)

        if not handled:
            await self.bus.route_user_message(msg)

    def _handle_status_message(self, data: dict[str, Any]) -> None:
        state = str(data.get("state") or data.get("status") or "").strip().lower()
        ts_ms = self._to_int(data.get("ts"))
        status_code = self._to_int(data.get("status_code"))
        ws_connected = self._ws is not None

        if state == "open":
            self._set_connection_state(
                "open",
                bridge_ws_connected=ws_connected,
                whatsapp_connected=True,
                qr_pending=False,
                ts_ms=ts_ms,
                status_code=status_code,
            )
            return

        if state in {"connecting", "reconnecting"}:
            self._set_connection_state(
                state,
                bridge_ws_connected=ws_connected,
                whatsapp_connected=False,
                ts_ms=ts_ms,
                status_code=status_code,
            )
            return

        if state in {"close", "closed", "disconnected"}:
            self._set_connection_state(
                "close",
                bridge_ws_connected=ws_connected,
                whatsapp_connected=False,
                qr_pending=False,
                ts_ms=ts_ms,
                status_code=status_code,
            )
            return

        if state == "logged_out":
            self._set_connection_state(
                "logged_out",
                bridge_ws_connected=ws_connected,
                whatsapp_connected=False,
                qr_pending=False,
                ts_ms=ts_ms,
                status_code=status_code,
            )
            return

        if state:
            self._set_connection_state(
                state,
                bridge_ws_connected=ws_connected,
                ts_ms=ts_ms,
                status_code=status_code,
            )

    def _handle_qr_message(self, data: dict[str, Any]) -> None:
        self._set_connection_state(
            "qr_pending",
            bridge_ws_connected=self._ws is not None,
            whatsapp_connected=False,
            qr_pending=True,
            ts_ms=self._to_int(data.get("ts")),
        )
        logger.info("WhatsAppChannel QR available; scan it in the bridge terminal to link WhatsApp")

    def _handle_send_result(self, data: dict[str, Any]) -> None:
        ok = bool(data.get("ok"))
        request_id = str(data.get("request_id") or "").strip()
        if ok:
            logger.debug("WhatsAppChannel send ack: request_id=%s jid=%s", request_id, data.get("jid"))
            return

        error = str(data.get("error") or "unknown error").strip()
        logger.warning("WhatsAppChannel send failed: request_id=%s error=%s", request_id, error)
        if error == "whatsapp not connected":
            self._set_connection_state(
                "connecting",
                bridge_ws_connected=self._ws is not None,
                whatsapp_connected=False,
            )

    def _set_connection_state(
        self,
        state: str,
        *,
        bridge_ws_connected: bool | None = None,
        whatsapp_connected: bool | None = None,
        qr_pending: bool | None = None,
        ts_ms: int | None = None,
        status_code: int | None = None,
    ) -> None:
        previous = (
            self._bridge_state,
            self._bridge_ws_connected,
            self._whatsapp_connected,
            self._qr_pending,
            self._last_status_code,
        )

        self._bridge_state = state
        if bridge_ws_connected is not None:
            self._bridge_ws_connected = bridge_ws_connected
        if whatsapp_connected is not None:
            self._whatsapp_connected = whatsapp_connected
        if qr_pending is not None:
            self._qr_pending = qr_pending
        if ts_ms is not None:
            self._last_status_ts_ms = ts_ms
        if status_code is not None or state in {"open", "connecting", "close", "logged_out", "qr_pending", "stopped"}:
            self._last_status_code = status_code

        current = (
            self._bridge_state,
            self._bridge_ws_connected,
            self._whatsapp_connected,
            self._qr_pending,
            self._last_status_code,
        )
        if current != previous:
            logger.info(
                "WhatsAppChannel state updated: state=%s bridge_ws_connected=%s whatsapp_connected=%s qr_pending=%s status_code=%s",
                self._bridge_state,
                self._bridge_ws_connected,
                self._whatsapp_connected,
                self._qr_pending,
                self._last_status_code,
            )

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_outgoing_text(msg: Message) -> str:
        payload = getattr(msg, "payload", None) or {}
        if msg.event_type == EventType.HEARTBEAT_RELAY and isinstance(payload, dict) and payload.get("heartbeat"):
            return str(payload.get("heartbeat"))

        if isinstance(payload, dict) and "content" in payload:
            content = payload.get("content")
            if isinstance(content, dict):
                return str(content.get("output", content))
            return str(content or "")
        if msg.params and "content" in msg.params:
            return str(msg.params.get("content") or "")
        if isinstance(msg.payload, str):
            return msg.payload
        return ""

    def _extract_target_jid(self, msg: Message) -> str:
        metadata = msg.metadata or {}
        for key in ("whatsapp_jid", "whatsapp_sender"):
            raw = metadata.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()

        default_jid = (self.config.default_jid or "").strip()
        if default_jid:
            return default_jid

        session_id = str(msg.session_id or "").strip()
        if session_id and "@" in session_id:
            return session_id
        return ""
