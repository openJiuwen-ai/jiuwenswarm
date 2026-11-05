# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Designer BGM generation: backends over mocked HTTP, handler fallback, graph wiring."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from jiuwenswarm.server.runtime.designer import music_generation
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
from jiuwenswarm.server.runtime.designer.music_generation import MusicRequest, generate_music, music_slot

_ENV_KEYS = (
    "MUSIC_GEN_PROTOCOL",
    "MUSIC_GEN_API_BASE",
    "MUSIC_GEN_API_KEY",
    "MUSIC_GEN_MODEL_NAME",
)


@pytest.fixture(autouse=True)
def _clean_music_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MUSIC_GEN_ENABLED", "true")


def _configure(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(f"MUSIC_GEN_{key.upper()}", value)


def _mock_http(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        music_generation,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_record)),
    )
    return seen


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


def test_slot_backend_comes_from_protocol_or_host(monkeypatch: pytest.MonkeyPatch) -> None:
    assert music_slot().backend is None
    assert not music_slot().complete

    _configure(monkeypatch, protocol="vllm-omni", api_base="http://gpu:8091/v1")
    assert music_slot().backend == "vllm-omni"
    assert music_slot().complete

    monkeypatch.delenv("MUSIC_GEN_PROTOCOL")
    _configure(monkeypatch, api_base="https://openrouter.ai/api/v1")
    assert music_slot().backend == "openrouter"
    assert not music_slot().complete
    _configure(monkeypatch, api_key="k")
    assert music_slot().complete

    _configure(monkeypatch, api_base="https://api.minimaxi.com/v1")
    assert music_slot().backend is None


def test_switch_off_disables_a_configured_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.designer.capabilities import detect_audio_backends

    _configure(monkeypatch, protocol="vllm-omni", api_base="http://gpu:8091/v1", enabled="false")
    assert not music_slot().complete
    assert "switched off" in (music_generation.music_problem() or "")
    assert detect_audio_backends()["can_music"] is False


def test_detect_audio_backends_reads_music_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.designer.capabilities import detect_audio_backends

    assert detect_audio_backends()["can_music"] is False
    _configure(monkeypatch, protocol="vllm-omni", api_base="http://gpu:8091/v1", model_name="MiniMax-Music3")
    backends = detect_audio_backends()
    assert backends["can_music"] is True
    assert backends["music"]["tool"] == "call_music_model"
    assert backends["music"]["model"] == "MiniMax-Music3"


@pytest.mark.asyncio
async def test_vllm_omni_music3_sends_tags_and_token_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, protocol="vllm-omni", api_base="http://gpu:8091/v1", model_name="MiniMaxAI/MiniMax-Music3")
    seen = _mock_http(
        monkeypatch,
        lambda _r: httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"}),
    )
    result = await generate_music(
        MusicRequest(caption="warm piano", duration_sec=600), dest_stem=tmp_path / "bgm"
    )
    assert result["music_path"].endswith("bgm.wav")
    assert Path(result["music_path"]).read_bytes() == b"RIFFwav"
    assert str(seen[0].url) == "http://gpu:8091/v1/audio/speech"
    body = _body(seen[0])
    assert body["input"] == "[Intro]\n[Inst]\n[Outro]"
    assert body["instructions"].startswith("warm piano")
    assert "no vocals" in body["instructions"]
    assert body["max_new_tokens"] == 9000
    assert "voice" not in body and "language" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(("seconds", "frames"), [(20, 750), (2, 200), (600, 9000)])
async def test_vllm_omni_yue2_sends_wordless_lyrics_style_and_frame_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds: float, frames: int
) -> None:
    _configure(monkeypatch, protocol="vllm-omni", api_base="http://gpu:8093/v1", model_name="m-a-p/YuE2-3B")
    seen = _mock_http(
        monkeypatch,
        lambda _r: httpx.Response(200, content=b"RIFFyue", headers={"content-type": "audio/wav"}),
    )
    result = await generate_music(
        MusicRequest(caption="lo-fi hip-hop at 85 BPM", duration_sec=seconds), dest_stem=tmp_path / "bgm"
    )
    assert Path(result["music_path"]).read_bytes() == b"RIFFyue"
    assert str(seen[0].url) == "http://gpu:8093/v1/audio/speech"
    body = _body(seen[0])
    assert body["input"] == "[instrumental]"
    assert body["instructions"].startswith("lo-fi hip-hop at 85 BPM")
    assert "no vocals" in body["instructions"]
    assert body["max_new_tokens"] == frames
    assert not {"voice", "language", "stream", "ambient_sound"} & set(body)


