# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for jiuwenswarm.agents.harness.common.tools.visual_gen_tools."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from PIL import Image

from jiuwenswarm.agents.harness.common.tools import visual_gen_tools as vg

# The @tool decorator wraps the function in a LocalFunction; ``._func`` is the
# original plain async function it wraps, callable directly with the same
# keyword arguments - see LocalFunction.__init__ (openjiuwen). Testing through
# this avoids bootstrapping the whole tool-invocation/callback framework that
# the wrapper's own __call__ wires up, which is out of scope for a unit test.
generate_visual = vg.generate_visual._func

_TEST_API_BASE = "https://visual-model.example/api/v1"
_TEST_MODEL = "example/visual-gen-model"


def _clear_visual_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "VISUAL_GEN_API_KEY", "VISUAL_GEN_API_BASE", "VISUAL_GEN_MODEL_NAME", "VISUAL_GEN_ENABLED",
        "VISUAL_GEN_PROTOCOL",
    ):
        monkeypatch.delenv(name, raising=False)


def _set_visual_gen_config(monkeypatch: pytest.MonkeyPatch, *, api_key: str = "sk-test") -> None:
    """Configure the dedicated "Visual processing" panel slot the tool reads."""
    monkeypatch.setenv("VISUAL_GEN_API_KEY", api_key)
    monkeypatch.setenv("VISUAL_GEN_API_BASE", _TEST_API_BASE)
    monkeypatch.setenv("VISUAL_GEN_MODEL_NAME", _TEST_MODEL)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch):
    """Every test starts with a clean slate for visual-gen env vars."""
    _clear_visual_env(monkeypatch)
    yield


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Force every httpx.AsyncClient() constructed by the module under test to
    route through a MockTransport, so no real network call is ever made."""
    real_async_client = httpx.AsyncClient

    class _PatchedAsyncClient(real_async_client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _PatchedAsyncClient)


def _data_uri(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _raster_bytes(image_format: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (3, 2), color=(35, 90, 160)).save(output, format=image_format)
    return output.getvalue()


# ---------------------------------------------------------------------------
# visual_gen_enabled - the "Visual processing" settings switch
# ---------------------------------------------------------------------------


def test_visual_gen_enabled_false_when_unset():
    assert vg.visual_gen_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
def test_visual_gen_enabled_true_for_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("VISUAL_GEN_ENABLED", value)
    assert vg.visual_gen_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", "", "garbage"])
def test_visual_gen_enabled_false_for_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("VISUAL_GEN_ENABLED", value)
    assert vg.visual_gen_enabled() is False


# ---------------------------------------------------------------------------
# _get_visual_gen_api_credentials
# ---------------------------------------------------------------------------


def test_credentials_empty_when_unset():
    api_key, api_base, model = vg._get_visual_gen_api_credentials()
    assert (api_key, api_base, model) == ("", "", "")


def test_credentials_read_from_dedicated_slot(monkeypatch: pytest.MonkeyPatch):
    _set_visual_gen_config(monkeypatch, api_key="sk-from-visual-panel")

    api_key, api_base, model = vg._get_visual_gen_api_credentials()

    assert api_key == "sk-from-visual-panel"
    assert api_base == _TEST_API_BASE
    assert model == _TEST_MODEL


def test_credentials_partial_config_leaves_missing_fields_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VISUAL_GEN_API_KEY", "sk-only-key-set")

    api_key, api_base, model = vg._get_visual_gen_api_credentials()

    assert api_key == "sk-only-key-set"
    assert api_base == ""
    assert model == ""


# ---------------------------------------------------------------------------
# generate_visual - happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_visual_success_saves_data_uri_image(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _set_visual_gen_config(monkeypatch)
    png_bytes = _raster_bytes("PNG")
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        assert request.url.path == "/api/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "Here is your image.",
                            "images": [{"image_url": {"url": _data_uri("image/png", png_bytes)}}],
                        }
                    }
                ]
            },
        )

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(
        prompt="a golden retriever puppy in a meadow",
        aspect_ratio="1:1",
        resolution="1024",
        save_dir=str(tmp_path),
    )

    assert "Image generated successfully!" in result
    saved_files = list(tmp_path.glob("*.png"))
    assert len(saved_files) == 1
    assert saved_files[0].read_bytes() == png_bytes
    assert str(saved_files[0]) in result

    submitted_body = json.loads(requests_seen[0].content)
    assert submitted_body["model"] == _TEST_MODEL
    assert submitted_body["modalities"] == ["image", "text"]
    assert "a golden retriever puppy in a meadow" in submitted_body["messages"][0]["content"]
    assert submitted_body["aspect_ratio"] == "1:1"
    assert submitted_body["resolution"] == "1024"
    assert requests_seen[0].headers["authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_generate_visual_save_failure_returns_error_not_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A disk-full/permission-denied write must surface as a clean [ERROR]
    string, not an unhandled OSError - save_dir is agent-controlled input."""
    _set_visual_gen_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [
                                {"image_url": {"url": _data_uri("image/png", _raster_bytes("PNG"))}}
                            ]
                        }
                    }
                ]
            },
        )

    _patch_async_client(monkeypatch, handler)

    def _raise_disk_full(self, data):
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_bytes", _raise_disk_full)

    result = await generate_visual(prompt="a cat", save_dir=str(tmp_path))

    assert result.startswith("[ERROR]: failed to save generated image")
    assert "No space left on device" in result


