# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""vLLM-Omni (self-deployed) image/video generation backend.

Every generation is a two-request flow: first ``GET {api_base}/models`` to
discover the actually served model id, then the generation request itself.
The served id matters twice:

- it is sent as the request ``model`` field (the server rejects a mismatching
  model name with a 400), and
- it drives the model-spec registry below, which decides model-specific
  request shaping — hardcoded sampler fields and the ``extra_params`` JSON.

Only MiniMax-H3 is registered for now. When the served model is not in the
registry, no ``extra_params`` is built and a plain generic request is sent.

Callers that carry explicit request knobs (ComfyUI-imported nodes) pass them
as ``**extra_fields``: top-level request fields such as sampling params, which
override the spec-built ones, the same way the ComfyUI-vLLM-Omni plugin adds
sampling params to the payload as-is.

References: vLLM-Omni ``docs/serving/videos_api.md``,
``docs/serving/image_generation_api.md``, ``docs/serving/image_edit_api.md``
and ``recipes/MiniMaxAI/MiniMax-H3.md``.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse

import requests

from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# A 50-step H3 generation takes ~9 min on 2xRTX5090.
_POLL_INTERVAL_SECONDS = 5.0
_POLL_TIMEOUT_SECONDS = 1800.0
_MODELS_TIMEOUT_SECONDS = 15.0
_CREATE_TIMEOUT_SECONDS = 120.0
_DOWNLOAD_TIMEOUT_SECONDS = 300.0

# Provider-level default: every vLLM-Omni video request pins fps=24. This is
# deliberately not user-facing; H3 output is fixed at 24 FPS anyway.
_VIDEO_FPS = 24

# H3 reference images accept up to 30 MiB each (recipe limit).
_MAX_REFERENCE_BYTES = 30 * 1024 * 1024

# Ref2VA reference limits (ComfyUI-vLLM-Omni ``utils/types.py``).
_MAX_REFERENCE_IMAGES = 9
_MAX_REFERENCE_VIDEOS = 3
_MAX_REFERENCE_AUDIOS = 3
_MAX_TOTAL_REFERENCES = 12

# Fields the request builders own; ``extra_fields`` may not replace them.
_RESERVED_REQUEST_FIELDS = frozenset(
    {
        "prompt",
        "model",
        "extra_params",
        "input_reference",
        "input_references",
        "image_reference",
        "video_reference",
        "audio_reference",
        "image",
        "url",
    }
)

ReferenceUpload = tuple[str, bytes, str]
Reference = ReferenceUpload | str