@pytest.mark.asyncio
async def test_vllm_omni_discovers_a_served_yue2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, protocol="vllm-omni", api_base="http://gpu:8093/v1")

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m-a-p/YuE2-3B"}]})
        return httpx.Response(200, content=b"RIFFyue", headers={"content-type": "audio/wav"})

    seen = _mock_http(monkeypatch, _handler)
    result = await generate_music(MusicRequest(caption="warm piano", duration_sec=10), dest_stem=tmp_path / "bgm")
    assert result["detail"] == "vLLM-Omni m-a-p/YuE2-3B"
    assert _body(seen[1])["input"] == "[instrumental]"


@pytest.mark.asyncio
async def test_vllm_omni_moss_discovers_model_and_sends_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, protocol="vllm-omni", api_base="http://gpu:8092/v1")

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "OpenMOSS/MOSS-SoundEffect"}]})
        return httpx.Response(200, content=b"RIFFmoss", headers={"content-type": "audio/wav"})

    seen = _mock_http(monkeypatch, _handler)
    result = await generate_music(MusicRequest(caption="rain bed", duration_sec=12), dest_stem=tmp_path / "bgm")
    assert "music_path" in result
    body = _body(seen[1])
    assert body["model"] == "OpenMOSS/MOSS-SoundEffect"
    assert body["ambient_sound"].startswith("rain bed")
    assert body["duration_seconds"] == 12
    assert "instructions" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("models", "expected"),
    [
        (httpx.Response(404, json={"detail": "Not Found"}), "GET /models returned HTTP 404 (the address usually ends with /v1)"),
        (httpx.Response(200, json={"data": [{"id": None}]}), "reported no served model"),
    ],
)
async def test_vllm_omni_discovery_reports_the_real_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, models: httpx.Response, expected: str
) -> None:
    _configure(monkeypatch, protocol="vllm-omni", api_base="http://127.0.0.1:8002")
    seen = _mock_http(monkeypatch, lambda _r: models)
    result = await generate_music(MusicRequest(caption="x", duration_sec=8), dest_stem=tmp_path / "bgm")
    assert expected in result["error"]
    assert "'None'" not in result["error"]
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_vllm_omni_rejects_non_music_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, protocol="vllm-omni", api_base="http://gpu:8091/v1", model_name="Qwen3-TTS")
    seen = _mock_http(monkeypatch, lambda _r: httpx.Response(500))
    result = await generate_music(MusicRequest(caption="x", duration_sec=8), dest_stem=tmp_path / "bgm")
    assert "Qwen3-TTS" in result["error"]
    assert seen == []


@pytest.mark.asyncio
async def test_vllm_omni_http_error_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, protocol="vllm-omni", api_base="http://gpu:8091/v1", model_name="MiniMax-Music3")
    _mock_http(monkeypatch, lambda _r: httpx.Response(400, json={"error": {"message": "instructions too long"}}))
    result = await generate_music(MusicRequest(caption="x", duration_sec=8), dest_stem=tmp_path / "bgm")
    assert result["error"].startswith("[ERROR]: vLLM-Omni MiniMax-Music3: HTTP 400")
    assert "instructions too long" in result["error"]


@pytest.mark.asyncio
async def test_minimax_cloud_is_not_a_music_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, protocol="minimax", api_base="https://api.minimax.io", api_key="k")
    seen = _mock_http(monkeypatch, lambda _r: httpx.Response(500))
    assert music_slot().backend is None
    result = await generate_music(MusicRequest(caption="x", duration_sec=8), dest_stem=tmp_path / "bgm")
    assert "MiniMax Music3 on vLLM-Omni" in result["error"]
    assert seen == []


