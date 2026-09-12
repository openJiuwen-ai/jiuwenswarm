# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for jiuwenswarm.agents.harness.common.tools.visual_gen_tools."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

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
    for name in ("VISUAL_GEN_API_KEY", "VISUAL_GEN_API_BASE", "VISUAL_GEN_MODEL_NAME", "VISUAL_GEN_ENABLED"):
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
    png_bytes = b"\x89PNG\r\n\x1a\nfakepngbytes"
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
            json={"choices": [{"message": {"images": [{"image_url": {"url": _data_uri("image/png", b"x")}}]}}]},
        )

    _patch_async_client(monkeypatch, handler)

    def _raise_disk_full(self, data):
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_bytes", _raise_disk_full)

    result = await generate_visual(prompt="a cat", save_dir=str(tmp_path))

    assert result.startswith("[ERROR]: failed to save image")
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
                            "images": [{"image_url": {"url": _data_uri("image/png", b"bytes")}}],
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
async def test_generate_visual_handles_remote_url_without_downloading(
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
                            "images": [{"image_url": {"url": "https://cdn.example.com/generated.png"}}],
                        }
                    }
                ]
            },
        )

    _patch_async_client(monkeypatch, handler)

    result = await generate_visual(prompt="a rainbow", save_dir=str(tmp_path))

    assert "https://cdn.example.com/generated.png" in result
    assert list(tmp_path.glob("*")) == []


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
                                {"image_url": {"url": _data_uri("image/png", b"one")}},
                                {"image_url": {"url": _data_uri("image/webp", b"two")}},
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
