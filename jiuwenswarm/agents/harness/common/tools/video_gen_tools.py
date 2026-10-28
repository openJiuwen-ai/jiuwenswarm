# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Text-to-video / image-to-video generation tools.

Video generation is a submit-then-poll job (can take minutes), not a single
request/response call. generate_video submits the job and polls for up to
_MAX_POLL_SECONDS synchronously (a short wait is fine inside one tool call);
if the job is still running after that, it returns the job_id and tells the
agent to call check_video_status instead of blocking indefinitely or
pretending the video is ready when it isn't.

Credentials come from a dedicated "Video processing" config panel slot
(VIDEO_GEN_API_KEY/VIDEO_GEN_API_BASE/VIDEO_GEN_MODEL_NAME/VIDEO_GEN_PROVIDER)
- separate from the "Video Model" slot video_understanding reads, so the two
capabilities can point at independent models (e.g. a chat-completions-style
video-analysis model for understanding, and OpenRouter's
bytedance/seedance-2.0-fast for generation) without one breaking the other.
There is no built-in default endpoint/model: whatever's configured there is
what these tools use, and api_key/api_base/model must all be set (a
self-deployed vLLM-Omni endpoint needs only the API URL).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    RootDecisionContext,
    current_root_decision_context,
)
from jiuwenswarm.agents.harness.common.tools import gen_toolkits
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify
from jiuwenswarm.common.chat_final import FINAL_MODE_APPEND, annotate_chat_final
from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.runtime.host_services import send_runtime_push

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 10
_MAX_POLL_SECONDS = 120
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}
_VIDEO_MIN_SECONDS = 4
_VIDEO_MAX_SECONDS = 15
# MiniMax-H3-Max rejects clips shorter than 5 s.
_H3_MAX_MIN_SECONDS = 5

# A video job that is still running when generate_video's/check_video_status's
# own bounded poll window gives up is not forgotten here: this module keeps
# polling it in the background and pushes the outcome into the chat once it
# settles. Without this, a slow job submitted in Team/multi-agent mode is
# never surfaced again once that round's stream ends (GitCode #5806) - the
# user has to think to ask again, and the agent has to think to re-call
# check_video_status, neither of which is guaranteed once the turn is over.
_WATCH_POLL_INTERVAL_SECONDS = 20
_WATCH_MAX_SECONDS = 30 * 60
# All of generate_video's/check_video_status's own "still running" messages
# (both the direct OpenRouter-style path here and every gen_toolkits backend)
# share this "Video job <id> submitted and still ..." / "Video job <id> is
# still ..." shape - see _pending_submit_message and the check_video_status
# paths below.
_PENDING_JOB_ID_RE = re.compile(r"^Video job (\S+) (?:submitted and still|is still) \w+")
_SAVED_TO_RE = re.compile(r"Saved to: (.+?)\n")
# Every backend's provider-reported terminal failure shares this exact shape
# (gen_toolkits.py's four _*_finish helpers and this module's own direct
# path all format it as f"[ERROR]: video job {id} ended with status {status}: ...").
_TERMINAL_FAILURE_RE = re.compile(r"^\[ERROR\]: video job \S+ ended with status \w+")
# job_ids with an active background watcher - guards against piling up a
# second watcher every time the agent (or this module itself) polls a job
# that is still pending.
_WATCHED_PENDING_JOBS: set[str] = set()
# Keeps the scheduled tasks referenced so they aren't garbage-collected mid-flight.
_BACKGROUND_WATCH_TASKS: set[asyncio.Task[None]] = set()


def _extract_pending_job_id(message: str) -> str | None:
    match = _PENDING_JOB_ID_RE.match(message or "")
    return match.group(1) if match else None


def _is_terminal_job_result(result: str) -> bool:
    """Whether *result* is a definitive outcome - success, or a provider-
    reported terminal status - rather than a transient/intermediate error
    (a non-200 poll response, invalid JSON, a momentary download hiccup,
    credentials cleared mid-watch, an unrecognized error shape, ...) that
    should just be retried instead of reported as a false failure.

    Used instead of "not a recognized still-pending message" to decide
    whether the watcher has something final to deliver: every result string
    this module or gen_toolkits.py can produce for an error that is NOT a
    genuine terminal outcome is also not one of these two shapes, so
    treating anything else as "keep polling" never mistakes a transient
    hiccup for completion.
    """
    return result.startswith("Video generated successfully!") or bool(_TERMINAL_FAILURE_RE.match(result))


