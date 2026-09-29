# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SshChannel - northbound SSH server channel into MessageHandler.

SSH client -> SshChannel -> MessageHandler (``ssh.relay``) ->
AgentOS Router / YuanRong southbound PTY relay.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.gateway.channel_manager.base import BaseChannel, RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.protocol.ssh.config import (
    DEFAULT_LISTEN_HOST,
    proxy_config_from_dict,
)
from jiuwenswarm.gateway.channel_manager.protocol.ssh.server import SSHAgentHooks, SSHProxy

logger = logging.getLogger(__name__)


@dataclass
class SshRelaySession:
    """Shared northbound/southbound handle for one SSH interactive-shell relay."""

    session_id: str
    process: Any
    done: asyncio.Event = field(default_factory=asyncio.Event)
    exit_code: int = 0
    # Optional northbound remote command (``ssh user@host "<agent> ..."``).
    # Used for AgentOS agent_type selection and southbound exec forwarding.
    command: str | None = None
    # Background southbound relay task (set by AgentOS Router); cancelled on
    # northbound timeout or disconnect so the YuanRong SSH connection is
    # torn down instead of lingering until relay_timeout_sec / TCP timeout.
    relay_task: asyncio.Task[Any] | None = None

    def cancel_relay(self) -> bool:
        """Cancel the southbound relay task if it is still running."""
        task = self.relay_task
        if task is None or task.done():
            return False
        task.cancel()
        return True


@dataclass
class SshChannelConfig:
    """SSH server channel configuration."""

    enabled: bool = False
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = 2222
    host_key_path: str = ""
    relay_timeout_sec: float = 3600.0

    def to_proxy_config(self):
        raw = {
            "listen_host": self.listen_host,
            "listen_port": self.listen_port,
            "host_key_path": self.host_key_path,
        }
        return proxy_config_from_dict(raw)

    @classmethod
    def from_dict(cls, conf: dict[str, Any]) -> "SshChannelConfig":
        return cls(
            enabled=bool(conf.get("enabled", False)),
            listen_host=str(conf.get("listen_host") or DEFAULT_LISTEN_HOST),
            listen_port=int(conf.get("listen_port", 2222)),
            host_key_path=str(conf.get("host_key_path") or ""),
            relay_timeout_sec=float(conf.get("relay_timeout_sec", 3600.0)),
        )


@dataclass
class _SshSession:
    session_id: str
    process: Any
    username: str
    client_addr: str
    relay: SshRelaySession | None = None


class SshChannel(BaseChannel):
    """SSH server channel that delivers ``ssh.relay`` requests to MessageHandler."""

    name = "ssh"

    def __init__(self, config: SshChannelConfig, router: RobotMessageRouter):
        super().__init__(config, router)
        self.config: SshChannelConfig = config
        self._proxy: SSHProxy | None = None
        self._running = False
        self._on_message_cb: Callable[[Message], Any] | None = None
        self._sessions: dict[str, _SshSession] = {}

    @property
    def channel_id(self) -> str:
        return self.name

    @property
    def clients(self) -> set[Any]:
        return set(self._sessions.keys())

    def on_message(self, callback: Callable[[Message], None]) -> None:
        self._on_message_cb = callback

    async def start(self) -> None:
        if self._running:
            return
        if not self.config.enabled:
            logger.info("[SSHChannel] disabled by config")
            return

        hooks = self._build_agent_hooks()
        self._proxy = SSHProxy(self.config.to_proxy_config(), agent_hooks=hooks)
        await self._proxy.start()
        self._running = True
        logger.info(
            "[SSHChannel] started (listen %s:%s -> MessageHandler; "
            "southbound relay via agent client)",
            self.config.listen_host,
            self.config.listen_port,
        )
        await self._proxy.wait_closed()

    async def stop(self) -> None:
        self._running = False
        self._sessions.clear()
        if self._proxy is not None:
            await self._proxy.stop()
            self._proxy = None

    def _build_agent_hooks(self) -> SSHAgentHooks:
        return SSHAgentHooks(
            register_session=self._register_session,
            unregister_session=self._unregister_session,
            submit_relay=self._submit_relay,
            wait_relay_done=self._wait_relay_done,
        )

    async def _register_session(
        self,
        *,
        session_id: str,
        process: Any,
        username: str,
        client_addr: str,
    ) -> None:
        self._sessions[session_id] = _SshSession(
            session_id=session_id,
            process=process,
            username=username,
            client_addr=client_addr,
        )

    async def _unregister_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        relay = session.relay if session is not None else None
        # Northbound session is over (clean exit, hard disconnect, or the
        # handler task itself was cancelled by asyncssh on connection loss).
        # The southbound pumps may never notice a dead client (stdin.read can
        # block forever), so explicitly cancel the relay task here.
        if relay is not None and relay.cancel_relay():
            logger.info(
                "[SSHChannel] northbound session %s closed; "
                "cancelled southbound relay",
                session_id,
            )

    async def _wait_relay_done(self, session_id: str) -> int:
        session = self._sessions.get(session_id)
        relay = session.relay if session is not None else None
        if relay is None:
            return 1
        try:
            await asyncio.wait_for(relay.done.wait(), timeout=self.config.relay_timeout_sec)
        except asyncio.TimeoutError:
            logger.error(
                "[SSHChannel] relay timeout for session %s "
                "(no southbound handler completed the relay within %.0fs); "
                "cancelling southbound relay",
                session_id,
                self.config.relay_timeout_sec,
            )
            if relay.cancel_relay():
                try:
                    await asyncio.wait_for(relay.done.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[SSHChannel] southbound relay did not finish after "
                        "cancel for session %s",
                        session_id,
                    )
            if not relay.done.is_set():
                relay.exit_code = 124
                relay.done.set()
            return 124
        return relay.exit_code

    async def _submit_relay(
        self,
        session_id: str,
        metadata: dict[str, Any],
    ) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            logger.error("[SSHChannel] unknown relay session: %s", session_id)
            return

        relay = SshRelaySession(
            session_id=session_id,
            process=session.process,
        )
        session.relay = relay

        if self._on_message_cb is None:
            session.process.stdout.write(b"[error] MessageHandler not available\r\n")
            relay.exit_code = 1
            relay.done.set()
            return

        username = str(metadata.get("username") or "").strip() or "unknown"
        # ``relay_session`` (with live ``process``) is handed to the
        # southbound AgentOS router in-process; it is never serialized.
        params: dict[str, Any] = {
            "relay_session": relay,
        }
        command = str(metadata.get("command") or "").strip()
        if command:
            params["command"] = command
            relay.command = command
        msg = Message(
            id=f"ssh_{uuid.uuid4().hex[:12]}",
            type="req",
            channel_id=self.channel_id,
            session_id=session_id,
            user_id=username,
            params=params,
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.SSH_RELAY,
            is_stream=False,
            metadata=metadata,
        )
        logger.info(
            "[SSHChannel] deliver to MessageHandler: session=%s method=%s",
            session_id,
            ReqMethod.SSH_RELAY.value,
        )
        result = self._on_message_cb(msg)
        if asyncio.iscoroutine(result):
            await result

    async def send(self, msg: Message) -> None:
        logger.debug(
            "[SSHChannel] outbound message ignored (PTY relay channel): %s",
            getattr(msg, "id", ""),
        )