def _sse(*chunks: dict[str, Any] | str) -> httpx.Response:
    lines = [": OPENROUTER PROCESSING"]
    for chunk in chunks:
        lines.append(f"data: {chunk if isinstance(chunk, str) else json.dumps(chunk)}")
    return httpx.Response(
        200,
        content=("\n\n".join(lines) + "\n\n").encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )


def _audio_chunk(raw: bytes) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {"audio": {"data": base64.b64encode(raw).decode("ascii")}}}]}


@pytest.mark.asyncio
async def test_lyria_streams_and_joins_decoded_pieces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, protocol="openrouter", api_key="k")
    seen = _mock_http(
        monkeypatch,
        lambda _r: _sse(
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "[[A0]]"}}]},
            _audio_chunk(b"ID3"),
            _audio_chunk(b"mp3"),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"cost": 0.08}},
            "[DONE]",
        ),
    )
    result = await generate_music(MusicRequest(caption="lofi", duration_sec=30), dest_stem=tmp_path / "bgm")
    assert Path(result["music_path"]).read_bytes() == b"ID3mp3"
    assert result["music_path"].endswith("bgm.mp3")
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer k"
    body = _body(seen[0])
    assert body["model"] == "google/lyria-3-pro-preview"
    assert body["modalities"] == ["text", "audio"]
    assert body["stream"] is True
    prompt = body["messages"][0]["content"]
    assert prompt == "lofi\nA purely instrumental piece.\nLength: about 30 seconds."


@pytest.mark.asyncio
async def test_lyria_without_audio_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, protocol="openrouter", api_key="k", model_name="google/lyria-3-clip-preview")
    _mock_http(
        monkeypatch,
        lambda _r: _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}, "[DONE]"),
    )
    result = await generate_music(MusicRequest(caption="x", duration_sec=20), dest_stem=tmp_path / "bgm")
    assert result["error"].startswith(
        "[ERROR]: OpenRouter google/lyria-3-clip-preview streamed no audio (finish_reason=stop)"
    )


@pytest.mark.asyncio
async def test_lyria_mid_stream_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, protocol="openrouter", api_key="k")
    _mock_http(
        monkeypatch,
        lambda _r: _sse(
            _audio_chunk(b"ID3"),
            {"error": {"code": 502, "message": "upstream failed"}, "choices": [{"finish_reason": "error"}]},
        ),
    )
    result = await generate_music(MusicRequest(caption="x", duration_sec=20), dest_stem=tmp_path / "bgm")
    assert result["error"] == "[ERROR]: OpenRouter google/lyria-3-pro-preview: upstream failed"


@pytest.mark.asyncio
async def test_lyria_http_error_names_the_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, api_base="https://openrouter.ai/api/v1", api_key="k")
    _mock_http(
        monkeypatch,
        lambda _r: httpx.Response(402, json={"error": {"code": 402, "message": "Insufficient credits"}}),
    )
    result = await generate_music(MusicRequest(caption="x", duration_sec=20), dest_stem=tmp_path / "bgm")
    assert result["error"] == (
        "[ERROR]: OpenRouter google/lyria-3-pro-preview: HTTP 402 insufficient credits Insufficient credits"
    )


@pytest.mark.asyncio
async def test_unconfigured_slot_is_an_error(tmp_path: Path) -> None:
    result = await generate_music(MusicRequest(caption="x", duration_sec=8), dest_stem=tmp_path / "bgm")
    assert "MUSIC_GEN_PROTOCOL" in result["error"]


def _music_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[str]) -> NodeExecutionContext:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.audio_nodes.graph_workspace_dir",
        lambda _graph: tmp_path,
    )
    graph = {
        "description": "a rainy street reunion",
        "nodes": [
            {"id": "n_music", "type": "audio", "config": {"role": "music"}},
            {"id": "n_clip_1", "type": "video", "config": {"pipeline": "clip", "duration_sec": 5}},
            {"id": "n_clip_2", "type": "video", "config": {"pipeline": "clip", "duration_sec": 8}},
        ],
        "metadata": {"bgm_lock": {"mood": "bittersweet", "style": "solo piano"}},
    }
    return NodeExecutionContext(
        graph=graph,
        run_id="run_m",
        node_id="n_music",
        emit_activity=lambda _kind, text, _tool: events.append(text),
    )


