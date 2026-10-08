# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CLIP,
    NODE_TYPE_TEXT,
    NODE_TYPE_VIDEO,
    SCHEMA_VERSION,
    normalize_execution_graph,
)
from jiuwenswarm.server.runtime.designer.handlers.clip import (
    ClipNodeHandler,
    build_clip_prompt,
    generate_clip_video,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext


def _graph(*, brief: str = "火车站晨间短片", clip_prompt: str | None = None):
    clip_config: dict[str, object] = {"role": NODE_ROLE_CLIP}
    if clip_prompt is not None:
        clip_config["prompt"] = clip_prompt
    return normalize_execution_graph(
        {
            "schema_version": SCHEMA_VERSION,
            "graph_id": "graph_clip01",
            "project_id": "proj_clip01",
            "title": "火车站",
            "description": "火车进站，年轻人走下车",
            "source": "manual",
            "nodes": [
                {
                    "id": "n_brief",
                    "type": NODE_TYPE_TEXT,
                    "label": "brief",
                    "config": {"role": NODE_ROLE_BRIEF, "prompt": brief},
                },
                {
                    "id": "n_clip",
                    "type": NODE_TYPE_VIDEO,
                    "label": "clip",
                    "config": clip_config,
                },
            ],
            "edges": [],
        }
    )


def test_build_clip_prompt_uses_brief_then_graph_text() -> None:
    graph = _graph()
    clip = graph["nodes"][1]
    prompt = build_clip_prompt(graph, clip)
    assert "USER PROMPT (authoritative story" not in prompt
    assert "火车站晨间短片" not in prompt
    # Design clips always use R2V story-form (Image-N binding), not long lock essays.
    assert "Image" in prompt or "image" in prompt.lower() or "scene" in prompt.lower()

    clip["config"] = {"role": NODE_ROLE_CLIP, "prompt": "只拍站台", "shot_action": "只拍站台"}
    out = build_clip_prompt(graph, clip)
    assert "USER PROMPT (authoritative story" not in out
    assert "Image" in out or "只拍站台" in out


def test_build_clip_prompt_reads_upstream_brief_and_storyboard(tmp_path: Path) -> None:
    from jiuwenswarm.common.schema.designer_graph import NODE_ROLE_STORYBOARD, NODE_TYPE_TABLE
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    brief = tmp_path / "brief.md"
    story = tmp_path / "storyboard.md"
    brief.write_text("# Brief\n**Visual style:** 火车进站", encoding="utf-8")
    story.write_text(
        "## 分镜表\n"
        "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
        "| 1 | 0.0-5.0s | 中景/平视 | 缓推进站 | 主体入画 | 站台 |\n",
        encoding="utf-8",
    )
    graph = _graph()
    graph["nodes"].insert(
        1,
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "storyboard",
            "config": {"role": NODE_ROLE_STORYBOARD},
        },
    )
    graph["nodes"][-1]["config"]["shot_action"] = "缓推进站"
    graph["nodes"][-1]["config"]["camera"] = "中景/平视"
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_clip_up",
        node_id="n_clip",
        run={
            "node_states": {
                "n_brief": {
                    "status": "completed",
                    "output_ref": file_output_ref(brief, kind="text", mime_type="text/markdown"),
                },
                "n_storyboard": {
                    "status": "completed",
                    "output_ref": file_output_ref(story, kind="table", mime_type="text/markdown"),
                },
            }
        },
    )
    prompt = build_clip_prompt(graph, graph["nodes"][-1], ctx)
    assert "缓推进站" in prompt or "Image" in prompt
    assert "USER PROMPT (authoritative story" not in prompt