def _form_value(value: Any) -> str:
    """Multipart text for one request field."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _checked_extra_fields(extra_fields: Mapping[str, Any]) -> dict[str, Any]:
    reserved = sorted(set(extra_fields) & _RESERVED_REQUEST_FIELDS)
    if reserved:
        raise ValueError(f"vLLM-Omni extra fields may not override: {', '.join(reserved)}")
    return {key: value for key, value in extra_fields.items() if value is not None}


# ---------------------------------------------------------------------------
# HTTP helpers (self-contained so video_tools/image_tools stay import-cycle free)
# ---------------------------------------------------------------------------


def _http_request(method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("verify", get_requests_verify())
    try:
        return requests.request(method, url, **kwargs)
    except requests.exceptions.ProxyError:
        with requests.Session() as session:
            session.trust_env = False
            return session.request(method, url, **kwargs)


def _auth_headers(api_key: str) -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _error_message(response: requests.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("msg")
            if msg:
                return str(msg)
        for key in ("detail", "message", "msg"):
            if data.get(key):
                return str(data[key])
    return response.text[:300]


def fetch_served_model_id(api_base: str, api_key: str = "") -> str | None:
    """First model id from ``GET {api_base}/models``; None when undiscoverable.

    A vLLM-Omni server instance serves a single model, so the first entry is
    the served one. Failures are non-fatal: callers fall back to a configured
    model name or omit the field (the server then uses its own default).
    """
    base = (api_base or "").strip().rstrip("/")
    if not base:
        return None
    try:
        response = _http_request(
            "GET",
            f"{base}/models",
            headers=_auth_headers(api_key),
            timeout=_MODELS_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("[vLLM-Omni] GET %s/models failed", base, exc_info=True)
        return None
    if not response.ok:
        logger.warning(
            "[vLLM-Omni] GET %s/models returned %s: %s",
            base,
            response.status_code,
            response.text[:200],
        )
        return None
    try:
        data = response.json().get("data")
    except Exception:
        logger.warning("[vLLM-Omni] GET %s/models returned non-JSON payload", base)
        return None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    model_id = str(first.get("id") if isinstance(first, dict) else "").strip()
    return model_id or None


# ---------------------------------------------------------------------------
# Model-spec registry: per-model request shaping (extra_params & hardcoded fields)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VllmOmniVideoInputs:
    """User-perceivable inputs a video spec may weave into the request."""

    size: str | None
    duration: int | float | None
    resolution: str | None
    has_references: bool
    fps: int | None = None


@dataclass(frozen=True)
class VllmOmniVideoForm:
    """Shaped multipart form: top-level fields + the extra_params JSON object."""

    fields: dict[str, str]
    extra_params: dict[str, Any] | None


@dataclass(frozen=True)
class VllmOmniVideoSpec:
    """One registered model family: matcher + request builder."""

    key: str
    matches: Callable[[str], bool]
    build: Callable[[VllmOmniVideoInputs], VllmOmniVideoForm]


def _parse_size(size: str | None) -> tuple[int, int] | None:
    """Accept both ``1280*720`` (DashScope style) and ``1280x720``."""
    value = (size or "").strip().lower().replace("*", "x")
    if "x" not in value:
        return None
    try:
        width_s, height_s = value.split("x", 1)
        width, height = int(width_s), int(height_s)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


# -- MiniMax-H3 (recipes/MiniMaxAI/MiniMax-H3.md) ----------------------------

# Recipe-pinned sampler values: not user-friendly, so they are hardcoded.
_MINIMAX_H3_NUM_INFERENCE_STEPS = 50  # matches the reference accuracy workloads
_MINIMAX_H3_FLOW_SHIFT = 12.0  # video sigma shift
_MINIMAX_H3_AUDIO_FLOW_SHIFT = 3.0  # audio sigma shift; H3 output always has audio
_MINIMAX_H3_MIN_DURATION_SECONDS = 4.0  # H3 output floor; no upper duration cap
_MINIMAX_H3_SHORT_EDGE = 768  # H3 shape policy requires exactly 768 when used
_MINIMAX_H3_NAMED_RATIOS: tuple[tuple[int, int], ...] = (
    (21, 9),
    (16, 9),
    (4, 3),
    (1, 1),
    (3, 4),
    (9, 16),
)


def _matches_minimax_h3(served_model_id: str) -> bool:
    # Covers "MiniMaxAI/MiniMax-H3", local paths like "/models/MiniMax-H3" and
    # task-partition paths like "/models/MiniMax-H3/FL2VA".
    normalized = (served_model_id or "").strip().lower().replace("\\", "/")
    return "minimax-h3" in normalized


def _snap_to_canvas_multiple(value: int, multiple: int = 32) -> int:
    """H3 requires a 32-pixel canvas multiple."""
    return max(multiple, int(round(value / multiple)) * multiple)


def _nearest_named_ratio(width: int, height: int, *, default: str = "16:9") -> str:
    target = width / height
    best = default
    best_err = float("inf")
    for rw, rh in _MINIMAX_H3_NAMED_RATIOS:
        err = abs(target - rw / rh)
        if err < best_err:
            best = f"{rw}:{rh}"
            best_err = err
    return best


def _build_minimax_h3_video_form(inputs: VllmOmniVideoInputs) -> VllmOmniVideoForm:
    # Dynamic task: any visual reference rides ref2va, plain text rides t2va.
    task = "ref2va" if inputs.has_references else "t2va"
    fields: dict[str, str] = {
        "num_inference_steps": str(_MINIMAX_H3_NUM_INFERENCE_STEPS),
        "flow_shift": str(_MINIMAX_H3_FLOW_SHIFT),
    }
    parsed = _parse_size(inputs.size)
    if parsed is not None:
        fields["width"] = str(_snap_to_canvas_multiple(parsed[0]))
        fields["height"] = str(_snap_to_canvas_multiple(parsed[1]))
    if task == "t2va":
        # T2VA requires one named output ratio.
        fields["aspect_ratio"] = _nearest_named_ratio(*parsed) if parsed else "16:9"
    elif parsed is None:
        # Ref2VA without explicit dimensions: adaptive ratio on the 768 canvas.
        fields["aspect_ratio"] = "adaptive"
        fields["short_edge"] = str(_MINIMAX_H3_SHORT_EDGE)
    # H3 has no resolution knob beyond the fixed 768px canvas; ``resolution``
    # is intentionally not forwarded.
    duration = (
        _MINIMAX_H3_MIN_DURATION_SECONDS
        if inputs.duration is None
        else float(inputs.duration)
    )
    duration = max(_MINIMAX_H3_MIN_DURATION_SECONDS, duration)
    extra_params = {
        "task": task,
        "duration": duration,
        "audio_flow_shift": _MINIMAX_H3_AUDIO_FLOW_SHIFT,
    }
    return VllmOmniVideoForm(fields=fields, extra_params=extra_params)


_VIDEO_SPECS: tuple[VllmOmniVideoSpec, ...] = (
    VllmOmniVideoSpec(
        key="minimax-h3",
        matches=_matches_minimax_h3,
        build=_build_minimax_h3_video_form,
    ),
)


def match_video_spec(served_model_id: str | None) -> VllmOmniVideoSpec | None:
    """Registry lookup keyed by the served model id from ``GET /models``."""
    if not served_model_id:
        return None
    for spec in _VIDEO_SPECS:
        if spec.matches(served_model_id):
            return spec
    return None


def _build_generic_video_form(inputs: VllmOmniVideoInputs) -> VllmOmniVideoForm:
    """Unregistered model: only OpenAI-style fields, never extra_params."""
    fields: dict[str, str] = {}
    parsed = _parse_size(inputs.size)
    if inputs.fps:
        # An explicit fps means an explicit frame lattice (ComfyUI Generate Video).
        if parsed is not None:
            fields["width"] = str(parsed[0])
            fields["height"] = str(parsed[1])
        if inputs.duration:
            fields["num_frames"] = str(max(1, round(float(inputs.duration) * inputs.fps)))
        return VllmOmniVideoForm(fields=fields, extra_params=None)
    if parsed is not None:
        fields["size"] = f"{parsed[0]}x{parsed[1]}"
    if inputs.duration:
        fields["seconds"] = str(max(1, int(inputs.duration)))
    return VllmOmniVideoForm(fields=fields, extra_params=None)


# ---------------------------------------------------------------------------
# Reference media loading
# ---------------------------------------------------------------------------


def _guess_mime(name: str, default_mime: str = "image/png") -> str:
    """Guessed MIME of ``name`` when it shares ``default_mime``'s major type."""
    mime, _ = mimetypes.guess_type(name)
    major = default_mime.split("/", 1)[0]
    if mime and mime.startswith(f"{major}/"):
        return mime
    return default_mime


