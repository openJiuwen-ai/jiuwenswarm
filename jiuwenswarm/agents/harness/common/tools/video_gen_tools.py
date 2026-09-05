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

Credentials come solely from the existing "Video Model" config panel slot
(VIDEO_API_KEY/VIDEO_API_BASE/VIDEO_MODEL_NAME - see
apply_video_model_config_from_yaml). There is no separate settings entry and
no built-in default endpoint/model: whatever's configured there is what
these tools use, and all three must be set. Note this is the SAME slot
video_understanding reads; pointing it at a generation-only model (e.g.
OpenRouter's bytedance/seedance-2.0-fast) will break video understanding,
since that tool expects a chat-completions-style video-analysis model, not a
/videos generation endpoint.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

import httpx
from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify
from jiuwenswarm.common.utils import get_agent_workspace_dir

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 10
_MAX_POLL_SECONDS = 120
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}


def _get_video_gen_api_credentials() -> tuple[str, str, str]:
    """Resolve video-generation credentials from the "Video Model" config
    panel slot only (VIDEO_API_KEY/VIDEO_API_BASE/VIDEO_MODEL_NAME - see
    apply_video_model_config_from_yaml). No fallback env vars, no built-in
    default endpoint or model - an empty string means unconfigured.
    """
    api_key = os.environ.get("VIDEO_API_KEY", "").strip()
    api_base = os.environ.get("VIDEO_API_BASE", "").strip()
    model = os.environ.get("VIDEO_MODEL_NAME", "").strip()
    return api_key, api_base, model


def _resolve_frame_reference(path_or_url: str) -> tuple[str | None, str | None]:
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
        return None, f"[ERROR]: first_frame_path {value!r} is not a URL/data URI and no such file exists."
    mime, _ = mimetypes.guess_type(str(path))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}", None


def _resolve_save_path(save_dir: str | None, filename: str) -> Path:
    root = Path(save_dir).expanduser() if save_dir else (get_agent_workspace_dir() / "generated_videos")
    root.mkdir(parents=True, exist_ok=True)
    return root / filename


async def _download_video(
    client: httpx.AsyncClient, headers: dict[str, str], api_base: str, job_id: str, save_dir: str | None
) -> str:
    try:
        content = await client.get(
            f"{api_base}/videos/{job_id}/content", params={"index": 0}, headers=headers
        )
    except httpx.HTTPError as exc:
        return f"[ERROR]: downloading video job {job_id} failed: {exc!r}"
    if content.status_code != 200:
        return f"[ERROR]: video job {job_id} completed but downloading content failed: {content.status_code}"

    target = _resolve_save_path(save_dir, f"video_{job_id}.mp4")
    target.write_bytes(content.content)
    return (
        "Video generated successfully!\n"
        f"Saved to: {target}\n"
        f"(job {job_id} - the remote source URL expires 24h after completion, so this "
        "local file is now the durable copy.)"
    )


async def _poll_job(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    job_id: str,
    polling_url: str,
    status: str,
    job: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], int]:
    # Seeded with the caller's already-known job state (e.g. the submit
    # response) rather than {}, so that a job already terminal on submit -
    # no polling iteration ever runs - doesn't lose its "error" field to an
    # empty dict here.
    job = dict(job) if job else {}
    elapsed = 0
    while status not in _TERMINAL_STATUSES and elapsed < _MAX_POLL_SECONDS:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS
        poll = await client.get(polling_url, headers=headers)
        if poll.status_code != 200:
            raise RuntimeError(f"polling video job {job_id} failed: {poll.status_code} {poll.text}")
        job = poll.json()
        status = job.get("status", "pending")
    return status, job, elapsed


@tool(
    name="generate_video",
    description=(
        "Generate a video clip from a text prompt (and optionally a first-frame image) "
        "using AI video generation models. Use this tool when the user wants to create "
        "or generate a video based on a text description. Video generation can take "
        "several minutes; if this tool returns a job_id instead of a finished video, call "
        "check_video_status with that job_id to keep waiting for it instead of resubmitting."
    ),
)
async def generate_video(
    prompt: str,
    aspect_ratio: str = "16:9",
    resolution: str = "480p",
    duration_seconds: int = 5,
    first_frame_path: str | None = None,
    generate_audio: bool = False,
    save_dir: str | None = None,
) -> str:
    """
    Generate a video from a text prompt.

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

    Returns:
        Path to the generated video file, or a job_id + status message if the
        job is still running after the bounded poll window.
    """
    api_key, api_base, model = _get_video_gen_api_credentials()
    if not (api_key and api_base and model):
        return (
            "[ERROR]: video generation is not configured - set the Video Model "
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
            job = submit.json()
            job_id = job.get("id")
            if not job_id:
                return f"[ERROR]: video generation submit returned no job id: {job}"
            polling_url = job.get("polling_url") or f"{api_base}/videos/{job_id}"
            status = job.get("status", "pending")

            status, job, elapsed = await _poll_job(client, headers, job_id, polling_url, status, job)

            if status not in _TERMINAL_STATUSES:
                return (
                    f"Video job {job_id} submitted and still {status} after {elapsed}s - generation can "
                    f"take several minutes. Call check_video_status with job_id={job_id} to check progress "
                    "and download it once ready."
                )
            if status != "completed":
                return (
                    f"[ERROR]: video job {job_id} ended with status {status}: "
                    f"{job.get('error', 'no error detail provided')}"
                )

            return await _download_video(client, headers, api_base, job_id, save_dir)
    except httpx.HTTPError as exc:
        return f"[ERROR]: video generation request failed: {exc!r}"


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
    api_key, api_base, _ = _get_video_gen_api_credentials()
    if not (api_key and api_base):
        return (
            "[ERROR]: video generation is not configured - set the Video Model "
            "API key, API URL, and model name in configuration settings."
        )
    job_id = (job_id or "").strip()
    if not job_id:
        return "[ERROR]: job_id is required."

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=30, verify=get_requests_verify()) as client:
            poll = await client.get(f"{api_base}/videos/{job_id}", headers=headers)
            if poll.status_code != 200:
                return f"[ERROR]: checking video job {job_id} failed: {poll.status_code} {poll.text}"
            job = poll.json()
            status = job.get("status", "unknown")
            if status == "completed":
                return await _download_video(client, headers, api_base, job_id, save_dir)
            if status in _TERMINAL_STATUSES:
                return (
                    f"[ERROR]: video job {job_id} ended with status {status}: "
                    f"{job.get('error', 'no error detail provided')}"
                )
            return f"Video job {job_id} is still {status}."
    except httpx.HTTPError as exc:
        return f"[ERROR]: checking video job {job_id} failed: {exc!r}"