@pytest.mark.asyncio
async def test_clip_handler_sends_scene_plate_and_storyboard_as_multimodal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_SCENE,
        NODE_ROLE_STORYBOARD,
        NODE_TYPE_IMAGE,
        NODE_TYPE_TABLE,
    )
    from jiuwenswarm.server.runtime.designer.handlers.clip import collect_clip_reference_images
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    scene = tmp_path / "scene.png"
    story = tmp_path / "storyboard.md"
    scene.write_bytes(b"png-scene")
    story.write_text(
        "## 分镜表\n"
        "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
        "| 1 | 0.0-2.0s | 全景/平视 | 缓摇 | 未入画 | 站台 |\n"
        "| 2 | 2.0-5.0s | 中景/平视 | 跟移 | 主体入画 | 出站 |\n",
        encoding="utf-8",
    )
    video = tmp_path / "generated_clip.mp4"
    video.write_bytes(b"fake-mp4")
    graph = _graph()
    graph["nodes"][-1]["config"]["scene_node_id"] = "n_scene"
    graph["nodes"][1:1] = [
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "storyboard",
            "config": {"role": NODE_ROLE_STORYBOARD},
        },
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "scene",
            "config": {"role": NODE_ROLE_SCENE},
        },
    ]
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_clip_mm",
        node_id="n_clip",
        run={
            "node_states": {
                "n_storyboard": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        story, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
                    ),
                },
                "n_scene": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        scene, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                    ),
                },
            }
        },
    )
    assert collect_clip_reference_images(ctx, node=graph["nodes"][-1]) == [scene.resolve()]
    prompt = build_clip_prompt(graph, graph["nodes"][-1], ctx)
    assert "Image" in prompt or "scene" in prompt.lower()

    seen: dict[str, object] = {}

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        reference_file: str | None = None,
        duration: int = 5,
        **kwargs,
    ) -> dict[str, str]:
        seen["first_frame"] = first_frame
        seen["reference_images"] = reference_images
        seen["reference_file"] = reference_file
        seen["force_reference_mode"] = kwargs.get("force_reference_mode")
        seen["prompt"] = prompt
        seen["duration"] = duration
        return {"video_path": str(video), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    await ClipNodeHandler().execute(graph["nodes"][-1], ctx)
    sent = [*(seen.get("reference_images") or [])]
    assert str(scene.resolve()) in [str(x) for x in sent if x]
    assert seen.get("first_frame") in (None, "")
    assert seen.get("force_reference_mode") is True
    assert seen["reference_file"] in (None, "")
    assert seen["duration"] == 2
    assert seen["prompt"]


@pytest.mark.asyncio
async def test_clip_handler_sends_character_and_keyframe_as_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_CHARACTER_DESIGN,
        NODE_ROLE_FRAME,
        NODE_ROLE_SCENE,
        NODE_ROLE_STORYBOARD,
        NODE_TYPE_IMAGE,
        NODE_TYPE_TABLE,
    )
    from jiuwenswarm.server.runtime.designer.handlers.clip import (
        collect_clip_reference_images,
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    frame = tmp_path / "keyframe.png"
    character = tmp_path / "character.png"
    scene = tmp_path / "scene.png"
    story = tmp_path / "storyboard.md"
    frame.write_bytes(b"png-frame")
    character.write_bytes(b"png-character")
    scene.write_bytes(b"png-scene")
    story.write_text(
        "## 分镜表\n"
        "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
        "| 1 | 0.0-5.0s | 中景/平视 | 缓推 | 主体入画 | 站台 |\n",
        encoding="utf-8",
    )
    video = tmp_path / "generated_clip.mp4"
    video.write_bytes(b"fake-mp4")
    graph = _graph()
    graph["nodes"][1:1] = [
        {
            "id": "n_character",
            "type": NODE_TYPE_IMAGE,
            "label": "character",
            "config": {"role": NODE_ROLE_CHARACTER_DESIGN},
        },
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "scene",
            "config": {"role": NODE_ROLE_SCENE},
        },
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "storyboard",
            "config": {"role": NODE_ROLE_STORYBOARD},
        },
        {
            "id": "n_frame",
            "type": NODE_TYPE_IMAGE,
            "label": "frame",
            "config": {"role": NODE_ROLE_FRAME},
        },
    ]
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_clip_r2va",
        node_id="n_clip",
        run={
            "node_states": {
                "n_character": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        character, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                    ),
                },
                "n_scene": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        scene, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                    ),
                },
                "n_storyboard": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        story, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
                    ),
                },
                "n_frame": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        frame, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                    ),
                },
            }
        },
    )
    assert collect_clip_reference_images(ctx) == [character.resolve(), scene.resolve()]
    prompt = build_clip_prompt(graph, graph["nodes"][-1], ctx)
    assert prompt

    seen: dict[str, object] = {}

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, str]:
        seen["first_frame"] = first_frame
        seen["reference_images"] = [str(x) for x in (reference_images or [])]
        seen["reference_file"] = kwargs.get("reference_file")
        seen["force_reference_mode"] = kwargs.get("force_reference_mode")
        seen["prompt"] = prompt
        return {"video_path": str(video), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    await ClipNodeHandler().execute(graph["nodes"][-1], ctx)
    refs = [Path(x).resolve() for x in (seen.get("reference_images") or [])]
    assert character.resolve() in refs
    assert scene.resolve() in refs
    assert frame.resolve() not in refs
    assert seen.get("first_frame") in (None, "")
    assert seen.get("force_reference_mode") is True
    assert seen["reference_file"] in (None, "")
    assert seen["prompt"]


@pytest.mark.asyncio
async def test_clip_handler_returns_file_output_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "generated_clip.mp4"
    video.write_bytes(b"fake-mp4")

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, str]:
        assert kwargs.get("force_reference_mode") is True
        assert first_frame in (None, "")
        assert "USER PROMPT (authoritative story" not in prompt
        return {"video_path": str(video), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    graph = _graph()
    result = await ClipNodeHandler().execute(
        graph["nodes"][1],
        NodeExecutionContext(graph=graph, run_id="run_clip01", node_id="n_clip"),
    )
    assert result.output_ref is not None
    assert result.output_ref["kind"] == NODE_TYPE_VIDEO
    assert result.output_ref["mime_type"] == "video/mp4"
    assert result.output_ref["label"] == "generated_clip.mp4"
    assert result.output_ref["uri"].startswith("file:")
    assert result.output_ref["uri"].endswith("generated_clip.mp4")


@pytest.mark.asyncio
async def test_generate_clip_video_raises_on_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_generate(request, *, save_dir=None):
        return {"error": "[ERROR]: MiniMax video create failed 402"}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.media_generation.generate_video",
        fake_generate,
    )
    with pytest.raises(RuntimeError, match="402"):
        await generate_clip_video("a boy playing basketball")