def _local_reference_path(value: str) -> Path | None:
    text = value.strip()
    if text.startswith("file:"):
        local = unquote(urlparse(text).path)
        if len(local) >= 3 and local[0] == "/" and local[2] == ":":
            local = local[1:]
        candidate = Path(local)
    else:
        candidate = Path(text).expanduser()
    if not candidate.is_file():
        return None
    return candidate.resolve()


def _read_reference(
    reference: str,
    default_mime: str = "image/png",
) -> Reference | None:
    """Upload ``(filename, bytes, mime)``, an http(s) URL, or None."""
    value = (reference or "").strip()
    if not value:
        return None
    if value.startswith("data:"):
        try:
            header, payload = value.split(",", 1)
            mime = header[len("data:"):].split(";")[0] or default_mime
            raw = base64.b64decode(payload)
        except Exception:
            logger.warning("[vLLM-Omni] undecodable data: reference skipped")
            return None
        ext = mimetypes.guess_extension(mime) or ""
        return (f"reference{ext}", raw, mime)
    if value.startswith(("http://", "https://")):
        return value
    local = _local_reference_path(value)
    if local is None:
        logger.warning("[vLLM-Omni] reference is not a readable local file: %s", value[:120])
        return None
    try:
        raw = local.read_bytes()
    except OSError:
        logger.warning("[vLLM-Omni] reference read failed: %s", local)
        return None
    return (local.name, raw, _guess_mime(str(local), default_mime))


