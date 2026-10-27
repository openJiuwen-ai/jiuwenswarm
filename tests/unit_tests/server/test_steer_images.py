# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Steer file references must remain confined to validated session uploads."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess

from PIL import Image
import pytest

from jiuwenswarm.server.runtime.attachments import steer_images


@pytest.fixture
def uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sessions = tmp_path / "sessions"
    upload_dir = sessions / "session-a" / "uploads"
    upload_dir.mkdir(parents=True)
    monkeypatch.setattr(steer_images, "get_agent_sessions_dir", lambda: sessions)
    return upload_dir


def _image(path: Path, image_format: str = "PNG") -> None:
    Image.new("RGB", (32, 16), color="red").save(path, format=image_format)


def _item(path: Path, mime_type: str = "image/png") -> dict:
    return {"type": "image", "path": str(path), "filename": path.name, "mime_type": mime_type}


@pytest.mark.parametrize("module_name", ["jiuwenswarm.runtime.session_input", "gateway.runtime.session_input"])
@pytest.mark.parametrize("query", [None, "", " ", "compare screenshots"])
def test_reference_only_steer_accepts_image_only_without_filesystem_access(module_name: str, query) -> None:
    module = importlib.import_module(module_name)
    params = {"input_mode": "steer", "media_items": [_item(Path("/not-an-existing-upload.png"))]}
    if query is not None:
        params["query"] = query
    module.validate_session_input(params)


@pytest.mark.parametrize("module_name", ["jiuwenswarm.runtime.session_input", "gateway.runtime.session_input"])
@pytest.mark.parametrize("change", [
    {"media_items": "not a list"},
    {"media_items": [{"type": "image", "base64Data": "AAAA"}]},
    {"media_items": [{"type": "video", "path": "/file.mp4", "filename": "file.mp4", "mime_type": "video/mp4"}]},
    {"media_items": [{"type": "image", "path": "/file.svg", "filename": "file.svg", "mime_type": "image/svg+xml"}]},
    {"media_items": [_item(Path("/file.png")) | {"_persisted": True}]},
    {"media_items": [_item(Path("/file.png")) | {"base64_data": "AAAA"}]},
    {"media_items": [_item(Path("/file.png")) | {"path": 123}]},
    {"media_items": [_item(Path("/file.png")) | {"filename": ""}]},
    {"media_items": [_item(Path("/file.png"))] * 9},
    {"images": ["file.png"]},
    {"image_files": ["file.png"]},
    {"files": {"uploaded_images": [_item(Path("/file.png"))]}},
    {"attachments": [{"path": "/file.pdf"}]},
    {"audio_files": ["file.wav"]},
    {"video_files": ["file.mp4"]},
    {"query": {"type": "image"}},
])
def test_reference_only_steer_rejects_unsupported_payload(module_name: str, change: dict) -> None:
    module = importlib.import_module(module_name)
    params = {"input_mode": "steer", "query": "look", "media_items": [_item(Path("/file.png"))], **change}
    with pytest.raises(ValueError):
        module.validate_session_input(params)


@pytest.mark.parametrize("module_name", ["jiuwenswarm.runtime.session_input", "gateway.runtime.session_input"])
def test_empty_steer_and_image_only_follow_up_still_require_text(module_name: str) -> None:
    module = importlib.import_module(module_name)
    for params in (
        {"input_mode": "steer", "query": "", "media_items": []},
        {"input_mode": "follow_up", "query": "", "media_items": [_item(Path("/file.png"))]},
    ):
        with pytest.raises(ValueError, match="non-empty text"):
            module.validate_session_input(params)


@pytest.mark.parametrize(("name", "image_format", "mime_type"), [
    ("shot.PNG", "PNG", "image/png"),
    ("shot.jpg", "JPEG", "image/jpeg"),
    ("shot.jpeg", "JPEG", "image/jpeg"),
    ("shot.JFIF", "JPEG", "image/jpeg"),
    ("shot.webp", "WEBP", "image/webp"),
    ("shot.gif", "GIF", "image/gif"),
])
def test_resolves_real_upload_and_recomputes_client_metadata(uploads: Path, name: str, image_format: str, mime_type: str) -> None:
    path = uploads / name
    _image(path, image_format)
    item = _item(path, mime_type) | {"size_bytes": "untrusted"}
    params = {"media_items": [item]}
    assert steer_images.resolve_steer_image_references(params, "session-a") == [{
        **_item(path, mime_type), "size_bytes": path.stat().st_size,
    }]
    assert item["size_bytes"] == "untrusted"


def test_resolves_both_uploaded_screenshots(uploads: Path) -> None:
    paths = [uploads / "before.png", uploads / "after.png"]
    for path in paths:
        _image(path)
    result = steer_images.resolve_steer_image_references({"media_items": [_item(path) for path in paths]}, "session-a")
    assert [image["path"] for image in result] == [str(path) for path in paths]


