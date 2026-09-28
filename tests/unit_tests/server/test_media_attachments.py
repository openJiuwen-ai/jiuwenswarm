# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Image upload filenames keep recognized suffixes and append canonical ones."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.attachments import media_attachments


@pytest.mark.parametrize(
    ("filename", "mime_type", "expected"),
    [
        ("sample.jpeg", "image/jpeg", "sample.jpeg"),
        ("sample.JFIF", "image/jpeg", "sample.JFIF"),
        ("sample.jpg", "image/jpeg", "sample.jpg"),
        ("photo", "image/jpeg", "photo.jpg"),
        ("photo.bin", "image/jpeg", "photo.bin.jpg"),
        ("shot.png", "image/png", "shot.png"),
        ("plain", "image/png", "plain.png"),
        ("anim.gif", "image/gif", "anim.gif"),
        ("pic.webp", "image/webp", "pic.webp"),
    ],
)
def test_ensure_image_upload_filename_keeps_known_suffixes(
    filename: str, mime_type: str, expected: str
) -> None:
    suffix = media_attachments.image_suffix_for_mime(mime_type)
    assert suffix is not None
    assert media_attachments.ensure_image_upload_filename(filename, suffix) == expected


def test_supported_image_suffixes_include_jpeg_aliases() -> None:
    suffixes = media_attachments.supported_image_suffixes()
    assert {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".gif"} <= suffixes
    assert media_attachments.image_suffix_for_mime("image/jpeg") == ".jpg"


@pytest.mark.parametrize(
    ("filename", "expected_name"),
    [
        ("sample.jpeg", "sample.jpeg"),
        ("sample.jfif", "sample.jfif"),
        ("photo", "photo.jpg"),
        ("notes.txt", "notes.txt.jpg"),
    ],
)
def test_store_image_item_filename_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, expected_name: str
) -> None:
    monkeypatch.setattr(media_attachments, "get_agent_sessions_dir", lambda: tmp_path)
    payload = b"\xff\xd8\xff" + b"jpeg-bytes"
    params = {
        "media_items": [
            {
                "type": "image",
                "mimeType": "image/jpeg",
                "filename": filename,
                "base64Data": base64.b64encode(payload).decode("ascii"),
            }
        ]
    }

    media_attachments.normalize_chat_media_attachments(params, session_id="sess-jpeg")

    stored = params["media_items"]
    assert isinstance(stored, list) and len(stored) == 1
    assert stored[0]["filename"] == expected_name
    stored_path = Path(stored[0]["path"])
    assert stored_path.name == expected_name
    assert stored_path.read_bytes() == payload
