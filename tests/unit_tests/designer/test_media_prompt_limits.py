# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Configured-backend prompt length resolution (no forced model)."""

from __future__ import annotations

import os


def test_resolves_minimax_image_1500(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
        resolve_prompt_limit,
    )

    monkeypatch.delenv("VISUAL_GEN_PROMPT_MAX_CHARS", raising=False)
    lim = resolve_prompt_limit("image", model="image-01", provider="minimax")
    assert lim.known
    assert lim.max_chars == 1500
    assert "1500" in lim.guidance()


def test_resolves_qwen_image_3(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
        resolve_prompt_limit,
    )

    monkeypatch.delenv("VISUAL_GEN_PROMPT_MAX_CHARS", raising=False)
    lim = resolve_prompt_limit("image", model="qwen-image-3.0", provider="dashscope")
    assert lim.known
    assert lim.max_chars == 18000


def test_resolves_minimax_h3_video_7000(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
        resolve_prompt_limit,
    )

    monkeypatch.delenv("VIDEO_GEN_PROMPT_MAX_CHARS", raising=False)
    lim = resolve_prompt_limit("video", model="MiniMax-H3-Max", provider="minimax")
    assert lim.known
    assert lim.max_chars == 7000


def test_resolves_hailuo_legacy_2000(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
        resolve_prompt_limit,
    )

    monkeypatch.delenv("VIDEO_GEN_PROMPT_MAX_CHARS", raising=False)
    lim = resolve_prompt_limit("video", model="MiniMax-Hailuo-02", provider="minimax")
    assert lim.known
    assert lim.max_chars == 2000


def test_resolves_wan_and_seedance(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
        resolve_prompt_limit,
    )

    monkeypatch.delenv("VIDEO_GEN_PROMPT_MAX_CHARS", raising=False)
    wan = resolve_prompt_limit("video", model="wan2.2-i2v", provider="dashscope")
    assert wan.known and wan.max_chars == 20000
    seed = resolve_prompt_limit("video", model="seedance-2.5", provider="bytedance")
    assert seed.known and seed.max_chars == 10000


def test_unknown_backend_soft_default(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
        resolve_prompt_limit,
    )

    monkeypatch.delenv("VISUAL_GEN_PROMPT_MAX_CHARS", raising=False)
    monkeypatch.delenv("VISUAL_GEN_MODEL_NAME", raising=False)
    monkeypatch.delenv("VISUAL_GEN_PROVIDER", raising=False)
    monkeypatch.delenv("VISUAL_GEN_API_BASE", raising=False)
    lim = resolve_prompt_limit("image", model="my-custom-lab-image-v9", provider="acme")
    assert not lim.known
    assert lim.soft
    assert lim.max_chars == 4000
    assert "soft advisory" in lim.guidance().lower() or "advisory" in lim.guidance().lower()


def test_env_override_wins(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
        resolve_prompt_limit,
    )

    monkeypatch.setenv("VIDEO_GEN_PROMPT_MAX_CHARS", "3333")
    lim = resolve_prompt_limit("video", model="MiniMax-H3-Max", provider="minimax")
    assert lim.max_chars == 3333
    assert lim.source == "env override"
    monkeypatch.delenv("VIDEO_GEN_PROMPT_MAX_CHARS", raising=False)


def test_trim_only_for_known_hard_cap() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
        PromptLimit,
        trim_prompt_to_limit,
    )

    hard = PromptLimit(
        kind="image",
        model="image-01",
        provider="minimax",
        max_chars=20,
        unit="chars",
        known=True,
        soft=False,
        source="test",
    )
    text, did = trim_prompt_to_limit("a" * 50 + ". more text here.", hard)
    assert did
    assert len(text) <= 20

    soft = PromptLimit(
        kind="image",
        model="x",
        provider="",
        max_chars=20,
        unit="chars",
        known=False,
        soft=True,
        source="soft",
    )
    text2, did2 = trim_prompt_to_limit("a" * 50, soft)
    assert not did2
    assert len(text2) == 50


def test_audio_locks_delegates_guidance(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.audio_locks import (
        image_prompt_limit_guidance,
        video_prompt_limit_guidance,
    )

    monkeypatch.delenv("VISUAL_GEN_PROMPT_MAX_CHARS", raising=False)
    monkeypatch.delenv("VIDEO_GEN_PROMPT_MAX_CHARS", raising=False)
    img = image_prompt_limit_guidance()
    vid = video_prompt_limit_guidance()
    assert "PROMPT LIMIT" in img
    assert "PROMPT LIMIT" in vid
    assert "call_image_model" in img
    assert "call_video_model" in vid
