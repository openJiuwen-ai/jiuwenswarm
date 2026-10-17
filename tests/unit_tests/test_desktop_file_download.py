from pathlib import Path
import urllib.request

import pytest

pytest.importorskip("webview")

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


@pytest.mark.parametrize("language", [None, "zh", "en", "en-US"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_download_confirmation_is_a_sheet_on_the_macos_window(
    monkeypatch, language, confirmed
):
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock

    runtime = _runtime()
    native_window = object()
    runtime.window = SimpleNamespace(
        native=native_window, evaluate_js=Mock(return_value=language)
    )
    alert = Mock()
    appkit = SimpleNamespace(
        NSAlert=SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: alert)),
        NSAlertStyleInformational=1,
        NSAlertFirstButtonReturn=1000,
    )
    schedule = Mock()
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(
        sys.modules, "PyObjCTools", SimpleNamespace(AppHelper=SimpleNamespace(callAfter=schedule))
    )
    monkeypatch.setattr(desktop_app.sys, "platform", "darwin")
    monkeypatch.setattr(desktop_app.os, "name", "posix")
    launch = Mock()
    monkeypatch.setattr(desktop_app.subprocess, "Popen", launch)
    path = '/tmp/report "quoted".pdf'

    runtime._show_download_complete(path)

    alert.beginSheetModalForWindow_completionHandler_.assert_not_called()
    schedule.call_args.args[0]()
    owner, complete = alert.beginSheetModalForWindow_completionHandler_.call_args.args
    assert owner is native_window
    assert path in alert.setInformativeText_.call_args.args[0]
    english = language in ("en", "en-US")
    alert.setMessageText_.assert_called_once_with("Download complete" if english else "下载完成")
    launch.assert_not_called()
    complete(1000 if confirmed else 1001)
    assert launch.call_count == int(confirmed)
    if confirmed:
        assert launch.call_args.args[0] == ["/usr/bin/open", "-R", path]


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_windows_download_confirmation_has_an_owner_on_the_ui_thread(
    monkeypatch, confirmed, language
):
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock

    runtime = _runtime()
    native_window = Mock()
    runtime.window = SimpleNamespace(native=native_window, evaluate_js=Mock(return_value=language))
    show = Mock(return_value="yes" if confirmed else "no")
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda callback: callback))
    monkeypatch.setitem(sys.modules, "System.Windows.Forms", SimpleNamespace(
        DialogResult=SimpleNamespace(Yes="yes"),
        MessageBox=SimpleNamespace(Show=show),
        MessageBoxButtons=SimpleNamespace(YesNo="yes-no"),
        MessageBoxIcon=SimpleNamespace(Information="info"),
    ))
    monkeypatch.setattr(desktop_app.os, "name", "nt")
    monkeypatch.setattr(desktop_app, "_creationflags", lambda: 0)
    launch = Mock()
    monkeypatch.setattr(desktop_app.subprocess, "Popen", launch)

    runtime._show_download_complete(r"C:\Downloads\report.pdf")

    show.assert_not_called()
    native_window.Invoke.assert_called_once()
    native_window.Invoke.call_args.args[0]()
    show.assert_called_once()
    owner, message, title, buttons, icon = show.call_args.args
    assert owner is native_window
    assert r"C:\Downloads\report.pdf" in message
    assert title == ("Download complete" if language == "en" else "下载完成")
    assert (buttons, icon) == ("yes-no", "info")
    assert launch.call_count == int(confirmed)
    if confirmed:
        assert launch.call_args.args[0][1:] == ["/select,", r"C:\Downloads\report.pdf"]
