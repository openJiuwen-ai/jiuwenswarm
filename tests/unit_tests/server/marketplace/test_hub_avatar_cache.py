import base64
import io
import time

import httpx
import pytest
from PIL import Image

from jiuwenswarm.server.runtime.marketplace.hub_avatar_cache import HubAvatarCache, thumbnail, MAX_INPUT_BYTES, MAX_THUMBNAIL_BYTES
from jiuwenswarm.server.runtime.marketplace.hub_catalog_cache import safe_metadata


def png(size=(700, 500)):
    output = io.BytesIO()
    Image.new('RGB', size, 'blue').save(output, format='PNG')
    return output.getvalue()


def test_thumbnail_is_bounded_and_survives_catalog_sanitization():
    value = thumbnail(png())
    body = base64.b64decode(value.split(',', 1)[1])
    assert len(body) <= MAX_THUMBNAIL_BYTES
    with Image.open(io.BytesIO(body)) as image:
        assert max(image.size) <= 256
    assert safe_metadata({'icon_uri': value})['icon_uri'] == value


@pytest.mark.parametrize('body', [b'invalid', b'x' * (MAX_INPUT_BYTES + 1)], ids=['invalid', 'oversized'])
def test_invalid_or_oversized_file_is_rejected(body):
    with pytest.raises((ValueError, OSError)):
        thumbnail(body)


def test_oversized_dimensions_are_rejected():
    with pytest.raises(ValueError):
        thumbnail(png((4097, 1)))


@pytest.mark.asyncio
async def test_untrusted_url_is_not_requested():
    cache = HubAvatarCache()
    for url in ('http://127.0.0.1/a', 'https://example.com/a', 'file:///tmp/a'):
        assert await cache.get(url) == ''


@pytest.mark.asyncio
async def test_rotating_signature_reuses_thumbnail_and_bounds_cache(monkeypatch):
    import jiuwenswarm.server.runtime.marketplace.hub_avatar_cache as module
    monkeypatch.setattr(module, 'MAX_ENTRIES', 2)
    calls = []
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=png())
    client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    cache = HubAvatarCache()
    root = 'https://openjiuwen-market.obs.ap-southeast-1.myhuaweicloud.com/'
    one = await cache.get(root + 'one.png?signature=first')
    assert one
    assert await cache.get(root + 'one.png?signature=second') == one
    assert len(calls) == 1
    await cache.get(root + 'two.png')
    await cache.get(root + 'three.png')
    assert len(cache.entries) == 2
    assert 'signature' not in repr(cache.entries)
