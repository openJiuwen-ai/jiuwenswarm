from __future__ import annotations

import asyncio
import base64
import struct
import zlib
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
    reset_current_multimodal_image_files,
    set_current_multimodal_image_files,
)
from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)


def _make_context() -> SimpleNamespace:
    return SimpleNamespace(_window_mutators=[])


def _png_bytes(rgb: tuple[int, int, int] = (0, 200, 60)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return (
            struct.pack(">I", len(data))
            + payload
            + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb)
    idat = zlib.compress(row)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.fixture()
def image_file(tmp_path):
    path = tmp_path / "green.png"
    path.write_bytes(_png_bytes())
    return {
        "type": "image",
        "filename": "green.png",
        "path": str(path),
        "mime_type": "image/png",
    }


@pytest.mark.asyncio
async def test_turn_bound_images_install_mutator_without_contextvar(image_file) -> None:
    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(object())
    rail.bind_turn_image_files([image_file])

    context = _make_context()
    await rail.before_model_call(SimpleNamespace(context=context))

    assert len(context._window_mutators) == 1


@pytest.mark.asyncio
async def test_turn_binding_survives_task_context_isolation(image_file) -> None:
    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(object())
    rail.bind_turn_image_files([image_file])

    async def _supervisor_round() -> None:
        context = _make_context()
        await rail.before_model_call(SimpleNamespace(context=context))
        assert len(context._window_mutators) == 1

    await asyncio.create_task(_supervisor_round())


@pytest.mark.asyncio
async def test_bound_empty_wins_until_restore_reopens_contextvar(image_file) -> None:
    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(object())
    token = set_current_multimodal_image_files([image_file])
    try:
        bound = rail.bind_turn_image_files([])
        context = _make_context()
        await rail.before_model_call(SimpleNamespace(context=context))
        assert context._window_mutators == []

        rail.restore_turn_image_files(bound)
        assert rail._turn_image_files is None
        context = _make_context()
        await rail.before_model_call(SimpleNamespace(context=context))
        assert len(context._window_mutators) == 1
    finally:
        reset_current_multimodal_image_files(token)


@pytest.mark.asyncio
async def test_restore_respects_ownership_token(image_file) -> None:
    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(object())

    token_a = rail.bind_turn_image_files([image_file])
    files_b = [dict(image_file, filename="b.png")]
    token_b = rail.bind_turn_image_files(files_b)

    rail.restore_turn_image_files(token_a)
    assert rail._turn_image_files == files_b

    rail.restore_turn_image_files(token_b)
    assert rail._turn_image_files is None
    assert rail._turn_binding_token is None


@pytest.mark.asyncio
async def test_restore_ignores_unknown_and_absent_tokens() -> None:
    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(object())

    rail.restore_turn_image_files(object())
    rail.restore_turn_image_files(None)
    assert rail._turn_image_files is None

    token = rail.bind_turn_image_files([{"path": "/tmp/x.png", "mime_type": "image/png"}])
    rail.restore_turn_image_files(object())
    assert rail._turn_image_files == [{"path": "/tmp/x.png", "mime_type": "image/png"}]
    rail.restore_turn_image_files(token)
    assert rail._turn_image_files is None


@pytest.mark.asyncio
async def test_installed_mutator_executes_and_injects_image_url(image_file) -> None:
    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(object())
    rail.bind_turn_image_files([image_file])

    context = _make_context()
    await rail.before_model_call(SimpleNamespace(context=context))
    assert len(context._window_mutators) == 1

    window = SimpleNamespace(
        context_messages=[{"role": "user", "content": "what color is this?"}]
    )
    updated = await context._window_mutators[0](context, window)
    assert updated is not None
    parts = updated.context_messages[0]["content"]
    assert parts[0]["type"] == "text"
    assert parts[-1]["type"] == "image_url"
    url = parts[-1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == _png_bytes()