@pytest.mark.asyncio
async def test_render_bgm_uses_film_length_and_bgm_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.designer.handlers import audio_nodes

    _configure(monkeypatch, protocol="vllm-omni", api_base="http://gpu:8091/v1", model_name="MiniMax-Music3")
    seen = _mock_http(
        monkeypatch,
        lambda _r: httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"}),
    )
    events: list[str] = []
    ctx = _music_ctx(tmp_path, monkeypatch, events)
    result = await audio_nodes.render_bgm(ctx.graph["nodes"][0], ctx, instructions="slow build")
    assert result.output_ref is not None
    assert result.output_ref["label"] == "designer_music_run_m_n_music.wav"
    assert result.output_ref["mime_type"] == "audio/wav"
    body = _body(seen[0])
    assert body["max_new_tokens"] == round(13 * 1.5 * 25)
    assert body["instructions"].startswith("slow build\nmood: bittersweet\nstyle: solo piano")
    assert "[0:00 - 0:05] Intro" in body["instructions"]
    assert "Accent the downbeats at the picture cuts: 0:05." in body["instructions"]
    assert events == []


def _wav_bytes(seconds: float) -> bytes:
    import io
    import wave

    import numpy as np

    rate = 22050
    t = np.arange(int(seconds * rate)) / rate
    pcm = (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned", "label", "message", "published_seconds", "raw_kept"),
    [
        (60.0, "designer_music_run_m_n_music.m4a", "13s (trimmed from 60s)", 13.0, True),
        (17.0, "designer_music_run_m_n_music.wav", "17s", 17.0, False),
    ],
)
async def test_render_bgm_cuts_an_overlong_bed_to_the_film(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returned: float,
    label: str,
    message: str,
    published_seconds: float,
    raw_kept: bool,
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers import audio_nodes
    from jiuwenswarm.server.runtime.designer.music_align import track_seconds

    pytest.importorskip("imageio_ffmpeg")
    _configure(monkeypatch, protocol="vllm-omni", api_base="http://gpu:8091/v1", model_name="MiniMax-Music3")
    audio = _wav_bytes(returned)
    _mock_http(monkeypatch, lambda _r: httpx.Response(200, content=audio, headers={"content-type": "audio/wav"}))
    ctx = _music_ctx(tmp_path, monkeypatch, [])

    result = await audio_nodes.render_bgm(ctx.graph["nodes"][0], ctx)

    assert result.output_ref is not None
    assert result.output_ref["label"] == label
    assert result.message.endswith(message)
    seconds = track_seconds(audio_nodes._find_ffmpeg() or "", tmp_path / label)
    assert seconds is not None and abs(seconds - published_seconds) < 0.3
    assert (tmp_path / "designer_music_run_m_n_music_raw.wav").is_file() == raw_kept


@pytest.mark.asyncio
async def test_user_reference_music_is_not_realigned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.designer.handlers import audio_nodes

    song = tmp_path / "theme.mp3"
    song.write_bytes(b"ID3" + b"\0" * 128)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.user_references.user_reference_audio_path", lambda _graph: song
    )
    ctx = _music_ctx(tmp_path, monkeypatch, [])
    result = await audio_nodes.MusicNodeHandler().execute(ctx.graph["nodes"][0], ctx)
    assert result.output_ref is not None
    assert not audio_nodes.is_generated_music(result.output_ref["label"])
    assert audio_nodes.is_generated_music("designer_music_run_m_n_music.mp3")


def test_bgm_caption_keeps_user_words_and_times_the_story_curve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers import audio_nodes

    ctx = _music_ctx(tmp_path, monkeypatch, [])
    ctx.graph["nodes"].append({"id": "n_clip_3", "type": "video", "config": {"pipeline": "clip", "duration_sec": 5}})
    ctx.graph["metadata"]["bgm_lock"] = {
        "user_request": "大气磅礴的现代中国风电子乐",
        "style": "modern Chinese electronic",
        "instruments": "guzheng, erhu, taiko, synth bass",
        "tempo_bpm": "120",
        "key": "D minor",
    }
    ctx.graph["metadata"]["script_analysis"] = {
        "clips": [{"clip_index": 1, "emotion": "setup"}, {"clip_index": 3, "emotion": "release"}]
    }
    for index, node in enumerate(ctx.graph["nodes"][1:], start=1):
        node["config"]["clip_index"] = index
    caption = audio_nodes.bgm_caption(ctx.graph["nodes"][0], ctx, "cinematic hybrid score")
    lines = caption.splitlines()
    assert lines[:2] == ["大气磅礴的现代中国风电子乐", "cinematic hybrid score"]
    assert "key: D minor" in lines
    assert "Total length 0:18; the piece ends at 0:18." in caption
    assert "[0:05 - 0:13] Peak: full arrangement, the biggest hit lands right at 0:05." in caption
    assert "[0:13 - 0:18] Resolve" in caption