def _schedule_completion_watch_if_pending(result: str, save_dir: str | None) -> None:
    """Arm a background watcher the first time *result* reports a still-running job."""
    job_id = _extract_pending_job_id(result)
    if not job_id or job_id in _WATCHED_PENDING_JOBS:
        return
    context = current_root_decision_context()
    if context is None:
        # No session/request to deliver to (e.g. a CLI/offline call) - the
        # caller is expected to poll check_video_status itself.
        return
    _WATCHED_PENDING_JOBS.add(job_id)
    task = asyncio.create_task(_watch_until_settled(job_id, save_dir, context))
    _BACKGROUND_WATCH_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_WATCH_TASKS.discard)


async def _watch_until_settled(
    job_id: str, save_dir: str | None, context: RootDecisionContext
) -> None:
    try:
        elapsed = 0
        while elapsed < _WATCH_MAX_SECONDS:
            await asyncio.sleep(_WATCH_POLL_INTERVAL_SECONDS)
            elapsed += _WATCH_POLL_INTERVAL_SECONDS
            result = await _check_video_status_impl(job_id, save_dir)
            if _is_terminal_job_result(result):
                await _deliver_watch_result(job_id, result, context)
                return
            if _extract_pending_job_id(result) is None:
                # Not a recognized "still pending" message either - a
                # transient/intermediate error. Keep waiting rather than
                # guessing: see _is_terminal_job_result.
                logger.warning(
                    "[generate_video] non-terminal error while watching job %s, retrying: %s",
                    job_id, result,
                )
        logger.warning(
            "[generate_video] gave up watching job %s for completion after %ss",
            job_id, elapsed,
        )
    except Exception:
        logger.exception("[generate_video] background completion watch failed for job %s", job_id)
    finally:
        _WATCHED_PENDING_JOBS.discard(job_id)


# SendFileToolkit.send_file never raises - a failure to deliver (no active
# Runtime push host, which is exactly the "turn already ended" scenario this
# watcher exists for) comes back as one of these Chinese failure strings
# instead (see send_file_to_user.py's _send_file_with_envelope/send_file).
_SEND_FILE_FAILURE_PREFIXES = ("发送文件失败", "提交文件失败")


async def _deliver_watch_result(job_id: str, result: str, context: RootDecisionContext) -> None:
    """Push a settled job's outcome into the chat it was submitted from.

    There is no live agent turn left to read this tool's return value once
    the bounded poll window (and the turn/stream that originally called
    generate_video) has already ended - this is the proactive-delivery path
    send_file_to_user already uses for files, reused here for the same reason.
    """
    saved_to = _SAVED_TO_RE.search(result + "\n")
    if saved_to:
        from jiuwenswarm.agents.harness.common.tools.send_file_to_user import SendFileToolkit

        local_path = saved_to.group(1).strip()
        toolkit = SendFileToolkit(context.request_id, context.session_id, context.channel_id)
        delivery = await toolkit.send_file(local_path)
        if delivery.startswith(_SEND_FILE_FAILURE_PREFIXES):
            # The video itself is fine - only the chat delivery failed - so
            # don't claim success, and don't lose the user's generated video
            # either: fall back to the same text-notice push as a true
            # generation failure, but say where the local file is.
            logger.warning(
                "[generate_video] send_file_to_user failed delivering completed job %s to session %s: %s",
                job_id, context.session_id, delivery,
            )
            await _push_fallback_notice(
                job_id, context,
                f"视频已生成，但自动投递到会话失败：{delivery}\n本地文件：{local_path}",
            )
            return
        logger.info(
            "[generate_video] delivered completed job %s to session %s: %s",
            job_id, context.session_id, delivery,
        )
        return

    await _push_fallback_notice(job_id, context, f"视频生成任务 {job_id} 未能成功完成：{result}")


