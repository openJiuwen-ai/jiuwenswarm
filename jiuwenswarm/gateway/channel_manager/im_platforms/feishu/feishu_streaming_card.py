"""CardKit primitives for one Feishu streaming response card."""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import requests

from jiuwenswarm.gateway.channel_manager.im_platforms.platform_adapter.streaming_session import (
    StreamingSession,
)


FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
TOKEN_REFRESH_MARGIN_SECONDS = 60
AUTH_ERROR_CODES = {99991663, 99991664}


class CardKitError(RuntimeError):
    """Raised when a CardKit operation fails."""


class FeishuCardKitClient:
    """Small async wrapper around the CardKit streaming endpoints."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        timeout: float = 10.0,
        requester: Callable[..., Any] = requests.request,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout = timeout
        self._requester = requester
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def create_card(self) -> str:
        card = {
            "schema": "2.0",
            "config": {
                "streaming_mode": True,
                "summary": {"content": "正在生成回复…"},
                "streaming_config": {
                    "print_frequency_ms": {"default": 50},
                    "print_step": {"default": 25},
                    "print_strategy": "delay",
                },
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "element_id": "content", "content": "正在生成回复…"}
                ]
            },
        }
        data = await self._authorized_request(
            "POST",
            "/cardkit/v1/cards",
            {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)},
        )
        card_id = str((data.get("data") or {}).get("card_id") or "")
        if not card_id:
            raise CardKitError("CardKit create response did not contain data.card_id")
        return card_id

    async def update_content(
        self,
        card_id: str,
        content: str,
        sequence: int,
    ) -> None:
        await self._authorized_request(
            "PUT",
            f"/cardkit/v1/cards/{card_id}/elements/content/content",
            {"content": content, "sequence": sequence, "uuid": uuid.uuid4().hex},
        )

    async def close_card(self, card_id: str, summary: str, sequence: int) -> None:
        settings = {
            "config": {
                "streaming_mode": False,
                "summary": {"content": summary[:100]},
            }
        }
        await self._authorized_request(
            "PATCH",
            f"/cardkit/v1/cards/{card_id}/settings",
            {
                "settings": json.dumps(settings, ensure_ascii=False),
                "sequence": sequence,
                "uuid": uuid.uuid4().hex,
            },
        )

    async def _authorized_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        for attempt in range(2):
            token = await self._get_token()
            try:
                return await self._request(
                    method,
                    path,
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                )
            except CardKitError as exc:
                if "authentication" not in str(exc).lower() or attempt:
                    raise
                async with self._token_lock:
                    if self._token == token:
                        self._token = ""
                        self._token_expires_at = 0.0
        raise CardKitError("CardKit authentication retry exhausted")

    async def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            data = await self._request(
                "POST",
                "/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            token = str(data.get("tenant_access_token") or "")
            if not token:
                raise CardKitError("CardKit token response did not contain tenant_access_token")
            lifetime = max(0, int(data.get("expire") or 7200) - TOKEN_REFRESH_MARGIN_SECONDS)
            self._token = token
            self._token_expires_at = time.monotonic() + lifetime
            return token

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await asyncio.to_thread(
                self._requester,
                method,
                f"{FEISHU_API_BASE}{path}",
                timeout=self._timeout,
                **kwargs,
            )
            data = response.json()
        except Exception as exc:
            raise CardKitError(f"CardKit request failed: {exc}") from exc
        code = data.get("code", 0)
        if response.status_code == 401 or code in AUTH_ERROR_CODES:
            raise CardKitError("CardKit authentication failed")
        if response.status_code >= 400 or code != 0:
            msg = data.get('msg', '')
            raise CardKitError(
                f"CardKit request failed: http={response.status_code} code={code}, message is {msg}"
            )
        return data


class _CardKitSurface:
    """Address one CardKit card through the shared streaming session."""

    def __init__(self, cardkit: FeishuCardKitClient) -> None:
        self._cardkit = cardkit

    async def open(self, text: str) -> str:
        # A CardKit card is always created empty: its content element is filled
        # by the first update, so there is nothing to do with ``text`` here.
        del text
        return await self._cardkit.create_card()

    async def write(self, handle: str, text: str, sequence: int) -> None:
        await self._cardkit.update_content(handle, text, sequence)

    async def close(self, handle: str, text: str, sequence: int) -> None:
        await self._cardkit.close_card(handle, text, sequence)


class FeishuStreamingSession(StreamingSession):
    """Accumulate model output and update exactly one CardKit card."""

    def __init__(
        self,
        cardkit: FeishuCardKitClient,
        send_card: Callable[[str], Awaitable[None]],
        *,
        debounce_ms: int = 150,
    ) -> None:
        async def announce(card_id: str) -> None:
            # Creating the card does not show it to anyone; that takes an
            # ordinary interactive message referencing the card id.
            await send_card(
                json.dumps({"type": "card", "data": {"card_id": card_id}})
            )

        super().__init__(
            _CardKitSurface(cardkit),
            announce=announce,
            debounce_ms=debounce_ms,
        )
