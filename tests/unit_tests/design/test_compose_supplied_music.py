# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_CLIP,
    NODE_ROLE_COMPOSE,
    NODE_TYPE_AUDIO,
    NODE_TYPE_VIDEO,
    SCHEMA_VERSION,
    normalize_execution_graph,
)
from jiuwenswarm.server.runtime.designer.handlers import compose as compose_mod
from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

_PROBE_WITH_AUDIO = (
    "Duration: 00:00:10.00, start: 0.000000, bitrate: 1000 kb/s\n"
    "Stream #0:0: Video: h264, yuv420p, 1280x720, 24 fps\n"
    "Stream #0:1: Audio: aac, 44100 Hz, stereo"
)


def _graph(*, music_node: bool = False) -> dict:
    nodes: list[dict] = [
        {
            "id": "n_clip_1",
            "type": NODE_TYPE_VIDEO,
            "label": "clip 1",
            "config": {"role": NODE_ROLE_CLIP, "clip_index": 1},
        },
        {
            "id": "n_clip_2",
            "type": NODE_TYPE_VIDEO,
            "label": "clip 2",
            "config": {"role": NODE_ROLE_CLIP, "clip_index": 2, "speech_line": "早上好"},
        },
    ]
    edges = [
        {"id": "e_1", "source": "n_clip_1", "target": "n_compose"},
        {"id": "e_2", "source": "n_clip_2", "target": "n_compose"},
    ]
    if music_node:
        nodes.append(
            {"id": "n_music", "type": NODE_TYPE_AUDIO, "label": "BGM", "config": {"role": "music"}}
        )
        edges.append({"id": "e_3", "source": "n_music", "target": "n_compose"})
    nodes.append(
        {
            "id": "n_compose",
            "type": NODE_TYPE_VIDEO,
            "label": "成片",
            "config": {"role": NODE_ROLE_COMPOSE, "inputs": [e["source"] for e in edges]},
        }
    )
    return normalize_execution_graph(
        {
            "schema_version": SCHEMA_VERSION,
            "graph_id": "graph_bgm01",
            "project_id": "proj_bgm01",
            "title": "成片",
            "nodes": nodes,
            "edges": edges,
        }
    )


def _clips(tmp_path: Path) -> tuple[Path, Path]:
    clip1 = tmp_path / "shot1.mp4"
    clip2 = tmp_path / "shot2.mp4"
    clip1.write_bytes(b"m" * 600)
    clip2.write_bytes(b"m" * 600)
    return clip1.resolve(), clip2.resolve()


def _ctx(graph: dict, clip1: Path, clip2: Path, *, music: Path | None = None) -> NodeExecutionContext:
    states: dict[str, dict] = {
        "n_clip_1": {
            "status": "completed",
            "output_ref": file_output_ref(clip1, kind=NODE_TYPE_VIDEO, mime_type="video/mp4"),
        },
        "n_clip_2": {
            "status": "completed",
            "output_ref": file_output_ref(clip2, kind=NODE_TYPE_VIDEO, mime_type="video/mp4"),
        },
    }
    if music is not None:
        states["n_music"] = {
            "status": "completed",
            "output_ref": file_output_ref(music, kind=NODE_TYPE_AUDIO, mime_type="audio/mp4"),
        }
    return NodeExecutionContext(
        graph=graph, run_id="run_bgm01", node_id="n_compose", run={"node_states": states}
    )


