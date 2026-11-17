"""Small public Hub thumbnails, never persisted as signed URLs or original files."""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
import hashlib
import logging
from contextvars import ContextVar
import io
import time
from urllib.parse import urlsplit

import httpx
from PIL import Image

from .hub_package_downloader import HubPackageDownloader

_avatar_request = ContextVar('hub_avatar_request', default=False)


class _OmitSignedAvatarRequest(logging.Filter):
    def filter(self, record):
        return not _avatar_request.get()


# httpx logs full URLs at INFO; signed image URLs must never enter logs.
logging.getLogger('httpx').addFilter(_OmitSignedAvatarRequest())

MAX_INPUT_BYTES = 5 * 1024 * 1024
MAX_EDGE = 4096
MAX_THUMBNAIL_BYTES = 8 * 1024
MAX_ENTRIES = 256
TTL = 24 * 3600


def thumbnail(body: bytes) -> str:
    if len(body) > MAX_INPUT_BYTES:
        raise ValueError('Avatar file too large')
    with Image.open(io.BytesIO(body)) as original:
        if max(original.size) > MAX_EDGE or original.width * original.height > MAX_EDGE ** 2:
            raise ValueError('Avatar dimensions too large')
        original.seek(0)
        image = original.convert('RGBA')
        image.thumbnail((256, 256))
        for edge in (256, 128, 64):
            image.thumbnail((edge, edge))
            output = io.BytesIO()
            image.save(output, format='WEBP', quality=70, method=4)
            body = output.getvalue()
            if len(body) <= MAX_THUMBNAIL_BYTES:
                return 'data:image/webp;base64,' + base64.b64encode(body).decode('ascii')
    raise ValueError('Avatar thumbnail too large')


class HubAvatarCache:
    def __init__(self):
        self.entries = OrderedDict()
        self.semaphore = asyncio.Semaphore(4)

    async def get(self, url: str) -> str:
        if not url:
            return ''
        parsed = urlsplit(url)
        # Only public Hub object-storage hosts; never forward credentials or redirects.
        if parsed.scheme != 'https' or parsed.username or parsed.password:
            return ''
        try:
            HubPackageDownloader(
                allowed_download_hosts=("openjiuwen-market.obs.*.myhuaweicloud.com", "*.obs.*.myhuaweicloud.com")
            ).assert_download_url_allowed(url)
        except ValueError:
            return ''
        except RuntimeError:
            return ''
        key = hashlib.sha256(f'{parsed.netloc}{parsed.path}'.encode()).hexdigest()
        async with self.semaphore:
            entry = self.entries.get(key)
            if entry and time.monotonic() - entry[0] < TTL:
                self.entries.move_to_end(key)
                return entry[1]
            token = _avatar_request.set(True)
            try:
                async with httpx.AsyncClient(timeout=4, follow_redirects=False) as client:
                    async with client.stream('GET', url) as response:
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            body.extend(chunk)
                            if len(body) > MAX_INPUT_BYTES:
                                raise ValueError('Avatar file too large')
                value = await asyncio.to_thread(thumbnail, bytes(body))
                self.entries[key] = (time.monotonic(), value)
                self.entries.move_to_end(key)
                while len(self.entries) > MAX_ENTRIES:
                    self.entries.popitem(last=False)
                return value
            except Exception:
                # A broken/expired image must not fail the catalog refresh.
                return ''
            finally:
                _avatar_request.reset(token)

    async def fill(self, cards):
        async def fill_one(card):
            value = await self.get(card.get('icon_uri', ''))
            card['icon_uri'] = value
        try:
            async with asyncio.timeout(20):
                await asyncio.gather(*(fill_one(card) for card in cards))
        except TimeoutError:
            pass
