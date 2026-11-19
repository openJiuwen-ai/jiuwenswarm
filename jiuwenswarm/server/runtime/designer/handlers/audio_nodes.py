# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Music/BGM handler: one film-wide bed, silent placeholder when no backend answers."""

from __future__ import annotations

import copy
import logging
import shutil
from functools import partial
from pathlib import Path

from jiuwenswarm.common.schema.designer_graph import (
    ACTIVITY_KIND_STAGE,
    NODE_ROLE_CLIP,
    NODE_TYPE_AUDIO,
    DesignerGraphNode,
    node_clip_index,
    node_pipeline,
)
from jiuwenswarm.server.runtime.designer.ffmpeg_runner import FfmpegCancelled, run_ffmpeg, run_off_loop
from jiuwenswarm.server.runtime.designer.handlers.common import (
    file_output_ref,
    graph_workspace_dir,
)
from jiuwenswarm.server.runtime.designer.handlers.types import (
    NodeExecutionContext,
    NodeResult,
    thread_snapshot,
)
from jiuwenswarm.server.runtime.designer.music_cues import (
    CueSheet,
    bpm_range,
    build_cue_sheet,
    cue_prompt,
    storyboard_emotions,
)

logger = logging.getLogger(__name__)
SILENT_MUSIC_PLACEHOLDER_TOKEN = "placeholder_silent"
USER_MUSIC_TOKEN = "user_ref"
_MIN_BED_SECONDS = 4.0
_MAX_BED_SECONDS = 360.0
_DEFAULT_BED_SECONDS = 18.0
# Music3 stops by 1.5x its budget; Lyria writes a whole song whatever length is asked.
_OVERLONG_RATIO = 1.5
_SHEET_TOLERANCE_SECONDS = 0.5
_AUDIO_MIME = {
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}


def is_silent_music_placeholder(path: Path | str | None) -> bool:
    """True for the no-API music stub (must not be mixed onto the film)."""
    name = Path(path or "").name.lower()
    return SILENT_MUSIC_PLACEHOLDER_TOKEN in name


def is_generated_music(path: Path | str | None) -> bool:
    """True for model-generated BGM; the user's own track and the silent stub are kept as they are."""
    name = Path(path or "").name.lower()
    return USER_MUSIC_TOKEN not in name and SILENT_MUSIC_PLACEHOLDER_TOKEN not in name


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = str(imageio_ffmpeg.get_ffmpeg_exe() or "").strip()
        return exe or None
    except Exception:  # noqa: BLE001
        return None


def film_bgm_lock(graph: dict, cfg: dict | None = None) -> dict:
    from jiuwenswarm.server.runtime.designer.audio_locks import resolve_audio_intent_flags

    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    bgm = resolve_audio_intent_flags(meta, cfg or {})["bgm_lock"] or meta.get("bgm_lock") or {}
    return bgm if isinstance(bgm, dict) else {}


def film_cue_sheet(node: DesignerGraphNode, ctx: NodeExecutionContext) -> CueSheet | None:
    """Planned sections and tempo from the storyboard clips, in film order."""
    from jiuwenswarm.server.runtime.designer.handlers.clip import video_duration_seconds

    clips = sorted(
        (n for n in (ctx.graph.get("nodes") or []) if isinstance(n, dict) and node_pipeline(n) == NODE_ROLE_CLIP),
        key=node_clip_index,
    )
    emotions = storyboard_emotions(ctx.graph)
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    return build_cue_sheet(
        [float(video_duration_seconds(ctx, clip)) for clip in clips],
        [emotions.get(node_clip_index(clip), "") for clip in clips],
        tempo=bpm_range(film_bgm_lock(ctx.graph, cfg)),
    )


def film_duration_sec(node: DesignerGraphNode, ctx: NodeExecutionContext) -> float:
    """Length of the one BGM bed: the node's own setting, else the planned clips' total."""
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    try:
        film = float(cfg.get("film_duration_sec") or cfg.get("duration_sec") or cfg.get("max_audio_sec") or 0)
    except (TypeError, ValueError):
        film = 0.0
    if film <= 0:
        sheet = film_cue_sheet(node, ctx)
        film = sheet.total if sheet is not None else 0.0
    if film > 0:
        return max(_MIN_BED_SECONDS, min(_MAX_BED_SECONDS, film))
    return _DEFAULT_BED_SECONDS


