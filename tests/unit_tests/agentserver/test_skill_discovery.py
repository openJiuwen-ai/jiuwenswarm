import asyncio
import base64
import json
import threading
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.skill_discovery import SkillDiscoveryReporter


def context():
    return SimpleNamespace(session=SimpleNamespace(get_session_id=lambda: "real-session"), exception=None, extra={},
        inputs=SimpleNamespace(tool_call=SimpleNamespace(id="real-call"), tool_name="bash",
                               tool_args={"command": "install test fixture"}))


def reporter(tmp_path, decision="accepted"):
    root = tmp_path / "skills"
    root.mkdir(exist_ok=True)
    bridge = SimpleNamespace(_send=AsyncMock(return_value={"decision": decision}), close=AsyncMock())
    return SkillDiscoveryReporter([root], tmp_path / "state.json", bridge)


@pytest.mark.asyncio
async def test_manual_copy_without_context_is_persisted_not_fabricated(tmp_path):
    r = reporter(tmp_path)
    directory = r.roots[0] / "manual"
    directory.mkdir()
    (directory / "SKILL.md").write_text("manual skill", encoding="utf-8")
    await r.new_observer().scan(r.roots)
    await r.flush()
    record = next(iter(r.records.values()))
    assert record["status"] == "awaiting_context" and record["event"] is None
    r.bridge._send.assert_not_called()
    saved = json.loads(r.state_path.read_text(encoding="utf-8"))
    assert saved["records"] == r.records
    restored = SkillDiscoveryReporter(r.roots, r.state_path, r.bridge)
    await restored.new_observer().scan(r.roots)
    assert len(restored.records) == 1
    await r.close()
    await restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["zip", "download", "directory"])
async def test_tool_installation_snapshots_content_and_deduplicates(tmp_path, method):
    r = reporter(tmp_path)
    observer = r.new_observer()
    await observer.scan(r.roots)  # Tool start baseline.
    directory = r.roots[0] / "installed"
    content = "---\nname: installed\ndescription: 示例\n---\n完成".encode()
    if method == "zip":
        archive = tmp_path / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("installed/SKILL.md", content)
        with zipfile.ZipFile(archive) as package:
            package.extractall(r.roots[0])
    else:
        directory.mkdir()
        (directory / "SKILL.md").write_bytes(content)
    # The background scanner may win the race. The per-tool observer must
    # still enrich the existing pending event rather than create another one.
    await r.new_observer().scan(r.roots)
    await observer.scan(r.roots, context=context())
    await r.flush()
    r.bridge._send.assert_awaited_once()
    event = r.bridge._send.call_args.args[0]
    assert event["stage"] == "skill.discovered" and event["mode"] == "report"
    assert event["sessionId"] == "real-session" and event["tool"]["id"] == "real-call"
    assert base64.b64decode(event["skill"]["documentBase64"]) == content
    (directory / "SKILL.md").unlink()  # Delivery no longer depends on this path.
    assert base64.b64decode(event["skill"]["documentBase64"]) == content
    (directory / "SKILL.md").write_bytes(content)
    await r.new_observer().scan(r.roots, context=context())
    await r.flush()
    r.bridge._send.assert_awaited_once()
    await r.close()


@pytest.mark.asyncio
async def test_market_style_copy_and_listing_scan_need_no_agent(tmp_path):
    r = reporter(tmp_path)
    directory = r.roots[0] / "market"
    directory.mkdir()
    (directory / "SKILL.md").write_text("market", encoding="utf-8")
    # Same Core hook is used by a directory scan and by a UI listing refresh.
    await r.new_observer().scan(r.roots)
    await r.new_observer().scan(r.roots)
    assert len(r.records) == 1
    r.bridge._send.assert_not_called()
    await r.close()


@pytest.mark.asyncio
async def test_local_channel_failure_retries_without_new_event_id(tmp_path):
    r = reporter(tmp_path, "unavailable")
    directory = r.roots[0] / "retry"
    directory.mkdir()
    (directory / "SKILL.md").write_text("retry", encoding="utf-8")
    await r.new_observer().scan(r.roots, context=context())
    await r._flush_task
    first = r.bridge._send.call_args.args[0]
    assert next(iter(r.records.values()))["status"] == "pending"
    r.bridge._send.return_value = {"decision": "accepted"}
    await asyncio.gather(r.flush(), r.flush())
    assert r.bridge._send.await_count == 2
    assert r.bridge._send.call_args.args[0] == first
    assert next(iter(r.records.values()))["status"] == "queued"
    await r.close()


@pytest.mark.asyncio
async def test_failed_persistence_does_not_silently_acknowledge(tmp_path, monkeypatch):
    r = reporter(tmp_path)
    directory = r.roots[0] / "retry"
    directory.mkdir()
    (directory / "SKILL.md").write_text("retry", encoding="utf-8")
    save = r._save
    def fail():
        raise OSError("disk unavailable")
    monkeypatch.setattr(r, "_save", fail)
    observer = r.new_observer()
    assert await observer.scan(r.roots) == []
    assert r.records == {}
    monkeypatch.setattr(r, "_save", save)
    assert len(await observer.scan(r.roots)) == 1
    await r.close()


@pytest.mark.asyncio
async def test_install_bookkeeping_failure_does_not_block_allowed_install(tmp_path, monkeypatch):
    r = reporter(tmp_path)
    def fail():
        raise OSError("disk unavailable")
    monkeypatch.setattr(r, "_save", fail)
    assert await r.mark_install_reported(r.roots[0] / "installed", "fingerprint") is False
    assert r.records == {}
    await r.close()


@pytest.mark.asyncio
async def test_cancelled_save_finishes_before_next_save(tmp_path, monkeypatch):
    r = reporter(tmp_path)
    started, release = threading.Event(), threading.Event()
    save = r._save
    calls = []
    def delayed_save():
        calls.append(len(calls))
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        save()
    monkeypatch.setattr(r, "_save", delayed_save)
    first = asyncio.create_task(r.mark_install_reported(r.roots[0] / "one", "one"))
    assert await asyncio.to_thread(started.wait, 5)
    first.cancel()
    second = asyncio.create_task(r.mark_install_reported(r.roots[0] / "two", "two"))
    try:
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert len(calls) == 1
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second
    assert len(json.loads(r.state_path.read_text(encoding="utf-8"))["records"]) == 2
    await r.close()
