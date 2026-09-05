# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for MultimodalImageRail."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.prompt import user_prompt_builder as upb
from jiuwenswarm.agents.harness.common.rails import multimodal_image_rail as _rail_mod
from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


class _Message:
    def __init__(self, content: Any) -> None:
        self.role = "user"
        self.content = content


class _Context:
    """Minimal stand-in for Core's ``ModelContext``."""

    def __init__(self, messages: list[_Message] | None = None) -> None:
        self._window_mutators: list[Any] = []
        self._messages = messages or []

    def get_messages(self) -> list[_Message]:
        return self._messages


@pytest.fixture
def read_counter(monkeypatch):
    calls: list[str] = []
    original = Path.read_bytes

    def counting_read_bytes(self: Path) -> bytes:
        calls.append(str(self))
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    return calls


@pytest.fixture
def image_request(tmp_path):
    """Bind one image attachment to a request scope."""

    path = tmp_path / "shot.png"
    path.write_bytes(PNG_HEADER + bytes(64))
    images = [
        {
            "type": "image",
            "filename": "shot.png",
            "path": str(path),
            "mime_type": "image/png",
        }
    ]
    token = upb.set_current_multimodal_image_files(images)
    try:
        yield images
    finally:
        upb.reset_current_multimodal_image_files(token)


def test_repeated_before_model_call_installs_one_mutator(
    image_request, read_counter, monkeypatch
):
    infos: list[str] = []
    monkeypatch.setattr(
        _rail_mod.logger,
        "info",
        lambda message, *args, **kwargs: infos.append(str(message)),
    )

    rail = MultimodalImageRail(enable_image_multimodal=True)
    context = _Context()
    ctx = SimpleNamespace(context=context)

    for _ in range(5):
        asyncio.run(rail.before_model_call(ctx))

    assert len(context._window_mutators) == 1
    assert len(read_counter) == 1
    installs = [
        message
        for message in infos
        if "Installed multimodal image context-window adapter" in message
    ]
    assert len(installs) == 1


def test_disabled_strips_content_and_installs_nothing(image_request, read_counter):
    context = _Context([_Message([{"type": "image_url", "image_url": {"url": "data:x"}}])])
    ctx = SimpleNamespace(context=context)

    asyncio.run(MultimodalImageRail(enable_image_multimodal=False).before_model_call(ctx))

    assert context._window_mutators == []
    assert read_counter == []
    assert context._messages[0].content == upb.IMAGE_CONTENT_OMITTED


def test_disabled_removes_a_previously_installed_mutator(image_request):
    context = _Context()
    ctx = SimpleNamespace(context=context)

    asyncio.run(MultimodalImageRail(enable_image_multimodal=True).before_model_call(ctx))
    assert len(context._window_mutators) == 1

    asyncio.run(MultimodalImageRail(enable_image_multimodal=False).before_model_call(ctx))
    assert context._window_mutators == []


def test_missing_context_is_a_no_op():
    rail = MultimodalImageRail(enable_image_multimodal=True)
    asyncio.run(rail.before_model_call(SimpleNamespace(context=None)))
