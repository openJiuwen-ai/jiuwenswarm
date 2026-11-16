from pathlib import Path

from agent_gateway.channel_manager.web import file_picker


def test_webp_keeps_image_mime_when_stdlib_guess_misses(tmp_path: Path, monkeypatch):
    """Python 3.11 mimetypes has no .webp; Windows 10 registry often doesn't either."""
    monkeypatch.setattr(file_picker.mimetypes, "guess_type", lambda _filename: (None, None))
    payload = b"RIFF\x1a\x00\x00\x00WEBPVP8 " + b"x" * 16
    path = tmp_path / "sample.webp"
    path.write_bytes(payload)

    item = file_picker.describe_local_file(path)

    assert item is not None
    assert item["kind"] == "image"
    assert item["mime_type"] == "image/webp"
    assert item["filename"] == "sample.webp"
    assert item["base64"]
    assert "error" not in item

    from jiuwenswarm.server.runtime.attachments import media_attachments

    monkeypatch.setattr(media_attachments, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    params = {
        "media_items": [
            {
                "type": "image",
                "mimeType": item["mime_type"],
                "filename": item["filename"],
                "base64Data": item["base64"],
            }
        ]
    }
    media_attachments.normalize_chat_media_attachments(params, session_id="sess-webp")
    stored = params["media_items"]
    assert len(stored) == 1
    stored_path = Path(stored[0]["path"])
    assert stored_path.suffix == ".webp"
    assert stored_path.read_bytes() == payload

    rejected = {
        "media_items": [
            {
                "type": "image",
                "mimeType": "application/octet-stream",
                "filename": "sample.webp",
                "base64Data": item["base64"],
            }
        ]
    }
    media_attachments.normalize_chat_media_attachments(rejected, session_id="sess-webp")
    assert "media_items" not in rejected