def _json_reference_field(urls: list[str], url_key: str) -> str:
    """One ``{url_key: url}`` object, or an ordered list of them."""
    items = [{url_key: url} for url in urls]
    return json.dumps(items[0] if len(items) == 1 else items, ensure_ascii=False)


def _load_media_references(
    items: list[str | None],
    *,
    limit: int,
    default_mime: str,
    max_bytes: int | None = _MAX_REFERENCE_BYTES,
) -> list[Reference]:
    """Deduped usable references of one media kind, capped at ``limit``."""
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = (item or "").strip()
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    references: list[Reference] = []
    for item in ordered:
        loaded = _read_reference(item, default_mime)
        if loaded is None:
            continue
        if max_bytes is not None and not isinstance(loaded, str) and len(loaded[1]) > max_bytes:
            logger.warning("[vLLM-Omni] reference exceeds 30 MiB, skipped: %s", loaded[0])
            continue
        references.append(loaded)
        if len(references) >= limit:
            break
    return references


def _load_video_references(
    first_frame: str | None,
    reference_images: list[str] | None,
) -> list[Reference]:
    """First frame + reference images, deduped, capped at the H3 image limit."""
    return _load_media_references(
        [first_frame, *(reference_images or [])],
        limit=_MAX_REFERENCE_IMAGES,
        default_mime="image/png",
    )


def _cap_total_references(
    images: list[Reference],
    videos: list[Reference],
    audios: list[Reference],
) -> tuple[list[Reference], list[Reference], list[Reference]]:
    """Trim to the Ref2VA total, dropping audios first, then videos."""
    budget = _MAX_TOTAL_REFERENCES
    images = images[:budget]
    budget -= len(images)
    videos = videos[:budget]
    budget -= len(videos)
    audios = audios[:budget]
    return images, videos, audios


def _video_reference_parts(
    images: list[Reference],
    videos: list[Reference],
    audios: list[Reference],
) -> tuple[dict[str, str], list[tuple[str, ReferenceUpload]]]:
    """Form fields and multipart files for one video request's references."""
    url_images = [item for item in images if isinstance(item, str)]
    url_videos = [item for item in videos if isinstance(item, str)]
    url_audios = [item for item in audios if isinstance(item, str)]
    uploads = [item for item in (*images, *videos, *audios) if not isinstance(item, str)]
    if uploads and (url_images or url_videos):
        raise ValueError(
            "vLLM-Omni video references cannot mix uploaded files with image or video URLs."
        )
    fields: dict[str, str] = {}
    if url_images:
        fields["image_reference"] = _json_reference_field(url_images, "image_url")
    if url_videos:
        fields["video_reference"] = _json_reference_field(url_videos, "video_url")
    if url_audios:
        fields["audio_reference"] = _json_reference_field(url_audios, "audio_url")
    if not uploads:
        return fields, []
    field = "input_reference" if len(uploads) == 1 else "input_references"
    return fields, [(field, item) for item in uploads]


# ---------------------------------------------------------------------------
# Video generation (async /v1/videos + poll + content download)
# ---------------------------------------------------------------------------


def _generated_output_path(output_dir: Path | None, suffix: str) -> Path:
    root = output_dir or get_agent_workspace_dir()
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return root / f"generated_{timestamp}_{random.randint(1000, 9999)}.{suffix}"


@dataclass(frozen=True)
class VllmOmniVideoStatus:
    """One poll of ``GET {api_base}/videos/{id}``."""

    status: str
    error: str | None = None

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def failed(self) -> bool:
        return self.status in {"failed", "cancelled", "canceled", "expired"}


