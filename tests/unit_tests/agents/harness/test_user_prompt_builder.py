# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for inline multimodal image injection in ``user_prompt_builder``.

The regression these guard: attached images used to be re-read and re-base64-encoded on
every single model call, because the context-window mutator was rebuilt per call and the
encode lived inside the mutator body.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.prompt import user_prompt_builder as upb

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def read_counter(monkeypatch):
    """Count ``Path.read_bytes`` calls per path."""

    calls: list[str] = []
    original = Path.read_bytes

    def counting_read_bytes(self: Path) -> bytes:
        calls.append(str(self))
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    return calls


@pytest.fixture
def image_scope():
    """Enter a request scope and guarantee it is released."""

    tokens: list[Any] = []

    def _enter(image_files: list[dict[str, Any]]):
        token = upb.set_current_multimodal_image_files(image_files)
        tokens.append(token)
        return token

    yield _enter

    for token in reversed(tokens):
        upb.reset_current_multimodal_image_files(token)


def _write_png(tmp_path: Path, name: str, size: int = 64) -> dict[str, Any]:
    path = tmp_path / name
    payload = PNG_HEADER + bytes(max(size - len(PNG_HEADER), 1))
    path.write_bytes(payload)
    return {
        "type": "image",
        "filename": name,
        "path": str(path),
        "mime_type": "image/png",
    }


class _Context:
    """Minimal stand-in for Core's ``ModelContext``."""

    def __init__(self) -> None:
        self._window_mutators: list[Any] = []


class _Window:
    def __init__(self, query: str = "describe this") -> None:
        self.context_messages: list[Any] = [{"role": "user", "content": query}]


def _run(coro):
    return asyncio.run(coro)


def _image_urls(window: Any) -> list[str]:
    parts = window.context_messages[-1]["content"]
    return [
        part["image_url"]["url"]
        for part in parts
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]


def _text_part(window: Any) -> str:
    parts = window.context_messages[-1]["content"]
    return next(part["text"] for part in parts if part.get("type") == "text")


def _drive(context: _Context, window: Any) -> Any:
    """Run every installed mutator over ``window``, as Core does per model call."""

    async def _apply() -> Any:
        result = window
        for mutator in context._window_mutators:
            result = await mutator(context, result)
        return result

    return _run(_apply())


def test_images_are_encoded_once_per_request(tmp_path, image_scope, read_counter):
    images = [_write_png(tmp_path, "a.png"), _write_png(tmp_path, "b.png")]
    image_scope(images)
    context = _Context()

    assert upb.ensure_multimodal_image_window_mutator(context, images) is True

    payloads = []
    for _ in range(5):
        window = _drive(context, _Window())
        payloads.append(_image_urls(window))

    assert [str(Path(image["path"])) for image in images] == sorted(read_counter)
    assert len(read_counter) == 2
    assert all(payload == payloads[0] for payload in payloads)
    assert len(payloads[0]) == 2


def test_install_is_idempotent_within_a_request(tmp_path, image_scope, read_counter):
    images = [_write_png(tmp_path, "a.png")]
    image_scope(images)
    context = _Context()

    assert upb.ensure_multimodal_image_window_mutator(context, images) is True
    assert upb.ensure_multimodal_image_window_mutator(context, images) is False
    assert upb.ensure_multimodal_image_window_mutator(context, images) is False

    assert len(context._window_mutators) == 1
    assert len(read_counter) == 1


def test_changed_image_set_reinstalls_and_reencodes(tmp_path, image_scope, read_counter):
    first = [_write_png(tmp_path, "a.png")]
    second = [_write_png(tmp_path, "b.png")]
    image_scope(first)
    context = _Context()

    assert upb.ensure_multimodal_image_window_mutator(context, first) is True
    assert upb.ensure_multimodal_image_window_mutator(context, second) is True

    assert len(context._window_mutators) == 1
    assert len(read_counter) == 2
    urls = _image_urls(_drive(context, _Window()))
    assert len(urls) == 1