@pytest.mark.asyncio
async def test_render_bgm_failure_leaves_silence_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers import audio_nodes

    _configure(monkeypatch, protocol="openrouter", api_key="k")
    _mock_http(monkeypatch, lambda _r: httpx.Response(429, json={"error": {"code": 429, "message": "slow down"}}))
    monkeypatch.setattr(audio_nodes, "_synthesize_bed", lambda dest, **_k: False)
    events: list[str] = []
    ctx = _music_ctx(tmp_path, monkeypatch, events)
    result = await audio_nodes.MusicNodeHandler().execute(ctx.graph["nodes"][0], ctx)
    assert result.output_ref is not None
    assert audio_nodes.is_silent_music_placeholder(result.output_ref["label"])
    assert "WARNING: OpenRouter google/lyria-3-pro-preview: HTTP 429 rate limited" in result.message
    assert len(events) == 1 and "HTTP 429" in events[0]


@pytest.mark.asyncio
async def test_render_bgm_backend_exception_leaves_silence_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers import audio_nodes

    async def broken(*_a: Any, **_k: Any) -> dict[str, str]:
        raise KeyError("choices")

    _configure(monkeypatch, protocol="openrouter", api_key="k")
    monkeypatch.setattr(music_generation, "generate_music", broken)
    monkeypatch.setattr(audio_nodes, "_synthesize_bed", lambda dest, **_k: False)
    events: list[str] = []
    ctx = _music_ctx(tmp_path, monkeypatch, events)
    result = await audio_nodes.MusicNodeHandler().execute(ctx.graph["nodes"][0], ctx)
    assert audio_nodes.is_silent_music_placeholder(result.output_ref["label"])
    assert "WARNING: music backend failed: KeyError" in result.message
    assert len(events) == 1


@pytest.mark.asyncio
async def test_render_bgm_lets_cancellation_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.designer.ffmpeg_runner import FfmpegCancelled
    from jiuwenswarm.server.runtime.designer.handlers import audio_nodes

    async def cancelled(*_a: Any, **_k: Any) -> dict[str, str]:
        raise FfmpegCancelled("design run cancelled")

    _configure(monkeypatch, protocol="openrouter", api_key="k")
    monkeypatch.setattr(music_generation, "generate_music", cancelled)
    ctx = _music_ctx(tmp_path, monkeypatch, [])
    with pytest.raises(FfmpegCancelled):
        await audio_nodes.render_bgm(ctx.graph["nodes"][0], ctx)


@pytest.mark.asyncio
async def test_unconfigured_handler_keeps_quiet_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers import audio_nodes

    monkeypatch.setattr(audio_nodes, "_synthesize_bed", lambda dest, **_k: False)
    events: list[str] = []
    ctx = _music_ctx(tmp_path, monkeypatch, events)
    result = await audio_nodes.MusicNodeHandler().execute(ctx.graph["nodes"][0], ctx)
    assert "no music API" in result.message
    assert "WARNING" not in result.message
    assert events == []


class _DummySpawner:
    async def spawn_node_agent(self, run_id: str, node_id: str) -> str:
        return "ok"

    def apply_agent_graph_patch(self, graph_id: str, patch: dict) -> dict:
        return {}

    def load_graph_snapshot(self, graph_id: str, run_id: str) -> dict:
        return {}