def submit_vllm_omni_video_sync(
    prompt: str,
    /,
    *,
    api_key: str,
    api_base: str,
    model: str,
    size: str | None,
    duration: int | float,
    resolution: str | None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    reference_videos: list[str] | None = None,
    reference_audios: list[str] | None = None,
    fps: int | None = None,
    negative_prompt: str | None = None,
    extra_params: Mapping[str, Any] | None = None,
    **extra_fields: Any,
) -> str:
    """Create a vLLM-Omni video job (``POST /v1/videos``) and return its id.

    ``fps`` replaces the pinned provider fps; ``extra_params`` merges over the
    spec-built ``extra_params`` JSON; ``extra_fields`` are top-level request
    fields (sampling / model params) that override the spec-built ones.
    """
    overrides = _checked_extra_fields(extra_fields)
    base = (api_base or "").strip().rstrip("/")
    if not base:
        raise ValueError("VIDEO_GEN_API_BASE is required for the vLLM-Omni backend.")
    headers = _auth_headers(api_key)

    # Request 1 of 2: discover the actually served model. It drives both the
    # ``model`` field (the server 400s on a mismatch) and the spec registry.
    served_model_id = fetch_served_model_id(base, api_key)
    model_to_send = served_model_id or (model or "").strip() or None
    spec = match_video_spec(served_model_id)
    if served_model_id and spec is None:
        logger.info(
            "[vLLM-Omni] served model %s is not registered; no extra_params built",
            served_model_id,
        )

    images = _load_video_references(first_frame, reference_images)
    videos = _load_media_references(
        list(reference_videos or []),
        limit=_MAX_REFERENCE_VIDEOS,
        default_mime="video/mp4",
        max_bytes=None,
    )
    audios = _load_media_references(
        list(reference_audios or []),
        limit=_MAX_REFERENCE_AUDIOS,
        default_mime="audio/mpeg",
        max_bytes=None,
    )
    requested = bool(first_frame or reference_images or reference_videos or reference_audios)
    if requested and not (images or videos or audios):
        raise ValueError(
            "reference files were provided but none could be read as local files or URLs."
        )
    if audios and not (images or videos):
        raise ValueError(
            "vLLM-Omni references need at least one image or video; audio-only is not supported."
        )
    images, videos, audios = _cap_total_references(images, videos, audios)
    reference_count = len(images) + len(videos) + len(audios)
    inputs = VllmOmniVideoInputs(
        size=size,
        duration=duration,
        resolution=resolution,
        has_references=reference_count > 0,
        fps=fps,
    )
    form = spec.build(inputs) if spec else _build_generic_video_form(inputs)

    fields: dict[str, str] = {
        "prompt": prompt,
        "fps": str(fps or _VIDEO_FPS),
        **form.fields,
    }
    if negative_prompt:
        fields["negative_prompt"] = negative_prompt
    fields.update({key: _form_value(value) for key, value in overrides.items()})
    if model_to_send:
        fields["model"] = model_to_send
    merged_extra_params = {**(form.extra_params or {}), **(extra_params or {})}
    if merged_extra_params:
        # Without explicit extra fields there is no seed / quality: reproducibility
        # and cache policies stay server-side.
        fields["extra_params"] = json.dumps(merged_extra_params, ensure_ascii=False)
    reference_fields, files = _video_reference_parts(images, videos, audios)
    fields.update(reference_fields)

    logger.info(
        "[vLLM-Omni] video create model=%s spec=%s fields=%s refs=%d uploads=%d",
        model_to_send,
        spec.key if spec else None,
        sorted(fields),
        reference_count,
        len(files),
    )
    response = _http_request(
        "POST",
        f"{base}/videos",
        headers=headers,
        data=fields,
        files=files or None,
        timeout=_CREATE_TIMEOUT_SECONDS,
    )
    if not response.ok:
        raise ValueError(
            f"vLLM-Omni video create failed {response.status_code}: {_error_message(response)}"
        )
    body = response.json()
    video_id = str(body.get("id") or "").strip()
    if not video_id:
        raise ValueError(f"vLLM-Omni video create response missing id: {body}")
    return video_id