def test_new_request_scope_rebuilds_the_mutator(tmp_path, read_counter):
    """A second request with identical paths must not reuse the first request's encode."""

    images = [_write_png(tmp_path, "a.png")]
    context = _Context()

    first_token = upb.set_current_multimodal_image_files(images)
    assert upb.ensure_multimodal_image_window_mutator(context, images) is True
    assert upb.ensure_multimodal_image_window_mutator(context, images) is False
    upb.reset_current_multimodal_image_files(first_token)

    # The finished request took its mutator with it.
    assert context._window_mutators == []

    second_token = upb.set_current_multimodal_image_files(images)
    try:
        assert upb.ensure_multimodal_image_window_mutator(context, images) is True
        assert len(context._window_mutators) == 1
        assert len(read_counter) == 2
    finally:
        upb.reset_current_multimodal_image_files(second_token)


def test_stale_mutator_from_another_scope_is_replaced(tmp_path):
    """Even if a mutator outlives its request, the next one must not inherit it."""

    images = [_write_png(tmp_path, "a.png")]
    context = _Context()

    first_token = upb.set_current_multimodal_image_files(images)
    upb.ensure_multimodal_image_window_mutator(context, images)
    stale = context._window_mutators[0]
    # Simulate the release failing to unhook the mutator.
    scope = upb._CURRENT_MULTIMODAL_IMAGE_SCOPE.get()
    scope.installed_mutators.clear()
    upb.reset_current_multimodal_image_files(first_token)
    assert context._window_mutators == [stale]

    second_token = upb.set_current_multimodal_image_files(images)
    try:
        assert upb.ensure_multimodal_image_window_mutator(context, images) is True
        assert context._window_mutators != [stale]
        assert len(context._window_mutators) == 1
    finally:
        upb.reset_current_multimodal_image_files(second_token)


def test_text_block_tracks_the_current_message(tmp_path, image_scope, read_counter):
    images = [_write_png(tmp_path, "a.png")]
    image_scope(images)
    context = _Context()
    upb.ensure_multimodal_image_window_mutator(context, images)

    first = _drive(context, _Window("first question"))
    second = _drive(context, _Window("second question"))

    assert "first question" in _text_part(first)
    assert "second question" in _text_part(second)
    assert _image_urls(first) == _image_urls(second)
    assert len(read_counter) == 1


def test_image_url_parts_are_not_aliased_across_calls(tmp_path, image_scope):
    images = [_write_png(tmp_path, "a.png")]
    image_scope(images)
    context = _Context()
    upb.ensure_multimodal_image_window_mutator(context, images)

    first = _drive(context, _Window())
    first_part = first.context_messages[-1]["content"][1]
    first_part["image_url"]["url"] = "mutated"

    second = _drive(context, _Window())
    assert _image_urls(second)[0].startswith("data:image/png;base64,")


def test_image_count_cap(tmp_path, image_scope, monkeypatch):
    monkeypatch.setattr(upb, "_MAX_VISION_INLINE_IMAGE_COUNT", 8)
    images = [_write_png(tmp_path, f"img{index}.png") for index in range(12)]
    image_scope(images)

    data_uris, injected = upb._build_image_url_parts(images)

    assert len(data_uris) == 8
    assert len(injected) == 8


def test_there_is_no_aggregate_size_cap(tmp_path, image_scope, read_counter):
    """Only the per-image and count caps apply; total payload matches today's behaviour."""

    images = [_write_png(tmp_path, f"img{index}.png", size=4096) for index in range(8)]
    image_scope(images)

    data_uris, injected = upb._build_image_url_parts(images)

    assert len(injected) == 8
    assert len(data_uris) == 8
    assert len(read_counter) == 8


def test_per_image_size_cap_rejects_before_reading(
    tmp_path, image_scope, monkeypatch, read_counter
):
    monkeypatch.setattr(upb, "_MAX_VISION_INLINE_IMAGE_BYTES", 100)
    small = _write_png(tmp_path, "small.png", size=50)
    large = _write_png(tmp_path, "large.png", size=500)
    image_scope([small, large])

    _data_uris, injected = upb._build_image_url_parts([small, large])

    assert [image["filename"] for image in injected] == ["small.png"]
    # The over-sized file is rejected on stat(), never loaded into memory.
    assert len(read_counter) == 1


def test_missing_files_install_nothing(tmp_path, image_scope):
    missing = {
        "type": "image",
        "filename": "gone.png",
        "path": str(tmp_path / "gone.png"),
        "mime_type": "image/png",
    }
    image_scope([missing])
    context = _Context()

    assert upb.ensure_multimodal_image_window_mutator(context, [missing]) is False
    assert context._window_mutators == []


