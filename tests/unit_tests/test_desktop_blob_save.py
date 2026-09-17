import base64
import os

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


def _begin_transfer(
    runtime: desktop_app.DesktopRuntime,
    filename: str,
    mime_type: str,
    total_size: int,
) -> str:
    result = runtime.begin_blob_save(filename, mime_type, total_size)
    assert result["ok"] is True
    assert result["cancelled"] is False
    transfer_id = result.get("transfer_id")
    assert isinstance(transfer_id, str)
    return transfer_id


def test_blob_save_streams_multiple_chunks_and_commits_atomically(
    monkeypatch, tmp_path
):
    runtime = _runtime()
    target_path = tmp_path / "share.png"
    content = desktop_app.PNG_SIGNATURE + b"x" * (
        desktop_app.DESKTOP_BLOB_CHUNK_SIZE + 17
    )
    monkeypatch.setattr(
        runtime, "_select_save_path", lambda filename, file_types: target_path
    )

    transfer_id = _begin_transfer(runtime, "share.png", "image/png", len(content))
    for offset in range(0, len(content), desktop_app.DESKTOP_BLOB_CHUNK_SIZE):
        chunk = content[offset : offset + desktop_app.DESKTOP_BLOB_CHUNK_SIZE]
        assert runtime.append_blob_save(
            transfer_id, base64.b64encode(chunk).decode("ascii")
        )

    assert runtime.finish_blob_save(transfer_id) == {
        "ok": True,
        "cancelled": False,
    }
    assert target_path.read_bytes() == content
    assert list(tmp_path.glob(".*.part")) == []


def test_blob_save_reports_cancellation_before_creating_a_transaction(monkeypatch):
    runtime = _runtime()
    monkeypatch.setattr(runtime, "_select_save_path", lambda filename, file_types: None)

    result = runtime.begin_blob_save("share.png", "image/png", 1024)

    assert result == {"ok": False, "cancelled": True}
    assert runtime._blob_save_transfers == {}


def test_blob_save_rejects_invalid_metadata_before_showing_a_dialog(monkeypatch):
    runtime = _runtime()

    def unexpected_dialog(*args, **kwargs):
        raise AssertionError("invalid blob metadata must not open a save dialog")

    monkeypatch.setattr(runtime, "_select_save_path", unexpected_dialog)

    assert runtime.begin_blob_save("share.svg", "image/png", 10) == {
        "ok": False,
        "cancelled": False,
    }
    assert runtime.begin_blob_save("share.png", "image/png;charset=utf-8", 10) == {
        "ok": False,
        "cancelled": False,
    }
    assert runtime.begin_blob_save("share.png", "image/png", -1) == {
        "ok": False,
        "cancelled": False,
    }


def test_blob_save_discards_invalid_chunks_and_preserves_existing_file(
    monkeypatch, tmp_path
):
    runtime = _runtime()
    target_path = tmp_path / "diagram.svg"
    target_path.write_bytes(b"existing")
    monkeypatch.setattr(
        runtime, "_select_save_path", lambda filename, file_types: target_path
    )
    transfer_id = _begin_transfer(runtime, "diagram.svg", "image/svg+xml", 6)

    assert runtime.append_blob_save(transfer_id, "not-base64") is False
    assert runtime.finish_blob_save(transfer_id) == {
        "ok": False,
        "cancelled": False,
    }
    assert target_path.read_bytes() == b"existing"
    assert list(tmp_path.glob(".*.part")) == []


def test_blob_save_rejects_incomplete_transfers_and_cleans_partial_files(
    monkeypatch, tmp_path
):
    runtime = _runtime()
    target_path = tmp_path / "diagram.mmd"
    target_path.write_bytes(b"existing")
    monkeypatch.setattr(
        runtime, "_select_save_path", lambda filename, file_types: target_path
    )
    transfer_id = _begin_transfer(runtime, "diagram.mmd", "text/plain", 10)
    assert runtime.append_blob_save(
        transfer_id, base64.b64encode(b"partial").decode("ascii")
    )

    assert runtime.finish_blob_save(transfer_id) == {
        "ok": False,
        "cancelled": False,
    }
    assert target_path.read_bytes() == b"existing"
    assert list(tmp_path.glob(".*.part")) == []


def test_blob_save_aborts_open_transactions_during_shutdown(monkeypatch, tmp_path):
    runtime = _runtime()
    target_path = tmp_path / "diagram.svg"
    monkeypatch.setattr(
        runtime, "_select_save_path", lambda filename, file_types: target_path
    )
    transfer_id = _begin_transfer(runtime, "diagram.svg", "image/svg+xml", 6)
    assert runtime.append_blob_save(
        transfer_id, base64.b64encode(b"<svg>").decode("ascii")
    )

    runtime.shutdown()

    assert runtime._blob_save_transfers == {}
    assert not target_path.exists()
    assert list(tmp_path.glob(".*.part")) == []


def test_blob_save_preserves_existing_file_when_atomic_replace_fails(
    monkeypatch, tmp_path
):
    runtime = _runtime()
    target_path = tmp_path / "diagram.svg"
    target_path.write_bytes(b"existing")
    content = b"<svg/>"
    monkeypatch.setattr(
        runtime, "_select_save_path", lambda filename, file_types: target_path
    )

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    transfer_id = _begin_transfer(runtime, "diagram.svg", "image/svg+xml", len(content))
    assert runtime.append_blob_save(
        transfer_id, base64.b64encode(content).decode("ascii")
    )

    assert runtime.finish_blob_save(transfer_id) == {
        "ok": False,
        "cancelled": False,
    }
    assert target_path.read_bytes() == b"existing"
    assert list(tmp_path.glob(".*.part")) == []