def bgm_caption(node: DesignerGraphNode, ctx: NodeExecutionContext, instructions: str = "") -> str:
    """Music prompt: the node's prompt, the user's own words, the agent's spec, ``bgm_lock`` and the cue sheet.

    ``config.prompt`` is the latest request for this node (canvas panel or chat patch), so it leads.
    """
    meta = ctx.graph.get("metadata") if isinstance(ctx.graph.get("metadata"), dict) else {}
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    bgm = film_bgm_lock(ctx.graph, cfg)
    parts: list[str] = []
    for part in (cfg.get("prompt"), bgm.get("user_request"), instructions):
        text = str(part or "").strip()
        if text and text not in parts:
            parts.append(text)
    # bgm_lock.rule is mixing guidance ("never drown dialogue"); compose ducks the
    # bed, and Lyria rejects such vocal/dialogue negations as PROHIBITED_CONTENT.
    parts.extend(
        f"{key}: {bgm[key]}"
        for key in ("mood", "style", "instruments", "key", "continuity")
        if str(bgm.get(key) or "").strip()
    )
    if not any(parts):
        story = str(ctx.graph.get("description") or meta.get("user_prompt") or "").strip()
        parts.append(f"Cinematic underscore for this short film: {story[:400]}" if story else "Cinematic underscore")
    parts.append("One continuous piece for the whole film, one identity, no genre jumps.")
    sheet = film_cue_sheet(node, ctx)
    if sheet is not None:
        parts.append(cue_prompt(sheet))
    return "\n".join(part for part in parts if part)


def _synthesize_bed(dest: Path, *, duration: float, kind: str) -> bool:
    """Create a silent local music placeholder."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    del kind
    # Empty BGM until a music API is wired — no noise, no sine score.
    lavfi = f"anullsrc=r=44100:cl=stereo,atrim=0:{max(0.2, duration)}"
    args = [
        "-y",
        "-f",
        "lavfi",
        "-i",
        lavfi,
        "-t",
        f"{max(0.2, duration):.2f}",
        "-ac",
        "2",
        "-ar",
        "44100",
        str(dest.resolve()),
    ]
    try:
        proc = run_ffmpeg([ffmpeg, *args], timeout=20)
        return proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0
    except Exception:  # noqa: BLE001
        logger.info("ffmpeg audio bed failed", exc_info=True)
        return False


def _write_empty_music_placeholder(dest: Path) -> Path:
    """Zero-byte stub when ffmpeg is missing. Compose must not mux this."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"")
    return dest


def _music_stem(ctx: NodeExecutionContext) -> str:
    return f"designer_music_{ctx.run_id}_{ctx.node_id}"


def _audio_result(path: Path, message: str) -> NodeResult:
    mime = _AUDIO_MIME.get(path.suffix.lower(), "audio/mpeg")
    return NodeResult(
        output_ref=file_output_ref(path, kind=NODE_TYPE_AUDIO, mime_type=mime),
        message=message,
    )


async def _silent_placeholder(ctx: NodeExecutionContext, *, duration: float, warning: str = "") -> NodeResult:
    reason = f"WARNING: {warning}" if warning else "no music API"
    dest = graph_workspace_dir(ctx.graph) / f"{_music_stem(ctx)}_{SILENT_MUSIC_PLACEHOLDER_TOKEN}.m4a"
    if await run_off_loop(partial(_synthesize_bed, dest, duration=duration, kind="music")):
        return _audio_result(dest, f"music placeholder silent {duration:.0f}s ({reason})")
    _write_empty_music_placeholder(dest)
    return _audio_result(dest, f"music placeholder empty file ({reason}; no ffmpeg)")


