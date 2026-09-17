from pathlib import Path
import urllib.request

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


def test_download_file_prompts_for_destination_before_downloading(
    monkeypatch, tmp_path
):
    runtime = _runtime()
    target_path = tmp_path / "report.pdf"
    selected = []
    downloaded = []
    completed = []

    def select_save_path(filename: str, file_types: tuple[str, ...]) -> Path:
        selected.append((filename, file_types))
        return target_path

    def urlretrieve(url: str, path: Path) -> None:
        downloaded.append((url, path))
        path.write_bytes(b"pdf-content")

    monkeypatch.setattr(runtime, "_select_save_path", select_save_path)
    monkeypatch.setattr(runtime, "_show_download_complete", completed.append)
    monkeypatch.setattr(urllib.request, "urlretrieve", urlretrieve)

    result = runtime.download_file("https://example.test/report.pdf", "report.pdf")

    assert result == {"ok": True, "cancelled": False}
    assert selected == [("report.pdf", ())]
    assert downloaded[0][0] == "https://example.test/report.pdf"
    assert downloaded[0][1].parent == target_path.parent
    assert downloaded[0][1].suffix == ".part"
    assert target_path.read_bytes() == b"pdf-content"
    assert list(tmp_path.glob(".*.part")) == []
    assert completed == [str(target_path)]


def test_download_file_stops_when_destination_selection_is_cancelled(monkeypatch):
    runtime = _runtime()
    download_started = False

    monkeypatch.setattr(runtime, "_select_save_path", lambda filename, file_types: None)

    def unexpected_download(url: str, path: Path) -> None:
        nonlocal download_started
        download_started = True

    monkeypatch.setattr(urllib.request, "urlretrieve", unexpected_download)

    result = runtime.download_file("https://example.test/report.pdf", "report.pdf")

    assert result == {"ok": False, "cancelled": True}
    assert download_started is False


def test_download_file_rejects_invalid_suggested_filename(monkeypatch):
    runtime = _runtime()

    def reject_filename(filename: str, file_types: tuple[str, ...]) -> Path:
        raise ValueError("empty_filename")

    monkeypatch.setattr(runtime, "_select_save_path", reject_filename)

    result = runtime.download_file("https://example.test/report.pdf", "")

    assert result == {"ok": False, "cancelled": False}


def test_download_file_reports_transfer_failure_and_preserves_existing_file(
    monkeypatch, tmp_path
):
    runtime = _runtime()
    target_path = tmp_path / "report.pdf"
    target_path.write_bytes(b"existing-content")
    completed = []

    monkeypatch.setattr(
        runtime, "_select_save_path", lambda filename, file_types: target_path
    )
    monkeypatch.setattr(runtime, "_show_download_complete", completed.append)

    def fail_download(url: str, path: Path) -> None:
        path.write_bytes(b"partial-content")
        raise OSError("network disconnected")

    monkeypatch.setattr(urllib.request, "urlretrieve", fail_download)

    result = runtime.download_file("https://example.test/report.pdf", "report.pdf")

    assert result == {"ok": False, "cancelled": False}
    assert target_path.read_bytes() == b"existing-content"
    assert list(tmp_path.glob(".*.part")) == []
    assert completed == []


def test_download_file_reports_destination_write_failure(monkeypatch, tmp_path):
    runtime = _runtime()
    missing_parent = tmp_path / "missing"
    target_path = missing_parent / "report.pdf"

    monkeypatch.setattr(
        runtime, "_select_save_path", lambda filename, file_types: target_path
    )

    result = runtime.download_file("https://example.test/report.pdf", "report.pdf")

    assert result == {"ok": False, "cancelled": False}
    assert target_path.exists() is False