@pytest.mark.asyncio
async def test_generate_visual_uses_defaults_when_unspecified(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _set_visual_gen_config(monkeypatch)
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [
                                {"image_url": {"url": _data_uri("image/png", _raster_bytes("PNG"))}}
                            ],
                        }
                    }
                ]
            },
        )

    _patch_async_client(monkeypatch, handler)

    await generate_visual(prompt="a sunset over mountains", save_dir=str(tmp_path))

    submitted_body = json.loads(requests_seen[0].content)
    assert submitted_body["aspect_ratio"] == "16:9"
    assert submitted_body["resolution"] == "512"


@pytest.mark.asyncio
async def test_generate_visual_downloads_remote_https_url_to_valid_local_raster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)
    monkeypatch.setattr(vg, "_resolve_host_addresses", lambda host, port: ("93.184.216.34",))
    png_bytes = _raster_bytes("PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert str(request.url) == "https://cdn.example.com/generated.png"
            return httpx.Response(200, headers={"content-type": "image/png"}, content=png_bytes)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [{"image_url": {"url": "https://cdn.example.com/generated.png"}}],
                        }
                    }
                ]
            },
        )

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="a rainbow", save_dir=str(tmp_path))

    saved = list(tmp_path.glob("*.png"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == png_bytes
    assert str(saved[0]) in result
    assert "https://cdn.example.com/generated.png" not in result


@pytest.mark.asyncio
async def test_generate_visual_multiple_images_all_saved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _set_visual_gen_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [
                                {"image_url": {"url": _data_uri("image/png", _raster_bytes("PNG"))}},
                                {"image_url": {"url": _data_uri("image/webp", _raster_bytes("WEBP"))}},
                            ],
                        }
                    }
                ]
            },
        )

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="two cats", save_dir=str(tmp_path))

    saved = sorted(tmp_path.glob("*"))
    assert len(saved) == 2
    assert {p.suffix for p in saved} == {".png", ".webp"}
    for path in saved:
        assert str(path) in result


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["modelark", "dashscope", "vllm-omni"])
async def test_generate_visual_passes_local_references_to_native_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
):
    _set_visual_gen_config(monkeypatch)
    monkeypatch.setenv("VISUAL_GEN_PROTOCOL", backend)
    monkeypatch.setattr(vg, "get_agent_workspace_dir", lambda: tmp_path)
    reference = tmp_path / "source.png"
    reference.write_bytes(_raster_bytes("PNG"))
    captured: dict[str, Any] = {}

    async def fake_generate_image(target, prompt, aspect_ratio, save_dir, options):
        captured.update(
            target=target,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            save_dir=save_dir,
            options=options,
        )
        output = Path(save_dir) / "result.png"
        output.write_bytes(_raster_bytes("PNG"))
        return f"Image generated successfully!\nSaved to: {output}"

    monkeypatch.setattr(vg.gen_toolkits, "generate_image", fake_generate_image)

    result = await generate_visual(
        prompt="keep this room geometry and improve the storage",
        aspect_ratio="4:3",
        save_dir=str(tmp_path),
        reference_image_paths=[str(reference)],
    )

    assert result.startswith("Image generated successfully!")
    assert captured["target"].backend == backend
    assert captured["prompt"] == "keep this room geometry and improve the storage"
    assert captured["aspect_ratio"] == "4:3"
    references = captured["options"].reference_image_uris
    assert len(references) == 1
    assert references[0].startswith("data:image/png;base64,")
    assert base64.b64decode(references[0].partition(",")[2]) == reference.read_bytes()