async def _push_fallback_notice(job_id: str, context: RootDecisionContext, content: str) -> None:
    pushed = await send_runtime_push(
        {
            "request_id": context.request_id,
            "channel_id": context.channel_id,
            "session_id": context.session_id,
            "payload": annotate_chat_final({"content": content}, final_mode=FINAL_MODE_APPEND),
            "is_complete": False,
        }
    )
    if not pushed:
        logger.warning(
            "[generate_video] no active push host to deliver job %s result to session %s",
            job_id, context.session_id,
        )


def video_gen_enabled() -> bool:
    """Whether the "Video processing" switch in configuration settings is on.

    Mirrors multimodal_model_enabled's env-var gate (see multimodal_config.py):
    VIDEO_GEN_ENABLED is set from that switch and checked independently of
    whether the Video processing config itself is complete, so a user can
    leave valid credentials in place while still turning generation off.
    """
    return str(os.environ.get("VIDEO_GEN_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on")


def _get_video_gen_api_credentials() -> tuple[str, str, str]:
    """Resolve video-generation credentials from the dedicated "Video
    processing" config panel slot only (VIDEO_GEN_API_KEY/VIDEO_GEN_API_BASE/
    VIDEO_GEN_MODEL_NAME). No fallback env vars, no built-in default endpoint
    or model - an empty string means unconfigured.
    """
    api_key = os.environ.get("VIDEO_GEN_API_KEY", "").strip()
    api_base = os.environ.get("VIDEO_GEN_API_BASE", "").strip()
    model = os.environ.get("VIDEO_GEN_MODEL_NAME", "").strip()
    return api_key, api_base, model


def video_gen_configured() -> bool:
    """Whether the Video processing config (key/base/model) is complete.

    Public entry point for callers outside this module (interface_deep.py,
    swarm/providers/tools.py) that only need a yes/no gate and shouldn't
    depend on _get_video_gen_api_credentials' private tuple shape.
    """
    return _credentials_complete(*_get_video_gen_api_credentials())


def _credentials_complete(api_key: str, api_base: str, model: str) -> bool:
    if gen_toolkits.detect_backend("VIDEO_GEN_PROTOCOL", api_base) in gen_toolkits.KEYLESS_BACKENDS:
        return bool(api_base)
    return bool(api_key and api_base and model)


def _clamp_duration(model: str, duration_seconds: int) -> int:
    compact = "".join(ch for ch in model.lower() if ch.isalnum())
    low = _H3_MAX_MIN_SECONDS if "h3max" in compact else _VIDEO_MIN_SECONDS
    return max(low, min(_VIDEO_MAX_SECONDS, int(duration_seconds)))


def _resolve_frame_reference(path_or_url: str, arg_name: str = "first_frame_path") -> tuple[str | None, str | None]:
    """first_frame_path may be a real http(s) URL, an already-complete data:
    URI, or a local file path - resolved to a data: URI here (server-side)
    rather than asked of the calling agent, since a large base64 blob is not
    something an LLM can reliably reproduce byte-for-byte through its own
    generation.
    """
    value = (path_or_url or "").strip()
    if not value:
        return None, None
    if value.startswith(("http://", "https://", "data:")):
        return value, None
    path = Path(value).expanduser()
    if not path.is_file():
        return None, f"[ERROR]: {arg_name} {value!r} is not a URL/data URI and no such file exists."
    mime, _ = mimetypes.guess_type(str(path))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}", None


def _resolve_save_path(save_dir: str | None, filename: str) -> Path:
    root = Path(save_dir).expanduser() if save_dir else (get_agent_workspace_dir() / "generated_videos")
    root.mkdir(parents=True, exist_ok=True)
    return root / filename


@dataclass(frozen=True)
class _JobContext:
    """Shared per-request transport/identity context for a video-generation
    job - bundles what would otherwise be 3 separate, always-travel-together
    parameters (client/headers/job_id) across _poll_job/_download_video and
    their callers (see G.FNM.03: too many arguments)."""

    client: httpx.AsyncClient
    headers: dict[str, str]
    job_id: str


