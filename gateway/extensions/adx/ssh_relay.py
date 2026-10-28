# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Southbound SSH over the authenticated ADX Environment CONNECT interface."""
from __future__ import annotations

import asyncio
import socket
import ssl
from typing import Any
from urllib.parse import urlsplit

from gateway.extensions.adx.config import AdxRouterConfig
from gateway.extensions.agentos.agentos_router.ssh_relay import (
    YuanrongSshRelay,
    _import_asyncssh,
    _is_ssh_connect_retryable,
)


class _ConnectedSsh:
    """Own the SSH connection and its two bounded CONNECT pumps together."""

    def __init__(self, connection: Any, writer: asyncio.StreamWriter,
                 peer_writer: asyncio.StreamWriter, pumps: list[asyncio.Task]) -> None:
        self._connection = connection
        self._writer = writer
        self._peer_writer = peer_writer
        self._pumps = pumps

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def close(self) -> None:
        self._connection.close()
        self._writer.close()
        self._peer_writer.close()
        for pump in self._pumps:
            pump.cancel()

    async def wait_closed(self) -> None:
        try:
            await self._connection.wait_closed()
        finally:
            self.close()
            await asyncio.gather(*self._pumps, return_exceptions=True)
            await asyncio.gather(self._writer.wait_closed(), self._peer_writer.wait_closed(),
                                 return_exceptions=True)


class AdxSshRelay(YuanrongSshRelay):
    def __init__(self, config: AdxRouterConfig) -> None:
        super().__init__(config.router.ssh, frontend_endpoint=config.ingress_url)
        self._adx = config

    async def _connect_until_ready(self, instance_id: str, *, user_id: str = '',
                                   session_id: str = '') -> Any:
        del session_id
        deadline = asyncio.get_running_loop().time() + self._adx.connect_timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError('ADX SSH readiness deadline exceeded')
            try:
                return await asyncio.wait_for(self._connect(instance_id, user_id),
                    timeout=min(remaining, self._settings.connect_timeout_s))
            except Exception as error:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0 or not _is_ssh_connect_retryable(error):
                    raise
                await asyncio.sleep(min(1.0, remaining))

    async def _connect(self, instance_id: str, user_id: str) -> Any:
        # Environment IDs form CONNECT authority; never admit header delimiters.
        if not instance_id or any(not (c.isascii() and (c.isalnum() or c in '-_+.@')) for c in instance_id):
            raise ValueError('Invalid ADX Environment ID for SSH')
        if not self._adx.ssh_known_hosts:
            raise ValueError('gateway.adx.ssh_known_hosts is required for SSH host verification')
        asyncssh = _import_asyncssh()
        keys = self._resolve_client_keys(user_id)
        origin = urlsplit(self._adx.ingress_url)
        reader, writer = await asyncio.open_connection(origin.hostname, origin.port or 443,
            ssl=ssl.create_default_context(), server_hostname=origin.hostname)
        peer_writer = None
        client_socket = None
        bridge_socket = None
        pumps = []
        try:
            authority = f'{instance_id}:{self._settings.port}'
            request = (f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n'
                       f'Authorization: Bearer {self._adx.token}\r\n'
                       'X-Adx-Access-Kind: ssh\r\n\r\n')
            writer.write(request.encode('ascii'))
            await writer.drain()
            headers = await reader.readuntil(b'\r\n\r\n')
            status_line = headers.split(b'\r\n', 1)[0].split()
            if len(status_line) < 2 or status_line[0] not in (b'HTTP/1.1', b'HTTP/1.0'):
                raise ValueError('Invalid ADX CONNECT response')
            status = int(status_line[1])
            if status != 200:
                if status in (502, 503, 504):
                    raise ConnectionError(f'ADX SSH target unavailable: HTTP {status}')
                raise ValueError(f'ADX SSH CONNECT rejected: HTTP {status}')
            client_socket, bridge_socket = socket.socketpair()
            client_socket.setblocking(False)
            bridge_socket.setblocking(False)
            peer_reader, peer_writer = await asyncio.open_connection(sock=bridge_socket)
            bridge_socket = None

            async def pump(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
                try:
                    while True:
                        data = await source.read(32768)
                        if not data:
                            break
                        target.write(data)
                        await target.drain()
                finally:
                    target.close()

            pumps = [asyncio.create_task(pump(reader, peer_writer)),
                     asyncio.create_task(pump(peer_reader, writer))]
            connection = await asyncssh.connect(instance_id, port=self._settings.port,
                sock=client_socket, username=self._adx.ssh_user, client_keys=keys,
                agent_path=None, known_hosts=self._adx.ssh_known_hosts)
            client_socket = None
            return _ConnectedSsh(connection, writer, peer_writer, pumps)
        except BaseException:
            if client_socket is not None:
                client_socket.close()
            if bridge_socket is not None:
                bridge_socket.close()
            writer.close()
            if peer_writer is not None:
                peer_writer.close()
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            await asyncio.gather(writer.wait_closed(), return_exceptions=True)
            raise