@pytest.mark.asyncio
async def test_generate_visual_validates_multiple_native_paths_with_commas_in_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)
    monkeypatch.setenv("VISUAL_GEN_PROTOCOL", "modelark")
    output_dir = tmp_path / "output, with comma"
    output_dir.mkdir()

    async def fake_generate_image(target, prompt, aspect_ratio, save_dir, options):
        first = Path(save_dir) / "first.png"
        second = Path(save_dir) / "second.webp"
        first.write_bytes(_raster_bytes("PNG"))
        second.write_bytes(_raster_bytes("WEBP"))
        return (
            "Image generated successfully!\n"
            f"Saved to: {first}\n"
            f"Saved to: {second}"
        )

    monkeypatch.setattr(vg.gen_toolkits, "generate_image", fake_generate_image)

    result = await generate_visual(prompt="draw", save_dir=str(output_dir))

    assert result.startswith("Image generated successfully!")
    assert result.count("Saved to:") == 2


@pytest.mark.asyncio
async def test_generate_visual_keeps_legacy_comma_delimited_native_paths_compatible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)
    monkeypatch.setenv("VISUAL_GEN_PROTOCOL", "modelark")

    async def fake_generate_image(target, prompt, aspect_ratio, save_dir, options):
        first = Path(save_dir) / "first.png"
        second = Path(save_dir) / "second.jpg"
        first.write_bytes(_raster_bytes("PNG"))
        second.write_bytes(_raster_bytes("JPEG"))
        return f"Image generated successfully!\nSaved to: {first}, {second}"

    monkeypatch.setattr(vg.gen_toolkits, "generate_image", fake_generate_image)

    result = await generate_visual(prompt="draw", save_dir=str(tmp_path))

    assert result.startswith("Image generated successfully!")


def test_native_result_prefers_unambiguous_saved_paths_json(tmp_path: Path) -> None:
    output_dir = tmp_path / "folder, with comma"
    output_dir.mkdir()
    first = output_dir / "first.png"
    second = output_dir / "second.webp"
    first.write_bytes(_raster_bytes("PNG"))
    second.write_bytes(_raster_bytes("WEBP"))
    result = (
        "Image generated successfully!\n"
        f"Saved to: {first}, {second}\n"
        f"Saved paths JSON: {json.dumps([str(first), str(second)])}"
    )

    assert vg._validate_native_provider_result(result, str(output_dir)) == result


def test_native_result_rejects_invalid_saved_paths_json(tmp_path: Path) -> None:
    result = "Image generated successfully!\nSaved paths JSON: not-json"

    assert vg._validate_native_provider_result(result, str(tmp_path)) == (
        "[ERROR]: image provider returned invalid saved image path JSON."
    )


