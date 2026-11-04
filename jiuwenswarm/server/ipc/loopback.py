# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One-use, authenticated Front-owned IPv4 loopback transport.

Only the private spawn pipe carries connection credentials. Transport frames
are consumed here, before the existing Hello/business protocol starts.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import math
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.server.ipc.protocol import HEADER, IpcError, decode_frame
from jiuwenswarm.server.ipc.session import FRONT_LOOPBACK
from jiuwenswarm.server.ipc.stream import IpcConnection, StreamConnection

logger = logging.getLogger(__name__)

_HOST = "127.0.0.1"
_AUTH_FRAME_BYTES = 4096
_AUTH_TIMEOUT = 2.0
_MAX_AUTH_CONNECTIONS = 16
_AUTH = "transport.auth"
_READY = "transport.ready"


def _fingerprint(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _bounded_integer(value: object, upper_bound: int) -> bool:
    return type(value) is int and 0 < value < upper_bound


def _valid_timeout(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and value > 0


async def _read_transport_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    header = await reader.readexactly(HEADER.size)
    size = HEADER.unpack(header)[0]
    if not 0 < size <= _AUTH_FRAME_BYTES:
        raise IpcError("Invalid transport authentication frame size")
    return decode_frame(await reader.readexactly(size))


@dataclass(frozen=True)
class LoopbackParameters:
    """Validated spawn credentials; never include the token in repr/errors."""

    port: int
    generation: int
    code_identity: str
    data_scope: str
    token: str = field(repr=False)
    timeout: float = 30.0

    @classmethod
    def from_payload(
        cls, payload: object, *, code_identity: str, data_scope: str,
    ) -> LoopbackParameters:
        if not isinstance(payload, dict):
            raise ValueError("runtime_ipc=front_loopback stage=parameters missing launch parameters")
        port = payload.get("port")
        generation = payload.get("generation")
        timeout = payload.get("timeout")
        checks = (
            payload.get("transport") == FRONT_LOOPBACK,
            payload.get("host") == _HOST,
            _bounded_integer(port, 65536),
            _bounded_integer(generation, 2**63),
            _fingerprint(payload.get("token")),
            _fingerprint(code_identity),
            payload.get("code_identity") == code_identity,
            _fingerprint(data_scope),
            payload.get("data_scope") == data_scope,
            _valid_timeout(timeout),
        )
        if not all(checks):
            raise ValueError("runtime_ipc=front_loopback stage=parameters invalid launch parameters")
        return cls(port, generation, code_identity, data_scope, payload["token"], float(timeout))

    def launch_payload(self) -> dict[str, Any]:
        return {"transport": FRONT_LOOPBACK, "host": _HOST, "port": self.port,
                "token": self.token, "generation": self.generation,
                "code_identity": self.code_identity, "data_scope": self.data_scope,
                "timeout": self.timeout}

    def auth_frame(self) -> dict[str, Any]:
        return {"kind": _AUTH, "transport": FRONT_LOOPBACK, "token": self.token,
                "generation": self.generation, "code_identity": self.code_identity,
                "data_scope": self.data_scope}

    def matches_auth(self, frame: dict[str, Any]) -> bool:
        """Check transport identity without exposing the one-use token."""
        token = frame.get("token")
        if not _fingerprint(token):
            return False
        return all((
            frame.get("kind") == _AUTH,
            frame.get("transport") == FRONT_LOOPBACK,
            hmac.compare_digest(token, self.token),
            type(frame.get("generation")) is int,
            frame.get("generation") == self.generation,
            frame.get("code_identity") == self.code_identity,
            frame.get("data_scope") == self.data_scope,
        ))


class FrontLoopbackListener:
    """Accept exactly one authenticated Worker, rejecting other candidates."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("runtime_ipc=front_loopback stage=parameters invalid timeout")
        self.generation = secrets.randbits(63) or 1
        self._timeout = timeout
        self._deadline = 0.0
        self._server: asyncio.AbstractServer | None = None
        self._parameters: LoopbackParameters | None = None
        self._accepted: asyncio.Future[StreamConnection] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._delivered = False
        self._closed = False
        self._auth_limit_logged = False

    async def start(self, *, code_identity: str, data_scope: str) -> None:
        if self._closed or self._accepted is not None:
            raise RuntimeError("runtime_ipc=front_loopback stage=listen listener already consumed")
        self._accepted = asyncio.get_running_loop().create_future()
        self._deadline = time.monotonic() + self._timeout
        try:
            self._server = await asyncio.start_server(self._on_connect, _HOST, 0, family=socket.AF_INET)
        except OSError as exc:
            raise OSError(exc.errno, f"runtime_ipc=front_loopback stage=bind errno={exc.errno}") from None
        port = self._server.sockets[0].getsockname()[1]
        self._parameters = LoopbackParameters(port, self.generation, code_identity,
                                              data_scope, secrets.token_hex(32), self._timeout)
        logger.info("runtime_ipc=front_loopback stage=listen host=%s port=%s generation=%s",
                    _HOST, port, self.generation)

    def launch_payload(self) -> dict[str, Any]:
        if self._parameters is None:
            raise RuntimeError("runtime_ipc=front_loopback stage=parameters listener unavailable")
        return self._parameters.launch_payload()

    def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closed or len(self._tasks) >= _MAX_AUTH_CONNECTIONS:
            writer.close()
            if not self._closed and not self._auth_limit_logged:
                self._auth_limit_logged = True
                logger.warning(
                    "runtime_ipc=front_loopback stage=auth rejected reason=connection_limit active=%s limit=%s",
                    len(self._tasks), _MAX_AUTH_CONNECTIONS,
                )
            return
        task = asyncio.create_task(self._authenticate(reader, writer), name="runtime-ipc-auth")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _accepts_auth(self, frame: dict[str, Any]) -> bool:
        expected = self._parameters
        if expected is None or self._accepted is None:
            return False
        if self._accepted.done():
            return False
        return expected.matches_auth(frame)

    async def _authenticate(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = StreamConnection(reader, writer)
        adopted = False
        try:
            timeout = max(0.001, min(_AUTH_TIMEOUT, self._deadline - time.monotonic()))
            async with asyncio.timeout(timeout):
                frame = await _read_transport_frame(reader)
                if not self._accepts_auth(frame):
                    logger.warning("runtime_ipc=front_loopback stage=auth rejected")
                    return
                # Claim synchronously before the first await so concurrent
                # copies of a valid credential cannot create two sessions.
                self._parameters = None
                if self._server is not None:
                    self._server.close()
                await conn.send({"kind": _READY, "generation": self.generation})
                self._accepted.set_result(conn)
                adopted = True
                logger.info("runtime_ipc=front_loopback stage=auth accepted generation=%s", self.generation)
        except (OSError, asyncio.IncompleteReadError, IpcError):
            logger.warning("runtime_ipc=front_loopback stage=auth rejected or timed out")
        finally:
            if not adopted:
                await conn.close()

    async def accept(self) -> IpcConnection:
        if self._accepted is None:
            raise RuntimeError("runtime_ipc=front_loopback stage=accept listener not started")
        try:
            conn = await asyncio.wait_for(asyncio.shield(self._accepted),
                                          timeout=max(0.001, self._deadline - time.monotonic()))
        except TimeoutError:
            raise TimeoutError("runtime_ipc=front_loopback stage=accept timed out") from None
        self._delivered = True
        return conn

    async def close(self) -> None:
        self._closed = True
        self._parameters = None
        server, self._server = self._server, None
        if server is not None:
            server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._accepted is not None:
            if not self._accepted.done():
                self._accepted.cancel()
            elif not self._accepted.cancelled() and not self._delivered:
                await self._accepted.result().close()
        # Since Python 3.12 wait_closed also waits for accepted streams. The
        # delivered IPC stream belongs to the supervisor, not this listener.
        if server is not None and not self._delivered:
            await server.wait_closed()


class FrontConnectSessionSource:
    """Connect once without binding a socket or acquiring a resident lease."""

    reconnectable = False

    def __init__(self, parameters: LoopbackParameters) -> None:
        self.generation = parameters.generation
        self._parameters: LoopbackParameters | None = parameters
        self._conn: StreamConnection | None = None

    async def start(self) -> None:
        parameters, self._parameters = self._parameters, None
        if parameters is None:
            raise RuntimeError("runtime_ipc=front_loopback stage=connect source already consumed")
        stage = "connect"
        try:
            async with asyncio.timeout(parameters.timeout):
                reader, writer = await asyncio.open_connection(_HOST, parameters.port, family=socket.AF_INET)
                self._conn = StreamConnection(reader, writer)
                stage = "auth"
                await self._conn.send(parameters.auth_frame())
                ready = await _read_transport_frame(reader)
                if ready.get("kind") != _READY or ready.get("generation") != self.generation:
                    raise IpcError("Invalid transport authentication acknowledgement")
        except BaseException as exc:
            await self.close()
            if isinstance(exc, (OSError, asyncio.IncompleteReadError, IpcError)):
                detail = f"runtime_ipc=front_loopback stage={stage} errno={getattr(exc, 'errno', None)}"
                raise ConnectionError(detail) from None
            raise
        logger.info("runtime_ipc=front_loopback stage=connected generation=%s", self.generation)

    async def accept(self) -> IpcConnection:
        conn, self._conn = self._conn, None
        if conn is None:
            raise ConnectionError("runtime_ipc=front_loopback stage=accept source already consumed")
        return conn

    async def close(self) -> None:
        self._parameters = None
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