async def _download_video(ctx: _JobContext, api_base: str, save_dir: str | None) -> str:
    try:
        content = await ctx.client.get(
            f"{api_base}/videos/{ctx.job_id}/content", params={"index": 0}, headers=ctx.headers
        )
    except httpx.HTTPError as exc:
        return f"[ERROR]: downloading video job {ctx.job_id} failed: {exc!r}"
    if content.status_code != 200:
        return f"[ERROR]: video job {ctx.job_id} completed but downloading content failed: {content.status_code}"

    filename = f"video_{ctx.job_id}.mp4"
    try:
        target = _resolve_save_path(save_dir, filename)
        target.write_bytes(content.content)
    except OSError as exc:
        return f"[ERROR]: failed to save video ({filename}) to {save_dir or 'agent workspace'}: {exc!r}"
    return (
        "Video generated successfully!\n"
        f"Saved to: {target}\n"
        f"(job {ctx.job_id} - the remote source URL expires 24h after completion, so this "
        "local file is now the durable copy.)"
    )


async def _poll_job(
    ctx: _JobContext,
    polling_url: str,
    status: str,
    job: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], int, str | None]:
    """Poll until the job reaches a terminal status or the poll budget runs
    out.

    Returns (status, job, elapsed_seconds, error). error is None on success
    (including the ordinary "still running after budget" case, which the
    caller reports itself); when set, the caller should return it directly
    as the tool's result rather than relying on status/job/elapsed. A
    non-200 poll response or a malformed JSON body is reported this way
    instead of raised: a raised exception here would only be caught by
    generate_video's ``except httpx.HTTPError``, and neither RuntimeError
    nor json.JSONDecodeError (a ValueError) is an instance of that, which
    previously let both crash the tool call instead of degrading gracefully.
    """
    # Seeded with the caller's already-known job state (e.g. the submit
    # response) rather than {}, so that a job already terminal on submit -
    # no polling iteration ever runs - doesn't lose its "error" field to an
    # empty dict here.
    job = dict(job) if job else {}
    elapsed = 0
    while status not in _TERMINAL_STATUSES and elapsed < _MAX_POLL_SECONDS:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS
        poll = await ctx.client.get(polling_url, headers=ctx.headers)
        if poll.status_code != 200:
            return status, job, elapsed, (
                f"[ERROR]: polling video job {ctx.job_id} failed: {poll.status_code} {poll.text}"
            )
        try:
            job = poll.json()
        except ValueError as exc:
            return status, job, elapsed, (
                f"[ERROR]: polling video job {ctx.job_id} returned invalid JSON: {exc!r}"
            )
        status = job.get("status", "pending")
    return status, job, elapsed, None