@pytest.mark.asyncio
async def test_generate_visual_builds_openrouter_multimodal_content_for_safe_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)
    monkeypatch.setattr(vg, "get_agent_workspace_dir", lambda: tmp_path)
    file_reference = tmp_path / "room photo.png"
    file_reference.write_bytes(_raster_bytes("PNG"))
    existing_data_uri = _data_uri("image/webp", _raster_bytes("WEBP"))
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [
                                {"image_url": {"url": _data_uri("image/png", _raster_bytes("PNG"))}}
                            ]
                        }
                    }
                ]
            },
        )

    _patch_async_client(monkeypatch, handler)

    await generate_visual(
        prompt="preserve the uploaded room",
        save_dir=str(tmp_path / "output"),
        reference_image_paths=[
            file_reference.as_uri(),
            existing_data_uri,
        ],
    )

    content = json.loads(requests_seen[0].content)["messages"][0]["content"]
    assert content[0]["type"] == "text"
    urls = [part["image_url"]["url"] for part in content[1:]]
    assert urls[0].startswith("data:image/png;base64,")
    assert base64.b64decode(urls[0].partition(",")[2]) == file_reference.read_bytes()
    assert urls[1] == existing_data_uri


@pytest.mark.asyncio
async def test_generate_visual_missing_reference_returns_error_before_provider_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)
    provider_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_called
        provider_called = True
        return httpx.Response(500)

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(
        prompt="edit this image",
        reference_image_paths=[str(tmp_path / "missing.png")],
    )

    assert result.startswith("[ERROR]: reference_image_paths[0]")
    assert "does not exist" in result
    assert provider_called is False


@pytest.mark.asyncio
async def test_generate_visual_minimax_rejects_reference_images_explicitly(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_visual_gen_config(monkeypatch)
    monkeypatch.setenv("VISUAL_GEN_PROTOCOL", "minimax")

    result = await generate_visual(
        prompt="edit this image",
        reference_image_paths=[_data_uri("image/png", _raster_bytes("PNG"))],
    )

    assert result.startswith("[ERROR]: the configured MiniMax image backend")
    assert "does not support reference images" in result


@pytest.mark.asyncio
async def test_generate_visual_openrouter_reference_rejection_is_not_silently_downgraded(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_visual_gen_config(monkeypatch)
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(400, text="model does not support image inputs")

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(
        prompt="edit this image",
        reference_image_paths=[_data_uri("image/png", _raster_bytes("PNG"))],
    )

    content = json.loads(requests_seen[0].content)["messages"][0]["content"]
    assert any(part.get("type") == "image_url" for part in content)
    assert result == (
        "[ERROR]: image generation request failed: 400 model does not support image inputs"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference",
    [
        "http://localhost:8080/reference.png",
        "https://cdn.example.com/reference.png",
        "file://fileserver/share/reference.png",
    ],
)
async def test_generate_visual_rejects_remote_reference_locations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reference: str
):
    _set_visual_gen_config(monkeypatch)
    monkeypatch.setattr(vg, "get_agent_workspace_dir", lambda: tmp_path)

    result = await generate_visual(prompt="edit this image", reference_image_paths=[reference])

    assert result.startswith("[ERROR]: reference_image_paths[0]")
    assert "remote" in result


@pytest.mark.asyncio
async def test_generate_visual_rejects_reference_outside_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_raster_bytes("PNG"))
    monkeypatch.setattr(vg, "get_agent_workspace_dir", lambda: workspace)

    result = await generate_visual(prompt="edit this image", reference_image_paths=[str(outside)])

    assert result == "[ERROR]: reference_image_paths[0] is outside the agent workspace."


@pytest.mark.asyncio
async def test_generate_visual_rejects_fake_image_reference_even_with_png_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)
    fake = tmp_path / "fake.png"
    fake.write_text("not an image", encoding="utf-8")
    monkeypatch.setattr(vg, "get_agent_workspace_dir", lambda: tmp_path)

    result = await generate_visual(prompt="edit this image", reference_image_paths=[str(fake)])

    assert result.startswith("[ERROR]: reference_image_paths[0] is not a readable raster image")