def test_directory_is_not_treated_as_an_image(tmp_path, image_scope):
    directory = tmp_path / "notafile.png"
    directory.mkdir()
    record = {
        "type": "image",
        "filename": "notafile.png",
        "path": str(directory),
        "mime_type": "image/png",
    }
    image_scope([record])

    _data_uris, injected = upb._build_image_url_parts([record])
    assert injected == []


def test_snapshot_semantics_survive_a_mid_run_delete(tmp_path, image_scope):
    images = [_write_png(tmp_path, "a.png")]
    image_scope(images)
    context = _Context()
    upb.ensure_multimodal_image_window_mutator(context, images)

    before = _image_urls(_drive(context, _Window()))
    Path(images[0]["path"]).unlink()
    after = _image_urls(_drive(context, _Window()))

    assert before == after
    assert after[0].startswith("data:image/png;base64,")


def test_reset_releases_cache_and_uninstalls_mutators(tmp_path):
    images = [_write_png(tmp_path, "a.png")]
    context = _Context()
    token = upb.set_current_multimodal_image_files(images)
    upb.ensure_multimodal_image_window_mutator(context, images)

    scope = upb._CURRENT_MULTIMODAL_IMAGE_SCOPE.get()
    assert scope is not None and scope.data_uri_cache
    assert len(context._window_mutators) == 1

    upb.reset_current_multimodal_image_files(token)

    assert upb._CURRENT_MULTIMODAL_IMAGE_SCOPE.get() is None
    assert scope.data_uri_cache == {}
    assert context._window_mutators == []


def test_remove_multimodal_image_window_mutator(tmp_path, image_scope):
    images = [_write_png(tmp_path, "a.png")]
    image_scope(images)
    context = _Context()
    upb.ensure_multimodal_image_window_mutator(context, images)

    unrelated = object()
    context._window_mutators.append(unrelated)

    assert upb.remove_multimodal_image_window_mutator(context) == 1
    assert context._window_mutators == [unrelated]
    assert upb.remove_multimodal_image_window_mutator(context) == 0


def test_uninstall_keeps_the_encode_cache(tmp_path, image_scope, read_counter):
    """Removing the mutator must not discard the request's encodes.

    Only ``reset_current_multimodal_image_files`` ends the cache's life; an uninstall
    within a live request (e.g. native image input toggling off then on again) has to
    leave it intact, or the images would be re-encoded on the next install.
    """

    images = [_write_png(tmp_path, "a.png")]
    image_scope(images)
    context = _Context()

    upb.ensure_multimodal_image_window_mutator(context, images)
    assert len(read_counter) == 1

    assert upb.remove_multimodal_image_window_mutator(context) == 1
    scope = upb._CURRENT_MULTIMODAL_IMAGE_SCOPE.get()
    assert scope is not None and scope.data_uri_cache

    assert upb.ensure_multimodal_image_window_mutator(context, images) is True
    assert len(context._window_mutators) == 1
    assert len(read_counter) == 1
    assert _image_urls(_drive(context, _Window()))[0].startswith("data:image/png;base64,")


def test_works_without_a_request_scope(tmp_path, read_counter):
    """Direct callers with no ContextVar set keep today's uncached behaviour."""

    images = [_write_png(tmp_path, "a.png")]
    assert upb._CURRENT_MULTIMODAL_IMAGE_SCOPE.get() is None

    messages = [{"role": "user", "content": "hello"}]
    first, injected_first = upb.prepare_multimodal_image_messages(messages, images)
    _second, injected_second = upb.prepare_multimodal_image_messages(messages, images)

    assert injected_first == 1
    assert injected_second == 1
    assert len(read_counter) == 2
    assert first[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_legacy_bare_token_is_still_accepted(tmp_path):
    """``reset_current_multimodal_image_files`` tolerates a pre-scope token."""

    token = upb._CURRENT_MULTIMODAL_IMAGE_FILES.set(())
    upb.reset_current_multimodal_image_files(token)
    assert upb.current_multimodal_image_files() == []


def test_non_context_object_is_ignored():
    assert upb.ensure_multimodal_image_window_mutator(SimpleNamespace()) is False
    assert upb.remove_multimodal_image_window_mutator(SimpleNamespace()) == 0
