# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Text-to-image and reference-image generation tool via an OpenRouter-style
chat-completions image modality (e.g. google/gemini-3.1-flash-image).

Unlike video generation (video_gen_tools.py), this call is synchronous - the
model returns the generated image(s) directly in the chat-completions
response (as data: URIs or remote URLs), with no submit-then-poll job to
track.

Credentials come from a dedicated "Visual processing" config panel slot
(VISUAL_GEN_API_KEY/VISUAL_GEN_API_BASE/VISUAL_GEN_MODEL_NAME/
VISUAL_GEN_PROVIDER/VISUAL_GEN_PROTOCOL) - independent of both:
- visual_question_answering's VISION_* slot (image understanding, a
  different capability), and
- image_tools.py's generate_image (IMAGE_GEN_* slot), whose implementation
  is hard-locked to a DashScope-only code path (see
  OpenAIModelClient._require_dashscope_media_profile in openjiuwen) and
  cannot serve an OpenRouter/Gemini-style model no matter what provider or
  key is configured there.

There is no built-in default endpoint/model: whatever's configured in the
dedicated slot above is what this tool uses, and api_key/api_base/model
must all be set (a self-deployed vLLM-Omni endpoint needs only the API URL).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import io
import json
import logging
import os
import re
import secrets
import socket
import time
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import url2pathname

import httpx
from openjiuwen.core.foundation.tool import tool
from PIL import Image, UnidentifiedImageError

from jiuwenswarm.agents.harness.common.tools import gen_toolkits
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify
from jiuwenswarm.common.utils import get_agent_workspace_dir

logger = logging.getLogger(__name__)

_MAX_REFERENCE_IMAGES = 8
_MAX_REFERENCE_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_REFERENCE_TOTAL_BYTES = 40 * 1024 * 1024
_MAX_GENERATED_IMAGES = 8
_MAX_GENERATED_IMAGE_BYTES = 50 * 1024 * 1024
_MAX_IMAGE_PIXELS = 80_000_000
_MAX_DOWNLOAD_REDIRECTS = 3
_INTERNAL_HOST_SUFFIXES = (".internal", ".local", ".lan", ".home.arpa", ".localhost")
_METADATA_HOSTS = {"metadata", "metadata.google.internal"}
_RASTER_FORMATS = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
    "GIF": ("image/gif", "gif"),
}
_DATA_IMAGE_URI = re.compile(
    r"\Adata:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]*={0,2})\Z",
    re.IGNORECASE,
)


