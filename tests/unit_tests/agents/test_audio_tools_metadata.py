"""Regression coverage for the audio_metadata upload file handle.

``requests`` does not close file objects owned by the caller. The upload handle
must close before response parsing and before the temp-file cleanup runs, so
repeated calls do not leak file descriptors and Windows can delete the temp
file.
"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.agents.harness.common.tools import audio_tools


class _Response:
    """Minimal requests.Response that records the upload handle state."""

    def __init__(self, handle, payload: str) -> None:
        self._handle = handle
        self._payload = payload
        self.encoding = ""
        self.closed_during_parse: bool | None = None

    @property
    def text(self) -> str:
        self.closed_during_parse = self._handle.closed
        return self._payload


@pytest.mark.asyncio
async def test_audio_metadata_closes_upload_handle(monkeypatch, tmp_path):
    audio_file = tmp_path / "sample.mp3"
    audio_file.write_bytes(b"ID3" + b"\x00" * 64)

    monkeypatch.setattr(audio_tools, "ACR_ACCESS_KEY", "test-key")
    monkeypatch.setattr(audio_tools, "ACR_ACCESS_SECRET", "test-secret")
    monkeypatch.setattr(audio_tools, "_compute_audio_length_seconds", lambda _path: 3.0)

    seen: dict[str, object] = {}

    def fake_post(_url, **kwargs):
        handle = kwargs["files"][0][1][1]
        seen["handle"] = handle
        seen["open_during_request"] = not handle.closed
        return _Response(
            handle,
            json.dumps(
                {
                    "metadata": {
                        "music": [
                            {
                                "title": "Test Song",
                                "artists": [{"name": "Test Artist"}],
                                "release_date": "2026-01-01",
                            }
                        ]
                    }
                }
            ),
        )

    monkeypatch.setattr(audio_tools.requests, "post", fake_post)

    result = await audio_tools.audio_metadata.invoke({"audio_path_or_url": str(audio_file)})

    handle = seen["handle"]
    assert seen["open_during_request"] is True
    assert handle.closed is True
    assert "Name: Test Song" in result
    assert "Test Artist" in result


@pytest.mark.asyncio
async def test_audio_metadata_closes_handle_before_temp_cleanup(monkeypatch, tmp_path):
    """Temp-file cleanup runs in ``finally``; the upload handle must be closed by then."""
    downloaded = tmp_path / "download.mp3"
    downloaded.write_bytes(b"ID3" + b"\x00" * 64)

    monkeypatch.setattr(audio_tools, "ACR_ACCESS_KEY", "test-key")
    monkeypatch.setattr(audio_tools, "ACR_ACCESS_SECRET", "test-secret")
    monkeypatch.setattr(audio_tools, "_compute_audio_length_seconds", lambda _path: 3.0)
    monkeypatch.setattr(audio_tools, "_download_audio_to_tempfile", lambda _url: str(downloaded))

    seen: dict[str, object] = {}

    def fake_post(_url, **kwargs):
        seen["handle"] = kwargs["files"][0][1][1]
        return _Response(seen["handle"], json.dumps({"metadata": {"music": []}}))

    real_remove = audio_tools.os.remove

    def fake_remove(path):
        seen["closed_at_cleanup"] = seen["handle"].closed
        real_remove(path)

    monkeypatch.setattr(audio_tools.requests, "post", fake_post)
    monkeypatch.setattr(audio_tools.os, "remove", fake_remove)

    await audio_tools.audio_metadata.invoke({"audio_path_or_url": "https://example.invalid/a.mp3"})

    assert seen["closed_at_cleanup"] is True
    assert seen["handle"].closed is True
    assert downloaded.exists() is False