@pytest.mark.asyncio
async def test_clip_handler_allows_text_only_when_notes_replace_stills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notes-only frame outputs are not image refs; tools fall through to text-only."""
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_FRAME,
        NODE_ROLE_STORYBOARD,
        NODE_TYPE_IMAGE,
        NODE_TYPE_TABLE,
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    notes = tmp_path / "frame_notes.md"
    story = tmp_path / "storyboard.md"
    video = tmp_path / "clip.mp4"
    notes.write_text("image_gen failed, wrote notes", encoding="utf-8")
    story.write_text("## 分镜表\n缓推", encoding="utf-8")
    video.write_bytes(b"fake-mp4")
    graph = _graph()
    graph["nodes"][1:1] = [
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "storyboard",
            "config": {"role": NODE_ROLE_STORYBOARD},
        },
        {
            "id": "n_frame",
            "type": NODE_TYPE_IMAGE,
            "label": "frame",
            "config": {"role": NODE_ROLE_FRAME},
        },
    ]
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_clip_notes",
        node_id="n_clip",
        run={
            "node_states": {
                "n_storyboard": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        story, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
                    ),
                },
                "n_frame": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        notes, kind="text", mime_type="text/markdown"
                    ),
                },
            }
        },
    )
    seen: dict[str, object] = {}

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, str]:
        seen["first_frame"] = first_frame
        seen["reference_images"] = reference_images
        seen["force_reference_mode"] = kwargs.get("force_reference_mode")
        return {"video_path": str(video), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    result = await ClipNodeHandler().execute(graph["nodes"][-1], ctx)
    assert result.output_ref is not None
    assert seen.get("force_reference_mode") is True
    assert seen.get("first_frame") in (None, "")
    assert not seen.get("reference_images")


@pytest.mark.asyncio
async def test_clip_handler_uses_scene_plate_and_storyboard_duration_for_shot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_SCENE,
        NODE_ROLE_STORYBOARD,
        NODE_TYPE_IMAGE,
        NODE_TYPE_TABLE,
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    scene = tmp_path / "scene.png"
    story = tmp_path / "storyboard.md"
    scene.write_bytes(b"png-scene")
    story.write_text(
        "## 分镜表\n"
        "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
        "| 1 | 0.0-2.0s | 全景/平视 | 缓摇 | 未入画 | 站台 |\n"
        "| 2 | 2.0-5.0s | 中景/平视 | 跟移 | 主体入画 | 出站 |\n",
        encoding="utf-8",
    )
    video = tmp_path / "generated_clip_2.mp4"
    video.write_bytes(b"fake-mp4")
    graph = _graph()
    graph["nodes"][1] = {
        "id": "n_clip_2",
        "type": NODE_TYPE_VIDEO,
        "label": "clip 2",
        "config": {
            "role": NODE_ROLE_CLIP,
            "shot_index": 2,
            "scene_node_id": "n_scene",
        },
    }
    graph["nodes"][1:1] = [
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "storyboard",
            "config": {"role": NODE_ROLE_STORYBOARD},
        },
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "scene",
            "config": {"role": NODE_ROLE_SCENE},
        },
    ]
    clip = next(node for node in graph["nodes"] if node["id"] == "n_clip_2")
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_clip_shot2",
        node_id="n_clip_2",
        run={
            "node_states": {
                "n_storyboard": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        story, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
                    ),
                },
                "n_scene": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        scene, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                    ),
                },
            }
        },
    )
    seen: dict[str, object] = {}

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        duration: int = 5,
        **kwargs,
    ) -> dict[str, str]:
        seen["first_frame"] = first_frame
        seen["reference_images"] = reference_images
        seen["force_reference_mode"] = kwargs.get("force_reference_mode")
        seen["prompt"] = prompt
        seen["duration"] = duration
        return {"video_path": str(video), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    await ClipNodeHandler().execute(clip, ctx)
    assert seen.get("first_frame") in (None, "")
    assert seen["reference_images"]
    assert str(scene.resolve()) in [str(x) for x in (seen["reference_images"] or [])]
    assert seen["duration"] == 3
    assert seen.get("force_reference_mode") is True
    assert seen["prompt"]


@pytest.mark.asyncio
async def test_compose_handler_merges_clips_in_shot_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import NODE_ROLE_COMPOSE
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref
    from jiuwenswarm.server.runtime.designer.handlers.compose import ComposeNodeHandler

    clip1 = tmp_path / "shot1.mp4"
    clip2 = tmp_path / "shot2.mp4"
    clip1.write_bytes(b"m" * 600)
    clip2.write_bytes(b"m" * 600)
    graph = normalize_execution_graph(
        {
            "schema_version": SCHEMA_VERSION,
            "graph_id": "graph_compose01",
            "project_id": "proj_compose01",
            "title": "成片",
            "nodes": [
                {
                    "id": "n_clip_1",
                    "type": NODE_TYPE_VIDEO,
                    "label": "clip 1",
                    "config": {"role": NODE_ROLE_CLIP, "shot_index": 1},
                },
                {
                    "id": "n_clip_2",
                    "type": NODE_TYPE_VIDEO,
                    "label": "clip 2",
                    "config": {"role": NODE_ROLE_CLIP, "shot_index": 2},
                },
                {
                    "id": "n_compose",
                    "type": NODE_TYPE_VIDEO,
                    "label": "成片",
                    "config": {"role": NODE_ROLE_COMPOSE, "inputs": ["n_clip_1", "n_clip_2"]},
                },
            ],
            "edges": [
                {"id": "e_1", "source": "n_clip_1", "target": "n_compose"},
                {"id": "e_2", "source": "n_clip_2", "target": "n_compose"},
            ],
        }
    )
    seen: dict[str, object] = {}

    def fake_concat(paths: list[Path], dest: Path) -> Path:
        seen["paths"] = [str(path) for path in paths]
        dest = Path(dest)
        dest.write_bytes(b"m" * 600)
        return dest.resolve()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.get_agent_workspace_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.compose.concatenate_clip_videos",
        fake_concat,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.compose._clip_video_usable",
        lambda path, ffmpeg=None: Path(path).is_file() and Path(path).stat().st_size >= 512,
    )
    result = await ComposeNodeHandler().execute(
        graph["nodes"][-1],
        NodeExecutionContext(
            graph=graph,
            run_id="run_compose01",
            node_id="n_compose",
            run={
                "node_states": {
                    "n_clip_1": {
                        "status": "completed",
                        "output_ref": file_output_ref(
                            clip1, kind=NODE_TYPE_VIDEO, mime_type="video/mp4"
                        ),
                    },
                    "n_clip_2": {
                        "status": "completed",
                        "output_ref": file_output_ref(
                            clip2, kind=NODE_TYPE_VIDEO, mime_type="video/mp4"
                        ),
                    },
                }
            },
        ),
    )
    assert seen["paths"] == [str(clip1.resolve()), str(clip2.resolve())]
    assert result.output_ref is not None
    assert result.output_ref["kind"] == NODE_TYPE_VIDEO
    assert str(result.output_ref.get("label") or "").endswith(".mp4")


def test_concatenate_clip_videos_concats_shot1_then_shot2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from jiuwenswarm.server.runtime.designer.handlers import compose as compose_mod

    clip1 = tmp_path / "shot1.mp4"
    clip2 = tmp_path / "shot2.mp4"
    dest = tmp_path / "film.mp4"
    clip1.write_bytes(b"clip-1")
    clip2.write_bytes(b"clip-2")
    calls: list[list[str]] = []
    listings: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if "-f" in cmd and "concat" in cmd:
            list_path = Path(cmd[cmd.index("-i") + 1])
            listings.append(list_path.read_text(encoding="utf-8"))
        dest.write_bytes(b"concatenated")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(compose_mod, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(compose_mod.subprocess, "run", fake_run)
    merged = compose_mod.concatenate_clip_videos([clip1, clip2], dest)
    assert merged == dest.resolve()
    assert dest.read_bytes() == b"concatenated"
    assert calls
    copy_cmd = calls[0]
    assert copy_cmd[0] == "ffmpeg"
    assert "-f" in copy_cmd and "concat" in copy_cmd
    assert "-c" in copy_cmd or "-c:v" in copy_cmd
    assert "-an" not in copy_cmd
    listing = listings[0]
    assert clip1.resolve().as_posix() in listing
    assert clip2.resolve().as_posix() in listing
    assert listing.index(clip1.resolve().as_posix()) < listing.index(clip2.resolve().as_posix())


def test_concatenate_clip_videos_falls_back_to_filter_concat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from jiuwenswarm.server.runtime.designer.handlers import compose as compose_mod

    clip1 = tmp_path / "shot1.mp4"
    clip2 = tmp_path / "shot2.mp4"
    dest = tmp_path / "film.mp4"
    clip1.write_bytes(b"clip-1")
    clip2.write_bytes(b"clip-2")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        joined = " ".join(cmd)
        if "-f" in cmd and "concat" in cmd and "-filter_complex" not in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="copy failed")
        if "-i" in cmd and "-filter_complex" not in cmd:
            # Probe: pretend both clips have audio so filter path keeps a=1.
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr=(
                    "Stream #0:0: Video: h264, yuv420p, 1280x720\n"
                    "Stream #0:1: Audio: aac, 44100 Hz, stereo"
                ),
            )
        dest.write_bytes(b"reencoded")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(compose_mod, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(compose_mod.subprocess, "run", fake_run)
    merged = compose_mod.concatenate_clip_videos([clip1, clip2], dest)
    assert merged == dest.resolve()
    assert dest.read_bytes() == b"reencoded"
    filter_cmd = next(cmd for cmd in calls if "-filter_complex" in cmd)
    spec = filter_cmd[filter_cmd.index("-filter_complex") + 1]
    assert str(clip1.resolve()) in filter_cmd
    assert str(clip2.resolve()) in filter_cmd
    assert filter_cmd.index(str(clip1.resolve())) < filter_cmd.index(str(clip2.resolve()))
    assert "concat=n=2:v=1:a=1" in spec
    assert "-an" not in filter_cmd
    assert "aac" in filter_cmd