def visual_gen_enabled() -> bool:
    """Whether the "Visual processing" switch in configuration settings is on.

    Mirrors video_gen_tools.video_gen_enabled's env-var gate: VISUAL_GEN_ENABLED
    is set from that switch and checked independently of whether the Visual
    processing config itself is complete, so a user can leave valid
    credentials in place while still turning generation off.
    """
    return str(os.environ.get("VISUAL_GEN_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on")


def _get_visual_gen_api_credentials() -> tuple[str, str, str]:
    """Resolve image-generation credentials from the dedicated "Visual
    processing" config panel slot only (VISUAL_GEN_API_KEY/VISUAL_GEN_API_BASE/
    VISUAL_GEN_MODEL_NAME). No fallback env vars, no built-in default endpoint
    or model - an empty string means unconfigured.
    """
    api_key = os.environ.get("VISUAL_GEN_API_KEY", "").strip()
    api_base = os.environ.get("VISUAL_GEN_API_BASE", "").strip()
    model = os.environ.get("VISUAL_GEN_MODEL_NAME", "").strip()
    return api_key, api_base, model


def visual_gen_configured() -> bool:
    """Whether the Visual processing config (key/base/model) is complete.

    Public entry point for callers outside this module (interface_deep.py,
    swarm/providers/tools.py) that only need a yes/no gate and shouldn't
    depend on _get_visual_gen_api_credentials' private tuple shape.
    """
    return _credentials_complete(*_get_visual_gen_api_credentials())


def _credentials_complete(api_key: str, api_base: str, model: str) -> bool:
    if gen_toolkits.detect_backend("VISUAL_GEN_PROTOCOL", api_base) in gen_toolkits.KEYLESS_BACKENDS:
        return bool(api_base)
    return bool(api_key and api_base and model)


def _resolve_save_path(save_dir: str | None, filename: str) -> Path:
    root = Path(save_dir).expanduser() if save_dir else (get_agent_workspace_dir() / "generated_images")
    root.mkdir(parents=True, exist_ok=True)
    return root / filename


def _extension_for_mime(mime: str) -> str:
    normalized = str(mime or "").strip().lower()
    for _format, (known_mime, extension) in _RASTER_FORMATS.items():
        if normalized == known_mime:
            return extension
    return "jpg"


def _inspect_raster(data: bytes, *, label: str, max_bytes: int) -> tuple[str, str]:
    """Return the decoded raster MIME and extension, rejecting fake/unsafe images."""
    if not data:
        raise ValueError(f"{label} is empty")
    if len(data) > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes // (1024 * 1024)} MiB limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                if image_format not in _RASTER_FORMATS:
                    raise ValueError(
                        f"{label} uses unsupported raster format {image_format or 'unknown'}"
                    )
                if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
                    raise ValueError(f"{label} has unsafe dimensions {width}x{height}")
                image.verify()
            # ``verify`` checks the container; a second decode ensures pixels can
            # actually be read instead of accepting a header-only/truncated file.
            with Image.open(io.BytesIO(data)) as decoded:
                decoded.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError(f"{label} exceeds the safe decoded image size") from None
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError(f"{label} is not a readable raster image: {exc}") from None
    return _RASTER_FORMATS[image_format]


def _decode_data_image_uri(
    value: str, *, label: str, max_bytes: int
) -> tuple[bytes, str, str]:
    match = _DATA_IMAGE_URI.fullmatch(value)
    if match is None:
        raise ValueError(
            f"{label} must be a strict base64 data URI for PNG, JPEG, WEBP, or GIF"
        )
    claimed_mime, encoded = match.groups()
    # Reject an oversized payload before allocating its decoded representation.
    if len(encoded) > ((max_bytes + 2) // 3) * 4:
        raise ValueError(f"{label} exceeds the {max_bytes // (1024 * 1024)} MiB limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} contains invalid base64 data: {exc}") from None
    actual_mime, extension = _inspect_raster(data, label=label, max_bytes=max_bytes)
    if claimed_mime.lower() != actual_mime:
        raise ValueError(
            f"{label} declares {claimed_mime.lower()} but decodes as {actual_mime}"
        )
    return data, actual_mime, extension


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_reference_image(
    path_or_uri: str, index: int, workspace_root: Path
) -> tuple[str | None, int, str | None]:
    """Resolve one reference image to a provider-readable URI.

    References may be strict image data URIs or local files inside the active
    agent workspace. Network references are intentionally rejected: allowing a
    model-controlled path/URL here would bypass the workspace boundary.
    """
    value = str(path_or_uri or "").strip()
    argument = f"reference_image_paths[{index}]"
    if not value:
        return None, 0, f"[ERROR]: {argument} is empty."
    if value.startswith("data:"):
        try:
            data, mime, _extension = _decode_data_image_uri(
                value, label=argument, max_bytes=_MAX_REFERENCE_IMAGE_BYTES
            )
        except ValueError as exc:
            return None, 0, f"[ERROR]: {exc}."
        return _data_uri(data, mime), len(data), None

    parsed = urlparse(value)
    if parsed.scheme.lower() in ("http", "https"):
        return None, 0, (
            f"[ERROR]: {argument} must be a local image inside the agent workspace; "
            "remote URLs are not allowed."
        )

    if value.startswith("file://"):
        if parsed.query or parsed.fragment:
            return None, 0, f"[ERROR]: {argument} has an invalid file URI."
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            return None, 0, f"[ERROR]: {argument} uses a remote file URI, which is not allowed."
        decoded_path = url2pathname(unquote(parsed.path))
        path = Path(decoded_path)
    else:
        if "://" in value:
            return None, 0, f"[ERROR]: {argument} uses an unsupported URI scheme."
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = workspace_root / path

    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, 0, f"[ERROR]: {argument} {value!r} does not exist."
    if not _is_within(resolved, workspace_root):
        return None, 0, f"[ERROR]: {argument} is outside the agent workspace."
    if not resolved.is_file():
        return None, 0, f"[ERROR]: {argument} is not a regular file."
    try:
        file_size = resolved.stat().st_size
        if file_size > _MAX_REFERENCE_IMAGE_BYTES:
            return None, 0, (
                f"[ERROR]: {argument} exceeds the "
                f"{_MAX_REFERENCE_IMAGE_BYTES // (1024 * 1024)} MiB limit."
            )
        data = resolved.read_bytes()
    except OSError as exc:
        return None, 0, f"[ERROR]: failed to read {argument} {value!r}: {exc!r}"
    try:
        mime, _extension = _inspect_raster(
            data, label=argument, max_bytes=_MAX_REFERENCE_IMAGE_BYTES
        )
    except ValueError as exc:
        return None, 0, f"[ERROR]: {exc}."
    return _data_uri(data, mime), len(data), None


def _resolve_reference_images(paths_or_uris: list[str] | None) -> tuple[tuple[str, ...], str | None]:
    if paths_or_uris is None:
        return (), None
    if not isinstance(paths_or_uris, list):
        return (), "[ERROR]: reference_image_paths must be a list."
    if len(paths_or_uris) > _MAX_REFERENCE_IMAGES:
        return (), f"[ERROR]: at most {_MAX_REFERENCE_IMAGES} reference images are allowed."
    try:
        workspace_root = get_agent_workspace_dir().expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return (), f"[ERROR]: agent workspace is unavailable: {exc!r}"
    references: list[str] = []
    total_bytes = 0
    for index, value in enumerate(paths_or_uris or []):
        uri, byte_count, error = _resolve_reference_image(value, index, workspace_root)
        if error:
            return (), error
        if uri and uri not in references:
            references.append(uri)
            total_bytes += byte_count
            if total_bytes > _MAX_REFERENCE_TOTAL_BYTES:
                return (), (
                    "[ERROR]: reference images exceed the combined "
                    f"{_MAX_REFERENCE_TOTAL_BYTES // (1024 * 1024)} MiB limit."
                )
    return tuple(references), None


def _provider_image_value(image: Any) -> str:
    if not isinstance(image, dict):
        return ""
    image_url = image.get("image_url") or {}
    nested = image_url.get("url") if isinstance(image_url, dict) else ""
    return str(nested or image.get("url") or "").strip()


def _resolve_host_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve a provider host for SSRF review (kept separate for deterministic tests)."""
    addresses: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        address = str(sockaddr[0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


async def _assert_public_https_url(url: str, *, label: str) -> None:
    """Reject URLs that could make a provider response act as an SSRF primitive."""
    try:
        parsed = urlparse(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid HTTPS URL: {exc}") from None
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{label} must be a credential-free HTTPS URL")
    host = parsed.hostname.rstrip(".").lower()
    if host in _METADATA_HOSTS or host == "localhost" or host.endswith(_INTERNAL_HOST_SUFFIXES):
        raise ValueError(f"{label} points to an internal host, which is not allowed")
    try:
        # Provider image URLs are expected to be ordinary CDN hostnames. Reject
        # every IP literal, including a currently public one, so alternate IP
        # spellings cannot bypass hostname and redirect checks.
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        raise ValueError(f"{label} uses an IP-literal host, which is not allowed")
    if "." not in host:
        raise ValueError(f"{label} points to a single-label host, which is not allowed")
    try:
        addresses = await asyncio.to_thread(_resolve_host_addresses, host, port)
    except OSError as exc:
        raise ValueError(f"{label} host could not be resolved: {exc}") from None
    if not addresses:
        raise ValueError(f"{label} host resolved to no addresses")
    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError:
            raise ValueError(f"{label} host resolved to an invalid address") from None
        if not resolved.is_global:
            raise ValueError(
                f"{label} host resolved to a private, loopback, link-local, or reserved address"
            )


async def _download_https_image(
    client: httpx.AsyncClient, url: str, *, label: str
) -> tuple[bytes, str, str]:
    current = url
    for redirect_index in range(_MAX_DOWNLOAD_REDIRECTS + 1):
        await _assert_public_https_url(current, label=label)
        async with client.stream("GET", current, follow_redirects=False) as response:
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise ValueError(f"{label} returned a redirect without a location")
                if redirect_index >= _MAX_DOWNLOAD_REDIRECTS:
                    raise ValueError(f"{label} exceeded the redirect limit")
                current = urljoin(current, location)
                continue
            if response.status_code != 200:
                raise ValueError(f"{label} download failed with HTTP {response.status_code}")
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    declared_length = None
                if declared_length is not None and declared_length > _MAX_GENERATED_IMAGE_BYTES:
                    raise ValueError(
                        f"{label} exceeds the "
                        f"{_MAX_GENERATED_IMAGE_BYTES // (1024 * 1024)} MiB limit"
                    )
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > _MAX_GENERATED_IMAGE_BYTES:
                    raise ValueError(
                        f"{label} exceeds the "
                        f"{_MAX_GENERATED_IMAGE_BYTES // (1024 * 1024)} MiB limit"
                    )
                chunks.append(chunk)
        data = b"".join(chunks)
        mime, extension = _inspect_raster(
            data, label=label, max_bytes=_MAX_GENERATED_IMAGE_BYTES
        )
        return data, mime, extension
    raise ValueError(f"{label} exceeded the redirect limit")


async def _materialize_provider_images(
    images: Any, save_dir: str | None
) -> tuple[list[str] | None, str | None]:
    if not isinstance(images, list) or not images:
        return None, "[ERROR]: provider returned no image entries."
    if len(images) > _MAX_GENERATED_IMAGES:
        return None, f"[ERROR]: provider returned more than {_MAX_GENERATED_IMAGES} images."

    decoded: list[tuple[bytes, str]] = []
    try:
        async with httpx.AsyncClient(timeout=120, verify=get_requests_verify()) as client:
            for index, image in enumerate(images):
                label = f"provider image[{index}]"
                value = _provider_image_value(image)
                if not value:
                    return None, f"[ERROR]: {label} has no URL or data URI."
                if value.startswith("data:"):
                    data, _mime, extension = _decode_data_image_uri(
                        value, label=label, max_bytes=_MAX_GENERATED_IMAGE_BYTES
                    )
                else:
                    data, _mime, extension = await _download_https_image(
                        client, value, label=label
                    )
                decoded.append((data, extension))
    except (ValueError, httpx.HTTPError) as exc:
        return None, f"[ERROR]: generated image validation failed: {exc}."

    saved: list[Path] = []
    try:
        nonce = secrets.token_hex(4)
        stamp = time.time_ns()
        for index, (data, extension) in enumerate(decoded):
            target = _resolve_save_path(
                save_dir, f"image_{stamp}_{index}_{nonce}.{extension}"
            )
            target.write_bytes(data)
            saved.append(target)
    except OSError as exc:
        for path in saved:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("failed to clean partial generated image %s", path)
        return None, (
            "[ERROR]: failed to save generated image to "
            f"{save_dir or 'agent workspace'}: {exc!r}"
        )
    return [str(path) for path in saved], None


def _validate_native_provider_result(result: str, save_dir: str | None) -> str:
    """Reject native-provider success strings that do not point to real rasters."""
    if result.startswith("[ERROR]:"):
        return result
    raw_paths: list[str] = []
    json_values = [
        line[len("Saved paths JSON:"):].strip()
        for line in result.splitlines()
        if line.startswith("Saved paths JSON:")
    ]
    if json_values:
        if len(json_values) != 1:
            return "[ERROR]: image provider returned ambiguous saved image paths."
        try:
            decoded_paths = json.loads(json_values[0])
        except json.JSONDecodeError:
            return "[ERROR]: image provider returned invalid saved image path JSON."
        if not isinstance(decoded_paths, list) or not all(
            isinstance(path, str) and path for path in decoded_paths
        ):
            return "[ERROR]: image provider returned invalid saved image path JSON."
        raw_paths.extend(decoded_paths)
    else:
        saved_values = [
            line[len("Saved to:"):].strip()
            for line in result.splitlines()
            if line.startswith("Saved to:") and line[len("Saved to:"):].strip()
        ]
        if not saved_values:
            return "[ERROR]: image provider did not return a local saved image path."
        for value in saved_values:
            # Compatibility with callers that still emit the historical
            # comma-delimited line. Treat an existing whole value as one path
            # first so commas inside directory names remain valid.
            if Path(value).expanduser().exists():
                raw_paths.append(value)
            else:
                raw_paths.extend(
                    part.strip() for part in value.split(", ") if part.strip()
                )
    if not raw_paths or len(raw_paths) > _MAX_GENERATED_IMAGES:
        return "[ERROR]: image provider returned an invalid number of saved image paths."
    expected_root = (
        Path(save_dir).expanduser() if save_dir else get_agent_workspace_dir() / "generated_images"
    ).resolve()
    for index, raw_path in enumerate(raw_paths):
        label = f"provider image[{index}]"
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return f"[ERROR]: {label} was not saved to a readable local file."
        if not _is_within(path, expected_root):
            return f"[ERROR]: {label} was saved outside the requested output directory."
        try:
            size = path.stat().st_size
            if size > _MAX_GENERATED_IMAGE_BYTES:
                return f"[ERROR]: {label} exceeds the generated image size limit."
            data = path.read_bytes()
            _inspect_raster(data, label=label, max_bytes=_MAX_GENERATED_IMAGE_BYTES)
        except (OSError, ValueError) as exc:
            return f"[ERROR]: generated image validation failed: {exc}."
    return result


@tool(
    name="generate_visual",
    description=(
        "Generate an image from a text prompt and optional reference images using AI image generation models. "
        "Use this tool when the user wants to create or generate an image based "
        "on a description or edit/transform supplied images. Returns the path to the saved generated image file."
    ),
)
async def generate_visual(
    prompt: str,
    aspect_ratio: str = "16:9",
    resolution: str = "512",
    save_dir: str | None = None,
    reference_image_paths: list[str] | None = None,
) -> str:
    """
    Generate an image from a text prompt.

    Args:
        prompt: Text description of the image to generate.
        aspect_ratio: e.g. "16:9", "9:16", "1:1".
        resolution: e.g. "512", "1024" (short-edge pixel size).
        save_dir: Optional directory to save the image (defaults to the agent
            workspace's generated_images/ folder).
        reference_image_paths: Optional local paths/``file://`` URIs inside the
            active agent workspace, or strict image data URIs, whose visible
            content should be preserved or transformed.

    Returns:
        Path to the generated image file, or an error message.
    """
    api_key, api_base, model = _get_visual_gen_api_credentials()
    if not _credentials_complete(api_key, api_base, model):
        return (
            "[ERROR]: image generation is not configured - set the Visual processing "
            "API key, API URL, and model name in configuration settings."
        )
    prompt = (prompt or "").strip()
    if not prompt:
        return "[ERROR]: prompt is required."
    reference_uris, reference_error = _resolve_reference_images(reference_image_paths)
    if reference_error:
        return reference_error

    # MiniMax (own /v1/image_generation), BytePlus ModelArk (Seedream), DashScope
    # (Qwen-Image) and vLLM-Omni are not OpenAI-compatible, so they have their own backends.
    backend = gen_toolkits.detect_backend("VISUAL_GEN_PROTOCOL", api_base)
    if backend:
        target = gen_toolkits.GenerationTarget(backend, api_key, api_base, model)
        options = gen_toolkits.ImageOptions(reference_image_uris=reference_uris)
        result = await gen_toolkits.generate_image(
            target, prompt, aspect_ratio, save_dir, options
        )
        return _validate_native_provider_result(result, save_dir)

    # Not every provider/model honors aspect_ratio/resolution as separate
    # request-body fields, so the hint is also folded into the prompt text
    # itself as a best-effort fallback the model can act on directly.
    full_prompt = f"{prompt}\n\n(Aspect ratio: {aspect_ratio}, resolution: {resolution}px)"
    message_content: str | list[dict[str, Any]] = full_prompt
    if reference_uris:
        message_content = [{"type": "text", "text": full_prompt}]
        message_content.extend(
            {"type": "image_url", "image_url": {"url": uri}}
            for uri in reference_uris
        )
    body: dict[str, Any] = {
        "model": model,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": message_content}],
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    logger.info(
        "[generate_visual] using model: %s (api_base: %s, aspect_ratio: %s, resolution: %s)",
        model, api_base, aspect_ratio, resolution,
    )

    try:
        async with httpx.AsyncClient(timeout=120, verify=get_requests_verify()) as client:
            resp = await client.post(f"{api_base}/chat/completions", headers=headers, json=body)
            if resp.status_code != 200:
                return f"[ERROR]: image generation request failed: {resp.status_code} {resp.text}"
            try:
                data = resp.json()
            except ValueError as exc:
                return f"[ERROR]: image generation request returned invalid JSON: {exc!r}"
    except httpx.HTTPError as exc:
        return f"[ERROR]: image generation request failed: {exc!r}"

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return f"[ERROR]: unexpected response shape from provider: {data}"

    images = message.get("images") or []
    if not images:
        content = message.get("content", "")
        return f"[ERROR]: no images returned. Model response: {content}"

    saved_paths, save_error = await _materialize_provider_images(images, save_dir)
    if save_error:
        return save_error
    assert saved_paths is not None
    return "Image generated successfully!\nSaved to: " + ", ".join(saved_paths)