def query_vllm_omni_video_sync(*, api_key: str, api_base: str, video_id: str) -> VllmOmniVideoStatus:
    """Poll one vLLM-Omni video job."""
    base = (api_base or "").strip().rstrip("/")
    poll = _http_request("GET", f"{base}/videos/{video_id}", headers=_auth_headers(api_key), timeout=60)
    if not poll.ok:
        raise ValueError(f"vLLM-Omni poll failed {poll.status_code}: {_error_message(poll)}")
    payload = poll.json()
    status = str(payload.get("status") or "").strip().lower()
    err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    err_msg = str(err.get("message") or "").strip() or None
    return VllmOmniVideoStatus(status=status or "unknown", error=err_msg)


def download_vllm_omni_video_sync(
    *, api_key: str, api_base: str, video_id: str, output_path: Path
) -> Path:
    """Save a completed vLLM-Omni video job's content to ``output_path``."""
    base = (api_base or "").strip().rstrip("/")
    response = _http_request(
        "GET",
        f"{base}/videos/{video_id}/content",
        headers=_auth_headers(api_key),
        timeout=_DOWNLOAD_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(response.content)
    return output_path


def invoke_vllm_omni_video_generation_sync(
    prompt: str,
    /,
    *,
    api_key: str,
    api_base: str,
    model: str,
    size: str | None,
    duration: int | float,
    resolution: str | None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    reference_videos: list[str] | None = None,
    reference_audios: list[str] | None = None,
    fps: int | None = None,
    negative_prompt: str | None = None,
    extra_params: Mapping[str, Any] | None = None,
    **extra_fields: Any,
) -> dict[str, Any]:
    """vLLM-Omni async video generation (``POST /v1/videos`` + poll + download).

    Blocks until the job finishes; see ``submit_vllm_omni_video_sync`` for the
    meaning of ``fps`` / ``extra_params`` / ``extra_fields``.
    """
    video_id = submit_vllm_omni_video_sync(
        prompt,
        api_key=api_key,
        api_base=api_base,
        model=model,
        size=size,
        duration=duration,
        resolution=resolution,
        first_frame=first_frame,
        reference_images=reference_images,
        reference_videos=reference_videos,
        reference_audios=reference_audios,
        fps=fps,
        negative_prompt=negative_prompt,
        extra_params=extra_params,
        **extra_fields,
    )
    deadline_ts = time.monotonic() + _POLL_TIMEOUT_SECONDS
    last_status = "unknown"
    while time.monotonic() < deadline_ts:
        job = query_vllm_omni_video_sync(api_key=api_key, api_base=api_base, video_id=video_id)
        last_status = job.status
        if job.completed:
            output_path = download_vllm_omni_video_sync(
                api_key=api_key,
                api_base=api_base,
                video_id=video_id,
                output_path=_generated_output_path(None, "mp4"),
            )
            return {
                "video_path": str(output_path.absolute()),
                "revised_prompt": prompt,
            }
        if job.failed:
            raise ValueError(job.error or f"vLLM-Omni video generation {job.status}")
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"vLLM-Omni video generation timed out (last status={last_status})")


# ---------------------------------------------------------------------------
# Image generation (OpenAI-compatible /v1/images/generations + /v1/images/edits)
# ---------------------------------------------------------------------------


def _load_image_references(
    reference_images: list[str] | None,
) -> list[Reference]:
    """Deduped edit input images; unreadable or oversized local ones are skipped."""
    ordered: list[str] = []
    seen: set[str] = set()
    for item in reference_images or []:
        value = (item or "").strip()
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    references: list[Reference] = []
    for item in ordered:
        loaded = _read_reference(item)
        if loaded is None:
            continue
        if not isinstance(loaded, str) and len(loaded[1]) > _MAX_REFERENCE_BYTES:
            logger.warning("[vLLM-Omni] reference exceeds 30 MiB, skipped: %s", loaded[0])
            continue
        references.append(loaded)
    return references


def _image_edit_reference_parts(
    references: list[Reference],
) -> tuple[dict[str, Any], list[tuple[str, ReferenceUpload]] | None]:
    """Form fields and multipart files for one image-edit request."""
    files = [("image", item) for item in references if not isinstance(item, str)]
    urls = [item for item in references if isinstance(item, str)]
    return ({"url": urls} if urls else {}), (files or None)