@pytest.fixture
def ffmpeg_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if len(cmd) == 3 and cmd[1] == "-i":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=_PROBE_WITH_AUDIO)
        Path(cmd[-1]).write_bytes(b"x" * 600)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(compose_mod, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(compose_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.compose._clip_video_usable",
        lambda path, ffmpeg=None: Path(path).is_file(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.audio_locks.resolve_video_audio_request",
        lambda cfg, meta: (bool((cfg or {}).get("speech_line")), None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.user_references.user_reference_audio_path",
        lambda graph: None,
    )
    return calls


def _upload_audio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    upload = tmp_path / "upload.mp3"
    upload.write_bytes(b"a" * 600)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.user_references.user_reference_audio_path",
        lambda graph: upload,
    )
    return upload


def _rewrite_of(calls: list[list[str]], source: Path) -> list[str]:
    return next(cmd for cmd in calls if "-c:v" in cmd and cmd[cmd.index("-i") + 1] == str(source))


def test_uploaded_music_keeps_only_the_dialogue_clips_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ffmpeg_calls: list[list[str]]
) -> None:
    _upload_audio(monkeypatch, tmp_path)
    clip1, clip2 = _clips(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    inputs = compose_mod._clips_for_supplied_music(
        _ctx(_graph(), clip1, clip2), [clip1, clip2], scratch
    )

    assert [path.parent for path in inputs] == [scratch.resolve(), scratch.resolve()]
    assert "-an" in _rewrite_of(ffmpeg_calls, clip1)
    dialogue = _rewrite_of(ffmpeg_calls, clip2)
    assert "-an" not in dialogue
    assert dialogue[dialogue.index("-af") + 1] == "highpass=f=150,lowpass=f=4000"


def test_uploaded_music_leaves_wired_user_videos_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ffmpeg_calls: list[list[str]]
) -> None:
    _upload_audio(monkeypatch, tmp_path)
    clip1, clip2 = _clips(tmp_path)
    upload = tmp_path / "user_video.mp4"
    upload.write_bytes(b"m" * 600)

    inputs = compose_mod._clips_for_supplied_music(
        _ctx(_graph(), clip1, clip2), [clip1, clip2, upload.resolve()], tmp_path
    )

    assert inputs[2] == upload.resolve()
    assert not any(cmd[cmd.index("-i") + 1] == str(upload.resolve()) for cmd in ffmpeg_calls if "-c:v" in cmd)


def test_without_supplied_music_clips_keep_their_audio(
    tmp_path: Path, ffmpeg_calls: list[list[str]]
) -> None:
    clip1, clip2 = _clips(tmp_path)

    inputs = compose_mod._clips_for_supplied_music(
        _ctx(_graph(), clip1, clip2), [clip1, clip2], tmp_path
    )

    assert inputs == [clip1, clip2]
    assert not any("-an" in cmd for cmd in ffmpeg_calls)


def test_silent_music_placeholder_is_not_supplied_music(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ffmpeg_calls: list[list[str]]
) -> None:
    _upload_audio(monkeypatch, tmp_path)
    placeholder = tmp_path / "designer_music_run_bgm01_n_music_placeholder_silent.m4a"
    placeholder.write_bytes(b"a" * 600)
    clip1, clip2 = _clips(tmp_path)

    inputs = compose_mod._clips_for_supplied_music(
        _ctx(_graph(music_node=True), clip1, clip2, music=placeholder), [clip1, clip2], tmp_path
    )

    assert inputs == [clip1, clip2]


def test_music_node_output_is_supplied_music(
    tmp_path: Path, ffmpeg_calls: list[list[str]]
) -> None:
    music = tmp_path / "designer_music_run_bgm01_n_music.mp3"
    music.write_bytes(b"a" * 600)
    clip1, clip2 = _clips(tmp_path)

    assert compose_mod._film_has_supplied_music(
        _ctx(_graph(music_node=True), clip1, clip2, music=music)
    )


def test_overlay_leaves_the_film_audio_unfiltered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ffmpeg_calls: list[list[str]]
) -> None:
    _upload_audio(monkeypatch, tmp_path)
    clip1, clip2 = _clips(tmp_path)
    film = tmp_path / "film.mp4"
    film.write_bytes(b"m" * 600)

    compose_mod.mix_compose_soundtrack(
        film, tmp_path / "film_bgm.mp4", ctx=_ctx(_graph(), clip1, clip2)
    )

    overlay = next(cmd for cmd in ffmpeg_calls if "[aout]" in cmd)
    assert "highpass" not in overlay[overlay.index("-filter_complex") + 1]


@pytest.mark.asyncio
async def test_failed_score_falls_back_to_the_clips_own_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ffmpeg_calls: list[list[str]]
) -> None:
    _upload_audio(monkeypatch, tmp_path)
    clip1, clip2 = _clips(tmp_path)
    graph = _graph()
    joined: list[list[Path]] = []

    def fake_concat(paths: list[Path], dest: Path) -> Path:
        joined.append(list(paths))
        Path(dest).write_bytes(b"m" * 600)
        return Path(dest).resolve()

    monkeypatch.setattr(compose_mod, "concatenate_clip_videos", fake_concat)
    monkeypatch.setattr(compose_mod.handler_io, "canvas_output_dir", lambda graph: tmp_path)
    monkeypatch.setattr(
        compose_mod, "mix_compose_soundtrack", lambda video, dest, ctx=None, brief="": video
    )

    result = await compose_mod.ComposeNodeHandler().execute(
        graph["nodes"][-1], _ctx(graph, clip1, clip2)
    )

    assert result.output_ref is not None
    assert len(joined) == 2
    assert joined[0] != [clip1, clip2]
    assert joined[1] == [clip1, clip2]