@tool(
    name="generate_video",
    description=(
        "Generate a video clip from a text prompt (and optionally a first-frame image, or "
        "reference images the clip must stay consistent with) using AI video generation "
        "models. Use this tool when the user wants to create or generate a video based on "
        "a text description. Video generation can take "
        "several minutes; if this tool returns a job_id instead of a finished video, call "
        "check_video_status with that job_id to keep waiting for it instead of resubmitting."
    ),
)
async def generate_video(  # pylint: disable=huawei-too-many-arguments
    prompt: str,
    aspect_ratio: str = "16:9",
    resolution: str = "480p",
    duration_seconds: int = 15,
    first_frame_path: str | None = None,
    generate_audio: bool = False,
    save_dir: str | None = None,
    reference_image_paths: list[str] | None = None,
    reference_mode: bool = False,
) -> str:
    """
    Generate a video from a text prompt.

    Deliberately not bundled into a dataclass/BaseModel despite the arg
    count (see G.FNM.03): this signature is what CallableSchemaExtractor
    (openjiuwen) auto-extracts into the tool's JSON tool-calling schema for
    the LLM. A plain dataclass param collapses to an opaque, propertyless
    `{"type": "object"}` there (no registered extractor for dataclasses,
    only pydantic BaseModel) - confirmed directly against
    CallableSchemaExtractor.get_type_schema - which would leave the model
    with no visible fields to call this tool with at all. A BaseModel would
    expand correctly, but changes the calling convention from flat
    top-level args to one nested object, which is a real behavioral change
    to an already-verified tool, not a mechanical refactor - flat args are
    kept intentionally.

    Args:
        prompt: Text description of the video to generate.
        aspect_ratio: e.g. "16:9", "9:16", "1:1".
        resolution: e.g. "480p", "720p", "1080p".
        duration_seconds: Clip length in seconds (model-dependent, typically 5-15).
        first_frame_path: Optional local file path, http(s) URL, or data: URI to
            condition the first frame on (image-to-video).
        generate_audio: Whether to request native audio generation, if supported
            by the model.
        save_dir: Optional directory to save the video (defaults to the agent
            workspace's generated_videos/ folder).
        reference_image_paths: Optional local file paths, http(s) URLs or data:
            URIs of images (characters, props, scenes) the clip must stay
            consistent with (reference-to-video).
        reference_mode: Treat first_frame_path as one more reference image
            instead of the literal opening frame.

    Returns:
        Path to the generated video file, or a job_id + status message if the
        job is still running after the bounded poll window.
    """
    api_key, api_base, model = _get_video_gen_api_credentials()
    if not _credentials_complete(api_key, api_base, model):
        return (
            "[ERROR]: video generation is not configured - set the Video processing "
            "API key, API URL, and model name in configuration settings."
        )
    prompt = (prompt or "").strip()
    if not prompt:
        return "[ERROR]: prompt is required."

    frame_data_uri = None
    if first_frame_path:
        frame_data_uri, err = _resolve_frame_reference(first_frame_path)
        if err:
            return err
    reference_uris: list[str] = []
    for path in reference_image_paths or []:
        uri, err = _resolve_frame_reference(path, "reference_image_paths")
        if err:
            return err
        reference_uris.append(uri)
    if reference_mode and not (reference_uris or frame_data_uri):
        return "[ERROR]: reference_mode needs reference_image_paths or first_frame_path."

    # MiniMax (v2 task API), BytePlus ModelArk (Seedance), DashScope (Wan) and
    # vLLM-Omni are not OpenRouter's /videos API, so they have their own backends.
    backend = gen_toolkits.detect_backend("VIDEO_GEN_PROTOCOL", api_base)
    if backend:
        target = gen_toolkits.GenerationTarget(backend, api_key, api_base, model)
        if backend != gen_toolkits.VLLM_OMNI:
            duration_seconds = _clamp_duration(model, duration_seconds)
        request = gen_toolkits.VideoRequest(
            prompt, aspect_ratio, resolution, duration_seconds, generate_audio, frame_data_uri,
            reference_image_uris=tuple(reference_uris), reference_mode=reference_mode,
        )
        result = await gen_toolkits.submit_video(target, request, save_dir)
        _schedule_completion_watch_if_pending(result, save_dir)
        return result
    if reference_uris or reference_mode:
        return (
            f"[ERROR]: the configured video model {model!r} ({api_base}) only supports text-to-video and "
            "first-frame image-to-video, not reference images. Retry with first_frame_path only, or ask the "
            "user to switch Settings > Agent (Video generation) to a provider that supports reference-to-video: "
            "MiniMax (MiniMax-H3), BytePlus ModelArk / Volcengine (Seedance 2.x), Alibaba DashScope "
            "(wan3.0-video / wan3.0-video-prime) or a self-deployed vLLM-Omni server."
        )

    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "duration": duration_seconds,
        "generate_audio": bool(generate_audio),
    }
    if frame_data_uri:
        body["frame_images"] = [
            {"type": "image_url", "image_url": {"url": frame_data_uri}, "frame_type": "first_frame"}
        ]

    headers = {"Authorization": f"Bearer {api_key}"}
    logger.info(
        "[generate_video] using model: %s (api_base: %s, aspect_ratio: %s, resolution: %s, duration: %ss)",
        model, api_base, aspect_ratio, resolution, duration_seconds,
    )

    try:
        async with httpx.AsyncClient(timeout=60, verify=get_requests_verify()) as client:
            submit = await client.post(f"{api_base}/videos", headers=headers, json=body)
            if submit.status_code not in (200, 201, 202):
                return f"[ERROR]: video generation submit failed: {submit.status_code} {submit.text}"
            try:
                job = submit.json()
            except ValueError as exc:
                return f"[ERROR]: video generation submit returned invalid JSON: {exc!r}"
            job_id = job.get("id")
            if not job_id:
                return f"[ERROR]: video generation submit returned no job id: {job}"
            polling_url = job.get("polling_url") or f"{api_base}/videos/{job_id}"
            status = job.get("status", "pending")
            ctx = _JobContext(client=client, headers=headers, job_id=job_id)

            status, job, elapsed, poll_error = await _poll_job(ctx, polling_url, status, job)
            if poll_error:
                return poll_error

            if status not in _TERMINAL_STATUSES:
                pending_message = (
                    f"Video job {job_id} submitted and still {status} after {elapsed}s - generation can "
                    f"take several minutes. Call check_video_status with job_id={job_id} to check progress "
                    "and download it once ready."
                )
                _schedule_completion_watch_if_pending(pending_message, save_dir)
                return pending_message
            if status != "completed":
                return (
                    f"[ERROR]: video job {job_id} ended with status {status}: "
                    f"{job.get('error', 'no error detail provided')}"
                )

            return await _download_video(ctx, api_base, save_dir)
    except httpx.HTTPError as exc:
        return f"[ERROR]: video generation request failed: {exc!r}"


