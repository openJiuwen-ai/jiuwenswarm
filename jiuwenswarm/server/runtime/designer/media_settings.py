# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Canvas-wide video and image generation settings.

``graph.metadata.video_settings`` and ``graph.metadata.image_settings`` are the
only stored copies. Every clip and still call reads them here at call time, so
one change applies to every node on the canvas. Clip length is per node: a
video node's own ``duration_sec``, else its storyboard timeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from jiuwenswarm.server.runtime.designer import media_generation
from jiuwenswarm.server.runtime.designer.media_generation import configured_model
from jiuwenswarm.server.runtime.designer.pipeline.image_capacity import (
    DEFAULT_IMAGE_RATIO,
    IMAGE_RATIOS,
    IMAGE_SIZES,
    ImageModelCapacity,
    active_image_capacity,
    snap_image_resolution,
)
from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import (
    VideoModelCapacity,
    active_video_capacity,
    default_ratio,
    ratio_value,
    snap_ratio,
    snap_resolution,
)

VIDEO_SETTINGS_KEY = "video_settings"
IMAGE_SETTINGS_KEY = "image_settings"


class MediaSettingsError(ValueError):
    """A settings patch names a field or value the active models do not accept."""


@dataclass(frozen=True)
class VideoSettings:
    aspect_ratio: str
    resolution: str

    def as_dict(self) -> dict[str, str]:
        return {"aspect_ratio": self.aspect_ratio, "resolution": self.resolution}


@dataclass(frozen=True)
class ImageSettings:
    aspect_ratio: str
    resolution: str

    @property
    def size(self) -> str:
        return IMAGE_SIZES[self.resolution][self.aspect_ratio]

    def as_dict(self) -> dict[str, str]:
        return {"aspect_ratio": self.aspect_ratio, "resolution": self.resolution}


def _stored(graph: Mapping[str, Any] | None, key: str) -> dict[str, Any]:
    meta = graph.get("metadata") if isinstance(graph, Mapping) else None
    raw = meta.get(key) if isinstance(meta, Mapping) else None
    return dict(raw) if isinstance(raw, Mapping) else {}


def default_video_settings(capacity: VideoModelCapacity | None = None) -> VideoSettings:
    cap = capacity or active_video_capacity()
    return VideoSettings(aspect_ratio=default_ratio(cap), resolution=cap.default_resolution)


def video_settings(
    graph: Mapping[str, Any] | None, capacity: VideoModelCapacity | None = None
) -> VideoSettings:
    """Stored canvas video settings, snapped onto the active video model."""
    cap = capacity or active_video_capacity()
    raw = _stored(graph, VIDEO_SETTINGS_KEY)
    return VideoSettings(
        aspect_ratio=snap_ratio(str(raw.get("aspect_ratio") or ""), cap),
        resolution=snap_resolution(str(raw.get("resolution") or ""), cap),
    )


def snap_image_ratio(requested: str) -> str:
    raw = str(requested or "").strip()
    if raw in IMAGE_RATIOS:
        return raw
    target = ratio_value(raw)
    if target is None:
        return DEFAULT_IMAGE_RATIO
    return min(IMAGE_RATIOS, key=lambda item: abs((ratio_value(item) or 0.0) - target))


def image_settings(
    graph: Mapping[str, Any] | None, capacity: ImageModelCapacity | None = None
) -> ImageSettings:
    """Stored canvas image settings, snapped onto the active image model."""
    cap = capacity or active_image_capacity()
    raw = _stored(graph, IMAGE_SETTINGS_KEY)
    return ImageSettings(
        aspect_ratio=snap_image_ratio(str(raw.get("aspect_ratio") or "")),
        resolution=snap_image_resolution(str(raw.get("resolution") or ""), cap),
    )


def _pick(value: Any, allowed: tuple[str, ...], field: str) -> str:
    token = str(value or "").strip()
    for item in allowed:
        if item.lower() == token.lower():
            return item
    raise MediaSettingsError(f"{field} must be one of {', '.join(allowed)}; got {token!r}")


def _check_fields(patch: Mapping[str, Any], allowed: set[str], kind: str) -> None:
    unknown = sorted(set(patch) - allowed)
    if unknown:
        raise MediaSettingsError(f"unknown {kind} setting(s): {', '.join(unknown)}")