@pytest.mark.asyncio
async def test_generate_visual_rejects_too_many_or_oversized_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)
    monkeypatch.setattr(vg, "get_agent_workspace_dir", lambda: tmp_path)
    valid_uri = _data_uri("image/png", _raster_bytes("PNG"))

    too_many = await generate_visual(
        prompt="edit", reference_image_paths=[valid_uri] * (vg._MAX_REFERENCE_IMAGES + 1)
    )
    assert too_many == f"[ERROR]: at most {vg._MAX_REFERENCE_IMAGES} reference images are allowed."

    image = tmp_path / "valid.png"
    image.write_bytes(_raster_bytes("PNG"))
    monkeypatch.setattr(vg, "_MAX_REFERENCE_IMAGE_BYTES", 8)
    oversized = await generate_visual(prompt="edit", reference_image_paths=[str(image)])
    assert "exceeds the 0 MiB limit" in oversized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data_uri, expected",
    [
        ("data:image/png;base64,%%%%", "strict base64 data URI"),
        (
            _data_uri("image/jpeg", _raster_bytes("PNG")),
            "declares image/jpeg but decodes as image/png",
        ),
        ("data:text/plain;base64,SGVsbG8=", "strict base64 data URI"),
    ],
)
async def test_generate_visual_strictly_validates_reference_data_uris(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data_uri: str, expected: str
):
    _set_visual_gen_config(monkeypatch)
    monkeypatch.setattr(vg, "get_agent_workspace_dir", lambda: tmp_path)

    result = await generate_visual(prompt="edit", reference_image_paths=[data_uri])

    assert result.startswith("[ERROR]:")
    assert expected in result


@pytest.mark.asyncio
async def test_generate_visual_rejects_fake_provider_image_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [
                                {"image_url": {"url": _data_uri("image/png", b"not-png")}}
                            ]
                        }
                    }
                ]
            },
        )

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="draw", save_dir=str(tmp_path))

    assert result.startswith("[ERROR]: generated image validation failed:")
    assert "not a readable raster image" in result
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_generate_visual_rejects_non_https_provider_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [
                                {"image_url": {"url": "http://cdn.example.com/generated.png"}}
                            ]
                        }
                    }
                ]
            },
        )

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="draw", save_dir=str(tmp_path))

    assert result.startswith("[ERROR]: generated image validation failed:")
    assert "credential-free HTTPS URL" in result
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/generated.png",
        "https://127.0.0.1/generated.png",
        "https://10.0.0.8/generated.png",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/generated.png",
        "https://93.184.216.34/generated.png",
        "https://renderer.internal/generated.png",
    ],
)
async def test_generate_visual_blocks_ssrf_provider_hosts_before_get(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str
):
    _set_visual_gen_config(monkeypatch)
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"images": [{"image_url": {"url": url}}]}}]},
        )

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="draw", save_dir=str(tmp_path))

    assert result.startswith("[ERROR]: generated image validation failed:")
    assert len(requests_seen) == 1
    assert requests_seen[0].method == "POST"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_generate_visual_blocks_hostname_that_resolves_to_private_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)
    monkeypatch.setattr(vg, "_resolve_host_addresses", lambda host, port: ("192.168.1.10",))
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [
                                {"image_url": {"url": "https://images.example.com/generated.png"}}
                            ]
                        }
                    }
                ]
            },
        )

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="draw", save_dir=str(tmp_path))

    assert "resolved to a private, loopback, link-local, or reserved address" in result
    assert len(requests_seen) == 1