async def render_bgm(
    node: DesignerGraphNode,
    ctx: NodeExecutionContext,
    *,
    instructions: str = "",
) -> NodeResult:
    """Generate the film's BGM bed; a failed backend leaves a silent placeholder and a warning."""
    duration = film_duration_sec(node, ctx)
    try:
        generated, warning = await _try_music_api(node, ctx, duration_sec=duration, instructions=instructions)
    except FfmpegCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Designer BGM backend raised for %s", ctx.node_id, exc_info=True)
        generated, warning = None, f"music backend failed: {type(exc).__name__}: {exc}"
    if generated is not None:
        return generated
    if warning:
        logger.warning("Designer BGM for %s fell back to silence: %s", ctx.node_id, warning)
        if callable(ctx.emit_activity):
            ctx.emit_activity(ACTIVITY_KIND_STAGE, f"BGM generation failed, using silence: {warning}", "music_gen")
    return await _silent_placeholder(ctx, duration=duration, warning=warning)


class MusicNodeHandler:
    """BGM module: user ref → music API → silent placeholder."""

    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        from jiuwenswarm.server.runtime.designer.user_references import (
            user_reference_audio_path,
        )

        user_audio = user_reference_audio_path(ctx.graph)
        if user_audio is not None and user_audio.is_file():
            suffix = user_audio.suffix.lower() or ".mp3"
            dest = graph_workspace_dir(ctx.graph) / f"{_music_stem(ctx)}_{USER_MUSIC_TOKEN}{suffix}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(user_audio, dest)
            return _audio_result(dest, "music from user audio reference")
        return await render_bgm(node, ctx)


async def _try_music_api(
    node: DesignerGraphNode,
    ctx: NodeExecutionContext,
    *,
    duration_sec: float,
    instructions: str = "",
) -> tuple[NodeResult | None, str]:
    """(generated bed, "") or (None, warning); no warning when no backend is configured."""
    from jiuwenswarm.server.runtime.designer.music_generation import (
        MusicRequest,
        generate_music,
        music_problem,
    )

    if music_problem():
        return None, ""
    caption = bgm_caption(node, ctx, instructions)
    result = await generate_music(
        MusicRequest(caption=caption, duration_sec=duration_sec),
        dest_stem=graph_workspace_dir(ctx.graph) / _music_stem(ctx),
    )
    path = Path(result.get("music_path") or "")
    if result.get("error") or not path.is_file():
        return None, str(result.get("error") or "music backend returned no file").removeprefix("[ERROR]: ")
    detail = result.get("detail") or "generated"
    fitted, length = await run_off_loop(
        _fit_to_film, path, copy.deepcopy(node), thread_snapshot(ctx), duration_sec
    )
    return _audio_result(fitted, f"music {detail} {length}"), ""


def _fit_to_film(
    path: Path, node: DesignerGraphNode, ctx: NodeExecutionContext, duration_sec: float
) -> tuple[Path, str]:
    """(track to publish, its length for the result text); an overlong bed is cut to the film, raw kept aside."""
    from jiuwenswarm.server.runtime.designer.music_align import align_music, track_seconds

    ffmpeg = _find_ffmpeg()
    actual = track_seconds(ffmpeg, path) if ffmpeg else None
    if ffmpeg is None or actual is None:
        return path, f"{duration_sec:.0f}s requested"
    if actual <= duration_sec * _OVERLONG_RATIO:
        return path, f"{actual:.0f}s"
    sheet = film_cue_sheet(node, ctx)
    if sheet is None or abs(sheet.total - duration_sec) > _SHEET_TOLERANCE_SECONDS:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        sheet = build_cue_sheet([duration_sec], [""], tempo=bpm_range(film_bgm_lock(ctx.graph, cfg)))
    if sheet is None:
        return path, f"{actual:.0f}s"
    raw = path.with_name(f"{path.stem}_raw{path.suffix}")
    path.replace(raw)
    aligned, notes = align_music(ffmpeg, raw, sheet, path.with_suffix(""))
    if aligned is None:
        logger.warning("Designer BGM trim failed for %s; publishing the raw %.0fs track", ctx.node_id, actual)
        return raw, f"{actual:.0f}s"
    logger.info("Designer BGM %s trimmed %.0fs -> %.1fs: %s", ctx.node_id, actual, sheet.total, "; ".join(notes))
    return aligned, f"{sheet.total:.0f}s (trimmed from {actual:.0f}s)"
