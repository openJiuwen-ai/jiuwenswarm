from pathlib import Path

from jiuwenswarm.channels.web import file_picker


def test_remember_and_resolve_last_file_picker_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(file_picker, "_last_dir_state_path", lambda: tmp_path / "last_file_picker_dir.txt")
    target = tmp_path / "uploads"
    target.mkdir()
    nested = target / "notes.txt"
    nested.write_text("x", encoding="utf-8")

    file_picker.remember_file_picker_dir(nested)
    assert file_picker.get_last_file_picker_dir() == str(target.resolve())
    assert file_picker.resolve_file_picker_initial_dir(None) == str(target.resolve())
    override = tmp_path / "other"
    override.mkdir()
    assert file_picker.resolve_file_picker_initial_dir(str(override)) == str(override.resolve())


def test_describe_local_file_document(tmp_path: Path):
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")

    item = file_picker.describe_local_file(path)

    assert item is not None
    assert item["kind"] == "document"
    assert item["filename"] == "notes.txt"
    assert item["path"] == str(path.resolve())
    assert item["size"] == 5
    assert "base64" not in item
    assert "error" not in item


def test_describe_local_file_image_includes_base64(tmp_path: Path):
    path = tmp_path / "pic.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16)

    item = file_picker.describe_local_file(path)

    assert item is not None
    assert item["kind"] == "image"
    assert item["filename"] == "pic.png"
    assert item["base64"]
    assert "error" not in item


def test_describe_local_file_forbidden_extension(tmp_path: Path):
    path = tmp_path / "setup.exe"
    path.write_bytes(b"MZ")

    item = file_picker.describe_local_file(path)

    assert item is not None
    assert item["kind"] == "document"
    assert item["error"] == "forbidden"


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


def test_missing_mimetypes_guess_uses_image_extension(monkeypatch):
    monkeypatch.setattr(file_picker.mimetypes, "guess_type", lambda _filename: (None, None))
    assert set(file_picker.IMAGE_EXTENSIONS) == set(file_picker._IMAGE_MIME_BY_EXTENSION)
    for ext in file_picker.IMAGE_EXTENSIONS:
        mime = file_picker.attachment_mime_type(f"sample{ext}")
        assert mime.startswith("image/")


def test_attachment_mime_keeps_mimetypes_guess(monkeypatch):
    monkeypatch.setattr(file_picker.mimetypes, "guess_type", lambda _filename: ("image/png", None))
    assert file_picker.attachment_mime_type("sample.webp") == "image/png"
    assert file_picker.attachment_mime_type("notes.txt") == "image/png"


def test_attachment_dialog_extensions_exclude_blacklist():
    for ext in (".exe", ".dll", ".msi", ".bat", ".ps1", ".dmg", ".app"):
        assert ext not in file_picker.ATTACHMENT_DIALOG_EXTENSIONS
    assert ".txt" in file_picker.ATTACHMENT_DIALOG_EXTENSIONS
    assert ".png" in file_picker.ATTACHMENT_DIALOG_EXTENSIONS


def test_tk_filetypes_omit_all_files_mask():
    filetypes = file_picker._tk_filetypes()
    assert len(filetypes) == 1
    assert filetypes[0][0] == "Allowed files"
    assert "*.*" not in filetypes[0][1]
    assert "*.txt" in filetypes[0][1]
    assert "*.exe" not in filetypes[0][1]