def write_media_settings(
    graph: dict[str, Any],
    *,
    video: VideoSettings | None = None,
    image: ImageSettings | None = None,
) -> None:
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    meta = dict(meta)
    if video is not None:
        meta[VIDEO_SETTINGS_KEY] = video.as_dict()
    if image is not None:
        meta[IMAGE_SETTINGS_KEY] = image.as_dict()
    graph["metadata"] = meta


def update_media_settings(
    graph: dict[str, Any],
    *,
    video: Mapping[str, Any] | None = None,
    image: Mapping[str, Any] | None = None,
    capacity: VideoModelCapacity | None = None,
    image_capacity: ImageModelCapacity | None = None,
) -> None:
    """Apply a partial settings patch. Values the active models reject raise."""
    if video is not None and not isinstance(video, Mapping):
        raise MediaSettingsError("video_settings must be an object")
    if image is not None and not isinstance(image, Mapping):
        raise MediaSettingsError("image_settings must be an object")
    cap = capacity or active_video_capacity()
    next_video = None
    if video:
        _check_fields(video, {"aspect_ratio", "resolution"}, "video")
        current = video_settings(graph, cap)
        next_video = VideoSettings(
            aspect_ratio=(
                _pick(video["aspect_ratio"], cap.ratios, "video aspect_ratio")
                if "aspect_ratio" in video
                else current.aspect_ratio
            ),
            resolution=(
                _pick(video["resolution"], cap.resolutions, "video resolution")
                if "resolution" in video
                else current.resolution
            ),
        )
    next_image = None
    if image:
        _check_fields(image, {"aspect_ratio", "resolution"}, "image")
        image_cap = image_capacity or active_image_capacity()
        current_image = image_settings(graph, image_cap)
        next_image = ImageSettings(
            aspect_ratio=(
                _pick(image["aspect_ratio"], IMAGE_RATIOS, "image aspect_ratio")
                if "aspect_ratio" in image
                else current_image.aspect_ratio
            ),
            resolution=(
                _pick(image["resolution"], image_cap.resolutions, "image resolution")
                if "resolution" in image
                else current_image.resolution
            ),
        )
    write_media_settings(graph, video=next_video, image=next_image)


def video_durations(graph: dict[str, Any], run: dict[str, Any] | None = None) -> dict[str, int]:
    """Seconds each generated video node will be rendered at, read like its handler does."""
    from jiuwenswarm.server.runtime.designer.handlers.clip import (
        generates_timed_video,
        video_duration_seconds,
    )
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    out: dict[str, int] = {}
    for node in graph.get("nodes") or []:
        if not generates_timed_video(graph, node):
            continue
        ctx = NodeExecutionContext(graph=graph, run_id="", node_id=str(node["id"]), run=run)
        out[str(node["id"])] = video_duration_seconds(ctx, node)
    return out


def media_capabilities(
    graph: dict[str, Any] | None = None, run: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Values the toolbar may offer, plus this canvas's effective settings and clip lengths."""
    cap = active_video_capacity()
    image_cap = active_image_capacity()
    out: dict[str, Any] = {
        "video": {
            "configured_model": configured_model("video"),
            "backend": media_generation.slot_settings("video").backend,
            "model_id": cap.model_id,
            "provider": cap.provider,
            "ratios": list(cap.ratios),
            "resolutions": list(cap.resolutions),
            "default_ratio": default_ratio(cap),
            "default_resolution": cap.default_resolution,
            "min_sec": cap.min_sec,
            "max_sec": cap.max_sec,
            "default_sec": cap.default_sec,
        },
        "image": {
            "configured_model": configured_model("image"),
            "backend": media_generation.slot_settings("image").backend,
            "ratios": list(IMAGE_RATIOS),
            "resolutions": list(image_cap.resolutions),
            "default_ratio": DEFAULT_IMAGE_RATIO,
            "default_resolution": image_cap.default_resolution,
        },
    }
    if graph is not None:
        out["settings"] = {
            "video": video_settings(graph, cap).as_dict(),
            "image": image_settings(graph, image_cap).as_dict(),
        }
        out["video_durations"] = video_durations(graph, run)
    return out