def _save_image_response_body(
    body: Any, prompt: str, api_key: str, output_dir: Path | None = None
) -> dict[str, Any]:
    """Shared response tail for /images/generations and /images/edits."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or not data:
        raise ValueError(f"vLLM-Omni image response missing data: {body}")
    first = data[0] if isinstance(data[0], dict) else {}
    image_b64 = str(first.get("b64_json") or "").strip() or None
    image_url = str(first.get("url") or "").strip() or None

    output_path = _generated_output_path(output_dir, "png")
    if image_b64:
        with open(output_path, "wb") as f:
            f.write(base64.b64decode(image_b64))
        return {"image_path": str(output_path.absolute()), "revised_prompt": prompt}
    if image_url:
        download = _http_request("GET", image_url, headers=_auth_headers(api_key), timeout=120)
        download.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(download.content)
        return {
            "image_path": str(output_path.absolute()),
            "revised_prompt": prompt,
            "original_url": image_url,
        }
    raise ValueError("vLLM-Omni image generation succeeded but returned no image data")


def invoke_vllm_omni_image_generation_sync(
    prompt: str,
    /,
    *,
    api_key: str,
    api_base: str,
    model: str,
    size: str | None,
    reference_images: list[str] | None = None,
    negative_prompt: str | None = None,
    output_dir: Path | None = None,
    **extra_fields: Any,
) -> dict[str, Any]:
    """vLLM-Omni image generation.

    Plain prompt rides text-to-image (``POST /v1/images/generations``, JSON);
    with reference images it rides image-to-image (``POST /v1/images/edits``,
    multipart, repeated ``image`` fields). Seed / quality are only sent when a
    caller passes them in ``extra_fields`` (top-level request fields).
    """
    overrides = _checked_extra_fields(extra_fields)
    base = (api_base or "").strip().rstrip("/")
    if not base:
        raise ValueError("IMAGE_GEN_API_BASE is required for the vLLM-Omni backend.")

    # The model field is optional server-side; prefer the configured name and
    # otherwise discover the served model (a mismatching name would be a 400).
    model_to_send = (model or "").strip() or fetch_served_model_id(base, api_key)
    parsed = _parse_size(size)

    references = _load_image_references(reference_images)
    if reference_images and not references:
        raise ValueError(
            "reference images were provided but none could be read as local files or URLs."
        )

    headers = _auth_headers(api_key)
    if references:
        # Image-to-image. Without an explicit size the server keeps "auto" and
        # infers dimensions from the first input image.
        fields: dict[str, Any] = {"prompt": prompt}
        if model_to_send:
            fields["model"] = model_to_send
        if parsed is not None:
            fields["size"] = f"{parsed[0]}x{parsed[1]}"
        if negative_prompt:
            fields["negative_prompt"] = negative_prompt
        fields.update({key: _form_value(value) for key, value in overrides.items()})
        reference_fields, files = _image_edit_reference_parts(references)
        fields.update(reference_fields)
        logger.info(
            "[vLLM-Omni] image edit model=%s size=%s refs=%d uploads=%d",
            model_to_send,
            fields.get("size"),
            len(references),
            len(files or []),
        )
        response = _http_request(
            "POST",
            f"{base}/images/edits",
            headers=headers,
            data=fields,
            files=files,
            timeout=600,
        )
    else:
        payload: dict[str, Any] = {"prompt": prompt, "response_format": "b64_json"}
        if parsed is not None:
            payload["size"] = f"{parsed[0]}x{parsed[1]}"
        if model_to_send:
            payload["model"] = model_to_send
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        payload.update(overrides)
        logger.info("[vLLM-Omni] image create model=%s size=%s", model_to_send, payload.get("size"))
        response = _http_request(
            "POST",
            f"{base}/images/generations",
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
            timeout=600,
        )
    if not response.ok:
        raise ValueError(
            f"vLLM-Omni image create failed {response.status_code}: {_error_message(response)}"
        )
    return _save_image_response_body(response.json(), prompt, api_key, output_dir)