@pytest.mark.asyncio
async def test_generate_visual_revalidates_redirect_and_blocks_private_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)
    monkeypatch.setattr(vg, "_resolve_host_addresses", lambda host, port: ("93.184.216.34",))
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "images": [
                                    {
                                        "image_url": {
                                            "url": "https://cdn.example.com/generated.png"
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        return httpx.Response(302, headers={"location": "https://127.0.0.1/secret"})

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="draw", save_dir=str(tmp_path))

    assert "IP-literal host" in result
    assert [request.method for request in requests_seen] == ["POST", "GET"]
    assert all(str(request.url) != "https://127.0.0.1/secret" for request in requests_seen)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_generate_visual_rejects_native_provider_fake_saved_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _set_visual_gen_config(monkeypatch)
    monkeypatch.setenv("VISUAL_GEN_PROTOCOL", "modelark")

    async def fake_generate_image(target, prompt, aspect_ratio, save_dir, options):
        output = Path(save_dir) / "fake.png"
        output.write_bytes(b"not an image")
        return f"Image generated successfully!\nSaved to: {output}"

    monkeypatch.setattr(vg.gen_toolkits, "generate_image", fake_generate_image)

    result = await generate_visual(prompt="draw", save_dir=str(tmp_path))

    assert result.startswith("[ERROR]: generated image validation failed:")
    assert "not a readable raster image" in result


# ---------------------------------------------------------------------------
# generate_visual - error / edge paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_visual_non_200_returns_error(monkeypatch: pytest.MonkeyPatch):
    _set_visual_gen_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request: invalid model")

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="a cat")

    assert result == "[ERROR]: image generation request failed: 400 bad request: invalid model"


@pytest.mark.asyncio
async def test_generate_visual_invalid_json_response_returns_error(monkeypatch: pytest.MonkeyPatch):
    _set_visual_gen_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="a cat")

    assert result.startswith("[ERROR]: image generation request returned invalid JSON:")


@pytest.mark.asyncio
async def test_generate_visual_unexpected_response_shape_returns_error(monkeypatch: pytest.MonkeyPatch):
    _set_visual_gen_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="a cat")

    assert result.startswith("[ERROR]: unexpected response shape from provider:")


@pytest.mark.asyncio
async def test_generate_visual_no_images_returned_error(monkeypatch: pytest.MonkeyPatch):
    _set_visual_gen_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "I cannot generate that image."}}]}
        )

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="a cat")

    assert result == "[ERROR]: no images returned. Model response: I cannot generate that image."


@pytest.mark.asyncio
async def test_generate_visual_http_error_is_caught(monkeypatch: pytest.MonkeyPatch):
    _set_visual_gen_config(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="a cat")

    assert result.startswith("[ERROR]: image generation request failed:")


# ---------------------------------------------------------------------------
# _resolve_save_path / _extension_for_mime
# ---------------------------------------------------------------------------


def test_resolve_save_path_defaults_to_agent_workspace_generated_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(vg, "get_agent_workspace_dir", lambda: tmp_path)

    target = vg._resolve_save_path(None, "image_abc.png")

    assert target == tmp_path / "generated_images" / "image_abc.png"
    assert target.parent.is_dir()


def test_resolve_save_path_honors_custom_save_dir(tmp_path: Path):
    custom_dir = tmp_path / "my_images"

    target = vg._resolve_save_path(str(custom_dir), "image_abc.png")

    assert target == custom_dir / "image_abc.png"
    assert custom_dir.is_dir()


@pytest.mark.parametrize(
    "mime, expected",
    [
        ("image/png", "png"),
        ("image/webp", "webp"),
        ("image/gif", "gif"),
        ("image/jpeg", "jpg"),
        ("application/octet-stream", "jpg"),
    ],
)
def test_extension_for_mime(mime: str, expected: str):
    assert vg._extension_for_mime(mime) == expected


def test_vllm_omni_is_configured_without_key_or_model(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VISUAL_GEN_API_BASE", "http://127.0.0.1:8091/v1")
    assert vg.visual_gen_configured() is False
    monkeypatch.setenv("VISUAL_GEN_PROTOCOL", "vllm-omni")
    assert vg.visual_gen_configured() is True
