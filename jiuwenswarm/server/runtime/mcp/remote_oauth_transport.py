"""Registered MCP transport: dynamic OAuth with no redirect credential leakage."""

import asyncio
from typing import ClassVar

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openjiuwen.core.foundation.tool.mcp.base import NO_TIMEOUT
from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
    StreamableHttpClient,
)

from jiuwenswarm.server.runtime.mcp.remote_oauth import (
    QCC_RESOURCE,
    OAuthError,
    RemoteOAuthAuth,
    oauth_manager,
)


class OAuthStreamableHttpClient(StreamableHttpClient):
    """Only explicitly selected OAuth configs use this registered client.

    Reuse core's tool/resource conversion and lifecycle, but own the HTTP
    client so OAuth credentials cannot follow same-origin path redirects.
    Core's existing static-header authentication strategy is untouched.
    """

    __client_name__: ClassVar[list[str]] = ["workswarm-oauth"]

    async def connect(self, *, timeout: float = NO_TIMEOUT) -> bool:
        async def guard(request):
            if str(request.url) != QCC_RESOURCE:
                raise OAuthError("OAuth resource redirect is not permitted.")

        if self._server_path != QCC_RESOURCE or self._name != "qcc-company":
            self._last_connect_error = OAuthError(
                "Invalid OAuth resource configuration."
            )
            return False
        task = asyncio.current_task()
        cancellations = task.cancelling() if task else 0
        try:
            http = await self._exit_stack.enter_async_context(
                httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        timeout if timeout != NO_TIMEOUT else 60.0, read=300.0
                    ),
                    follow_redirects=False,
                    auth=RemoteOAuthAuth(oauth_manager(), self._name),
                    event_hooks={"request": [guard]},
                )
            )
            self._client = streamable_http_client(self._server_path, http_client=http)
            streams = await self._exit_stack.enter_async_context(self._client)
            self._read, self._write, *_ = streams
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(self._read, self._write, sampling_callback=None)
            )
            await self._session.initialize()
            self._is_disconnected = False
            return True
        except BaseException as exc:
            # SDK task groups may wrap HTTP failures. Never retain bodies or
            # credentials in the error that reaches model/UI diagnostics.
            self._last_connect_error = OAuthError(
                "OAuth MCP connection failed. Check authorization and retry."
            )
            await self.disconnect()
            # Process-control exceptions must propagate after cleanup.
            if not isinstance(exc, (Exception, asyncio.CancelledError)):
                raise
            if (
                isinstance(exc, asyncio.CancelledError)
                and task
                and task.cancelling() > cancellations
            ):
                raise
            return False