@pytest.mark.asyncio
async def test_call_music_model_tool_completes_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openjiuwen.core.single_agent.ability_manager import AbilityManager

    from jiuwenswarm.server.runtime.designer.node_agent import DesignerGraphToolkit, build_designer_tools

    _configure(monkeypatch, protocol="vllm-omni", api_base="http://gpu:8091/v1", model_name="MiniMax-Music3")
    seen = _mock_http(
        monkeypatch,
        lambda _r: httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"}),
    )
    ctx = _music_ctx(tmp_path, monkeypatch, [])
    ctx.graph["nodes"][0]["config"]["tools"] = ["call_music_model"]
    toolkit = DesignerGraphToolkit(_DummySpawner(), ctx)
    tool = next(t for t in build_designer_tools(toolkit) if t.card.name == "call_music_model")
    assert AbilityManager._resolve_call_timeout(tool.card) == 900.0

    out = await toolkit.call_music_model(instructions="jazz trio")
    assert out.startswith("music_ready")
    assert toolkit.completed is not None
    assert toolkit.completed.output_ref["kind"] == "audio"
    assert _body(seen[0])["max_new_tokens"] == round(13 * 1.5 * 25)


def _graph_with_bgm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    can_music: bool,
    policy: str,
    include_music: bool | None = None,
    prompt: str = "office short film with a warm score",
) -> dict[str, Any]:
    from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.capabilities.detect_audio_backends",
        lambda: {"can_speech": False, "can_music": can_music, "can_video_audio": True, "video_audio_model": "wan"},
    )
    return build_smart_video_graph(
        project_id="p",
        prompt=prompt,
        analysis={
            "source": "llm",
            "audio": {
                "include_music": policy != "silent" if include_music is None else include_music,
                "policy": policy,
            },
            "characters": [{"id": "char_1", "name": "Lead"}],
            "scenes": [{"id": "scene_1", "name": "office"}],
            "clips": [{"clip_index": 1, "action": "monologue", "character_ids": ["char_1"]}],
        },
    )


def test_smart_graph_adds_one_music_node_feeding_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.designer.orchestration import assign_audio_node_agents

    graph = _graph_with_bgm(monkeypatch, can_music=True, policy="optional_music")
    assign_audio_node_agents(graph)
    music = [n for n in graph["nodes"] if n["id"] == "n_music"]
    assert len(music) == 1
    cfg = music[0]["config"]
    assert cfg["delegate"] == "agent"
    assert "call_music_model" in cfg["tools"]
    edges = {(e["source"], e["target"]) for e in graph["edges"]}
    assert ("n_brief", "n_music") in edges
    assert ("n_music", "n_compose") in edges
    compose = next(n for n in graph["nodes"] if n["id"] == "n_compose")
    assert "n_music" in compose["config"]["inputs"]
    assert graph["metadata"]["audio_routing"]["music_nodes"] is True


def test_smart_graph_skips_music_node_when_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _graph_with_bgm(monkeypatch, can_music=True, policy="silent")
    assert "n_music" not in [n["id"] for n in graph["nodes"]]


def test_configured_music_adds_bgm_even_when_analysis_skipped_music(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _graph_with_bgm(monkeypatch, can_music=True, policy="speech", include_music=False)
    assert "n_music" in [n["id"] for n in graph["nodes"]]


def test_no_bgm_node_without_a_music_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _graph_with_bgm(monkeypatch, can_music=False, policy="speech_and_music")
    assert "n_music" not in [n["id"] for n in graph["nodes"]]
    assert graph["metadata"]["audio_routing"]["music_nodes"] is False


def test_no_bgm_node_when_user_declines_music(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _graph_with_bgm(monkeypatch, can_music=True, policy="speech", prompt="办公室短片，不要背景音乐")
    assert "n_music" not in [n["id"] for n in graph["nodes"]]


def test_bgm_caption_leads_with_the_node_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.designer.handlers import audio_nodes

    ctx = _music_ctx(tmp_path, monkeypatch, [])
    node = ctx.graph["nodes"][0]
    node["config"]["prompt"] = "欢快的钢琴小品"
    ctx.graph["metadata"]["bgm_lock"] = {"user_request": "欢快的钢琴小品", "mood": "bright"}
    lines = audio_nodes.bgm_caption(node, ctx, "solo piano, 100 bpm").splitlines()
    assert lines[:3] == ["欢快的钢琴小品", "solo piano, 100 bpm", "mood: bright"]