async def _check_video_status_impl(job_id: str, save_dir: str | None = None) -> str:
    api_key, api_base, model = _get_video_gen_api_credentials()
    backend = gen_toolkits.detect_backend("VIDEO_GEN_PROTOCOL", api_base)
    if not (api_base and (api_key or backend in gen_toolkits.KEYLESS_BACKENDS)):
        return (
            "[ERROR]: video generation is not configured - set the Video processing "
            "API key, API URL, and model name in configuration settings."
        )
    job_id = (job_id or "").strip()
    if not job_id:
        return "[ERROR]: job_id is required."

    if backend:
        target = gen_toolkits.GenerationTarget(backend, api_key, api_base, model)
        result = await gen_toolkits.check_video(target, job_id, save_dir)
        _schedule_completion_watch_if_pending(result, save_dir)
        return result

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=30, verify=get_requests_verify()) as client:
            poll = await client.get(f"{api_base}/videos/{job_id}", headers=headers)
            if poll.status_code != 200:
                return f"[ERROR]: checking video job {job_id} failed: {poll.status_code} {poll.text}"
            try:
                job = poll.json()
            except ValueError as exc:
                return f"[ERROR]: checking video job {job_id} returned invalid JSON: {exc!r}"
            status = job.get("status", "unknown")
            if status == "completed":
                ctx = _JobContext(client=client, headers=headers, job_id=job_id)
                return await _download_video(ctx, api_base, save_dir)
            if status in _TERMINAL_STATUSES:
                return (
                    f"[ERROR]: video job {job_id} ended with status {status}: "
                    f"{job.get('error', 'no error detail provided')}"
                )
            pending_message = f"Video job {job_id} is still {status}."
            _schedule_completion_watch_if_pending(pending_message, save_dir)
            return pending_message
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        # A transient provider-reachability blip is not a terminal outcome -
        # the job keeps running server-side. Reporting it as [ERROR] here
        # would fail _PENDING_JOB_ID_RE and make _watch_until_settled treat
        # the job as settled, pushing a bogus failure notice and giving up
        # instead of retrying (matches gen_toolkits' own backends, which all
        # distinguish these via _TRANSIENT_HTTP_ERRORS/_unreachable_job_message).
        pending_message = (
            f"Video job {job_id} is still unconfirmed - the provider could not be reached ({exc!r}). "
            "Call check_video_status again to keep waiting (do not use shell sleep)."
        )
        _schedule_completion_watch_if_pending(pending_message, save_dir)
        return pending_message
    except httpx.HTTPError as exc:
        return f"[ERROR]: checking video job {job_id} failed: {exc!r}"


@tool(
    name="check_video_status",
    description=(
        "Poll a previously submitted generate_video job by job_id and download it once "
        "complete. Use this after generate_video returns a job_id instead of a finished video."
    ),
)
async def check_video_status(job_id: str, save_dir: str | None = None) -> str:
    """
    Check the status of a video generation job, downloading it if ready.

    Args:
        job_id: The job id returned by generate_video.
        save_dir: Optional directory to save the video (defaults to the agent
            workspace's generated_videos/ folder).

    Returns:
        Path to the generated video file, or a status message if still running.
    """
    return await _check_video_status_impl(job_id, save_dir)
