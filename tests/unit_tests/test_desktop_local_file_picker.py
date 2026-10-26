from pathlib import Path

from jiuwenswarm.channels.desktop import desktop_app


def _runtime() -> desktop_app.DesktopRuntime:
    return desktop_app.DesktopRuntime(
        frontend_host="127.0.0.1",
        ports={
            "app": 19001,
            "web": 19000,
            "frontend": 5173,
            "tui": 19002,
            "third_party": 19003,
        },
    )


def test_describe_local_file_document(tmp_path: Path):
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")

    item = desktop_app.DesktopRuntime._describe_local_file(path)

    assert item is not None
    assert item["kind"] == "document"
    assert item["filename"] == "notes.txt"
    assert item["path"] == str(path.resolve())
    assert item["size"] == 5
    assert "base64" not in item
    assert "error" not in item


def test_describe_local_file_image_includes_base64(tmp_path: Path):
    path = tmp_path / "pic.png"
    # Minimal valid-enough bytes for base64 packaging (not a real PNG decode check).
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16)

    item = desktop_app.DesktopRuntime._describe_local_file(path)

    assert item is not None
    assert item["kind"] == "image"
    assert item["filename"] == "pic.png"
    assert item["base64"]
    assert "error" not in item


def test_describe_local_file_forbidden_extension(tmp_path: Path):
    path = tmp_path / "setup.exe"
    path.write_bytes(b"MZ")

    item = desktop_app.DesktopRuntime._describe_local_file(path)

    assert item is not None
    assert item["kind"] == "document"
    assert item["error"] == "forbidden"


def test_attachment_open_file_types_excludes_blacklist():
    file_types = desktop_app.attachment_open_file_types()
    assert len(file_types) == 1
    filter_text = file_types[0]
    assert filter_text.startswith("Allowed files (")
    assert "*.*" not in filter_text
    assert "*.txt" in filter_text
    assert "*.png" in filter_text
    for ext in (".exe", ".dll", ".msi", ".bat", ".ps1", ".dmg", ".app"):
        assert f"*{ext}" not in filter_text
    for ext in desktop_app.FORBIDDEN_DOCUMENT_EXTENSIONS:
        assert f"*{ext}" not in filter_text


def test_select_local_files_uses_allowed_file_types(monkeypatch, tmp_path: Path):
    from jiuwenswarm.channels.web import file_picker

    runtime = _runtime()
    doc = tmp_path / "a.md"
    doc.write_text("# hi", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(file_picker, "_last_dir_state_path", lambda: tmp_path / "last_file_picker_dir.txt")

    class FakeWindow:
        def create_file_dialog(self, *args, **kwargs):
            captured["file_types"] = kwargs.get("file_types")
            captured["directory"] = kwargs.get("directory")
            return [str(doc)]

    runtime.window = FakeWindow()
    results = runtime.select_local_files(allow_multiple=True)

    assert len(results) == 1
    assert results[0]["filename"] == "a.md"
    assert captured["file_types"] == desktop_app.attachment_open_file_types()
    # First open with no memory → user home; selection remembers the file's parent.
    assert captured["directory"] == str(Path.home())
    assert file_picker.get_last_file_picker_dir() == str(tmp_path.resolve())

    # Next open should start in the remembered directory.
    captured.clear()
    runtime.select_local_files(allow_multiple=True)
    assert captured["directory"] == str(tmp_path.resolve())


def test_window_api_select_local_files_delegates(monkeypatch, tmp_path: Path):
    runtime = _runtime()
    called = {}

    def fake_select(allow_multiple: bool = True, initial_dir: str | None = None):
        called["allow_multiple"] = allow_multiple
        called["initial_dir"] = initial_dir
        return [{"path": str(tmp_path / "x.txt"), "filename": "x.txt", "kind": "document"}]

    monkeypatch.setattr(runtime, "select_local_files", fake_select)
    api = desktop_app._WindowApi(runtime)

    out = api.select_local_files(False)

    assert called["allow_multiple"] is False
    assert called["initial_dir"] is None
    assert out[0]["filename"] == "x.txt"
