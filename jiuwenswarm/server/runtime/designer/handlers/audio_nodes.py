# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Music/BGM handler with a silent no-API placeholder."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from jiuwenswarm.common.schema.designer_graph import NODE_TYPE_AUDIO, DesignerGraphNode
from jiuwenswarm.server.runtime.designer.handlers.common import (
    file_output_ref,
    graph_workspace_dir,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult

logger = logging.getLogger(__name__)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
SILENT_MUSIC_PLACEHOLDER_TOKEN = "placeholder_silent"


def is_silent_music_placeholder(path: Path | str | None) -> bool:
    """True for the no-API music stub (must not be mixed onto the film)."""
    name = Path(path or "").name.lower()
    return SILENT_MUSIC_PLACEHOLDER_TOKEN in name


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


def _bed_duration_sec(cfg: dict) -> float:
    try:
        film = float(cfg.get("film_duration_sec") or cfg.get("duration_sec") or cfg.get("max_audio_sec") or 0)
    except (TypeError, ValueError):
        film = 0.0
    if film > 0:
        return max(4.0, min(90.0, film))
    return 18.0


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
        kwargs: dict[str, object] = {
            "capture_output": True,
            "text": True,
            "timeout": 20,
        }
        # CREATE_NO_WINDOW is Windows-only; passing it on Linux/macOS/WSL raises.
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        proc = subprocess.run([ffmpeg, *args], **kwargs)
        return proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0
    except Exception:  # noqa: BLE001
        logger.info("ffmpeg audio bed failed", exc_info=True)
        return False


def _write_empty_music_placeholder(dest: Path) -> Path:
    """Zero-byte stub when ffmpeg is missing. Compose must not mux this."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"")
    return dest


class MusicNodeHandler:
    """BGM module: user ref → future music API → silent placeholder.

    ``call_music_model`` stays on the node tools for when a music backend exists.
    Until then this handler only occupies the slot with an empty/silent file so
    compose will not color the film with a fake score.
    """

    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        duration = _bed_duration_sec(cfg)
        stem = f"designer_music_{ctx.run_id}_{ctx.node_id}"
        from jiuwenswarm.server.runtime.designer.user_references import (
            user_reference_audio_path,
        )

        user_audio = user_reference_audio_path(ctx.graph)
        if user_audio is not None and user_audio.is_file():
            dest = graph_workspace_dir(ctx.graph) / f"{stem}{user_audio.suffix.lower() or '.mp3'}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(user_audio, dest)
            suffix = dest.suffix.lower()
            mime = {
                ".wav": "audio/wav",
                ".m4a": "audio/mp4",
                ".aac": "audio/aac",
                ".ogg": "audio/ogg",
                ".flac": "audio/flac",
            }.get(suffix, "audio/mpeg")
            return NodeResult(
                output_ref=file_output_ref(dest, kind=NODE_TYPE_AUDIO, mime_type=mime),
                message="music from user audio reference",
            )

        # Hook for a future music API: if the tool is later implemented on this
        # handler path, call it here before falling back to silence.
        generated = await _try_music_api(node, ctx, duration_sec=duration)
        if generated is not None:
            return generated

        dest = (
            graph_workspace_dir(ctx.graph)
            / f"{stem}_{SILENT_MUSIC_PLACEHOLDER_TOKEN}.m4a"
        )
        if _synthesize_bed(dest, duration=duration, kind="music"):
            return NodeResult(
                output_ref=file_output_ref(dest, kind=NODE_TYPE_AUDIO, mime_type="audio/mp4"),
                message=f"music placeholder silent {duration:.0f}s (no music API)",
            )
        _write_empty_music_placeholder(dest)
        return NodeResult(
            output_ref=file_output_ref(dest, kind=NODE_TYPE_AUDIO, mime_type="audio/mp4"),
            message="music placeholder empty file (no music API / no ffmpeg)",
        )


async def _try_music_api(
    node: DesignerGraphNode,
    ctx: NodeExecutionContext,
    *,
    duration_sec: float,
) -> NodeResult | None:
    """Reserved: generate real BGM when a music backend is configured.

    Returns None so the silent placeholder is used. Wire ``call_music_model``
    (or an equivalent HTTP client) here when MUSIC_API_KEY / models.music exists.
    """
    _ = (node, ctx, duration_sec)
    try:
        from jiuwenswarm.server.runtime.designer.capabilities import detect_audio_backends

        if not detect_audio_backends().get("can_music"):
            return None
    except Exception:  # noqa: BLE001
        return None
    # Backend advertised but no generator is plugged into this handler yet.
    return None