@pytest.mark.parametrize("location", ["other-session", "local", "nested", "relative", "traversal"])
def test_rejects_paths_outside_direct_current_session_uploads(uploads: Path, tmp_path: Path, location: str) -> None:
    path = {
        "other-session": uploads.parent.parent / "session-b" / "uploads" / "shot.png",
        "local": tmp_path / "private.png",
        "nested": uploads / "nested" / "shot.png",
        "relative": Path("shot.png"),
        "traversal": uploads / ".." / "uploads" / "shot.png",
    }[location]
    if location not in {"relative", "traversal"}:
        path.parent.mkdir(parents=True, exist_ok=True)
        _image(path)
    with pytest.raises(ValueError, match="outside the current session uploads"):
        steer_images.resolve_steer_image_references({"media_items": [_item(path)]}, "session-a")


@pytest.mark.parametrize("bad_file", ["missing", "directory", "empty", "oversized", "invalid", "truncated"])
def test_rejects_unreadable_or_invalid_uploaded_image(uploads: Path, bad_file: str) -> None:
    path = uploads / "shot.png"
    if bad_file == "directory":
        path.mkdir()
    elif bad_file == "empty":
        path.touch()
    elif bad_file == "oversized":
        with path.open("wb") as stream:
            stream.truncate(steer_images.MAX_STEER_IMAGE_BYTES + 1)
    elif bad_file == "invalid":
        path.write_text("not an image", encoding="utf-8")
    elif bad_file == "truncated":
        _image(path)
        path.write_bytes(path.read_bytes()[:-12])
    with pytest.raises(ValueError):
        steer_images.resolve_steer_image_references({"media_items": [_item(path)]}, "session-a")


@pytest.mark.parametrize(("name", "image_format", "mime_type"), [
    ("shot.png", "JPEG", "image/png"),
    ("shot.png", "PNG", "image/jpeg"),
    ("shot.jpg", "PNG", "image/png"),
])
def test_rejects_spoofed_mime_or_extension(uploads: Path, name: str, image_format: str, mime_type: str) -> None:
    path = uploads / name
    _image(path, image_format)
    with pytest.raises(ValueError, match="actual image format"):
        steer_images.resolve_steer_image_references({"media_items": [_item(path, mime_type)]}, "session-a")


def test_rejects_forged_filename(uploads: Path) -> None:
    path = uploads / "shot.png"
    _image(path)
    with pytest.raises(ValueError, match="filename does not match"):
        steer_images.resolve_steer_image_references({"media_items": [_item(path) | {"filename": "other.png"}]}, "session-a")


def test_rejects_filename_that_upload_storage_cannot_produce(uploads: Path) -> None:
    path = uploads / "shot\u202e.png"
    _image(path)
    with pytest.raises(ValueError, match="not an uploaded filename"):
        steer_images.resolve_steer_image_references({"media_items": [_item(path)]}, "session-a")


def test_image_steering_requires_a_current_session(uploads: Path) -> None:
    path = uploads / "shot.png"
    _image(path)
    with pytest.raises(ValueError, match="requires a current session"):
        steer_images.resolve_steer_image_references({"media_items": [_item(path)]}, None)


def test_rejects_excessive_decoded_image_before_loading(uploads: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = uploads / "shot.png"
    _image(path)
    monkeypatch.setattr(steer_images, "MAX_STEER_IMAGE_PIXELS", 100)
    with pytest.raises(ValueError, match="exceeds 100 pixels"):
        steer_images.resolve_steer_image_references({"media_items": [_item(path)]}, "session-a")


@pytest.mark.parametrize("directory", [False, True])
def test_rejects_symlink_escape(uploads: Path, tmp_path: Path, directory: bool) -> None:
    original = tmp_path / "outside"
    original.mkdir()
    _image(original / "shot.png")
    link = uploads if directory else uploads / "shot.png"
    if directory:
        uploads.rmdir()
    try:
        link.symlink_to(original if directory else original / "shot.png", target_is_directory=directory)
    except OSError:
        pytest.skip("creating symlinks is unavailable on this platform")
    with pytest.raises(ValueError, match="symlinks or junctions"):
        steer_images.resolve_steer_image_references({"media_items": [_item(uploads / "shot.png")]}, "session-a")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction containment")
@pytest.mark.parametrize("junction_level", ["session", "uploads"])
def test_rejects_windows_junction_escape(uploads: Path, tmp_path: Path, junction_level: str) -> None:
    outside = tmp_path / "outside"
    target_uploads = outside / "uploads" if junction_level == "session" else outside
    target_uploads.mkdir(parents=True)
    _image(target_uploads / "shot.png")
    uploads.rmdir()
    junction = uploads if junction_level == "uploads" else uploads.parent
    if junction_level == "session":
        uploads.parent.rmdir()
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True, check=False)
    assert created.returncode == 0, created.stderr.decode(errors="replace")
    with pytest.raises(ValueError, match="symlinks or junctions"):
        steer_images.resolve_steer_image_references({"media_items": [_item(uploads / "shot.png")]}, "session-a")


def test_rejects_file_replaced_during_validation(uploads: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = uploads / "shot.png"
    _image(path)
    original = steer_images._image_mime_type

    def replace_after_validation(data: bytes) -> str:
        mime_type = original(data)
        path.write_bytes(data + b"changed")
        return mime_type

    monkeypatch.setattr(steer_images, "_image_mime_type", replace_after_validation)
    with pytest.raises(ValueError, match="changed during validation"):
        steer_images.resolve_steer_image_references({"media_items": [_item(path)]}, "session-a")
