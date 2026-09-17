"""CardKit primitives for one Feishu streaming response card."""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import requests


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
                    {"tag": "markdown", "element_id": "content", "content": ""}
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


class FeishuStreamingSession:
    """Accumulate model output and update exactly one CardKit card."""

    def __init__(
        self,
        cardkit: FeishuCardKitClient,
        send_card: Callable[[str], Awaitable[None]],
        *,
        debounce_ms: int = 150,
    ) -> None:
        self._cardkit = cardkit
        self._send_card = send_card
        self._debounce_ms = debounce_ms
        self._card_id = ""
        self._text = ""
        self._sequence = 0
        self._closed = False
        self._closing = False
        self._flush_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    @property
    def rendered_text(self) -> str:
        return self._text

    @property
    def is_active(self) -> bool:
        return bool(self._card_id) and not self._closed and not self._closing

    async def start(self) -> None:
        self._card_id = await self._cardkit.create_card()
        try:
            await self._send_card(
                json.dumps({"type": "card", "data": {"card_id": self._card_id}})
            )
        except Exception:
            try:
                await self._cardkit.close_card(self._card_id, "", self._next_sequence())
            except CardKitError:
                pass
            raise

    def replace(self, text: str) -> None:
        if self.is_active:
            self._text = text
            self._schedule_flush()

    async def finalize(self, final_text: str = "") -> str:
        if self._closed:
            return self._text
        self._closing = True
        if final_text:
            self._text = _merge_streamed_and_final(self._text, final_text)
        if self._flush_task is not None:
            await asyncio.shield(self._flush_task)
        try:
            await self._write_snapshot(self._text)
            await self._cardkit.close_card(
                self._card_id,
                self._text,
                self._next_sequence(),
            )
            return self._text
        finally:
            self._closed = True

    def _schedule_flush(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        await asyncio.sleep(self._debounce_ms / 1000)
        if self.is_active:
            snapshot = self._text
            await self._write_snapshot(snapshot)
            if self.is_active and snapshot != self._text:
                self._flush_task = None
                self._schedule_flush()

    async def _write_snapshot(self, text: str) -> None:
        async with self._write_lock:
            await self._cardkit.update_content(
                self._card_id,
                text,
                self._next_sequence(),
            )

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence


def _merge_streamed_and_final(streamed: str, final: str) -> str:
    if not final.strip():
        return streamed
    if not streamed.strip() or final.startswith(streamed):
        return final
    if streamed.startswith(final):
        return streamed
    return final if len(final) >= len(streamed) else streamed
