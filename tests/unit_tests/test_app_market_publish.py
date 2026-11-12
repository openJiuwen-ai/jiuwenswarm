"""Regression tests for publishing generated applications from a conversation."""
import asyncio
from pathlib import Path

import pytest

from jiuwenswarm.extensions.app_market.manager import AppMarketManager


def test_publish_generated_html_keeps_entry_and_source_session(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    page = source / "gomoku.html"
    page.write_text("<html><body>gomoku</body></html>")
    manager = AppMarketManager(str(tmp_path / "market"))

    assert callable(getattr(manager, "publish_app", None)), "Conversation publishing is not exposed"
    result = asyncio.run(manager.publish_app(str(page), "web-session"))
    manifest = manager._service._storage.load_manifest(result["app_id"])

    assert manifest.page_entry_path == "gomoku.html"
    assert manifest.source_session == "web-session"
    assert Path(manifest.work_dir, manifest.page_entry_path).read_text() == page.read_text()
    listed = manager.list_apps()["apps"]
    assert len(listed) == 1
    assert listed[0]["entry_path"] == str(Path(manifest.work_dir, manifest.page_entry_path).resolve())
    assert listed[0]["source_session"] == "web-session"


def test_publish_rejects_missing_entry_without_registering_app(tmp_path):
    manager = AppMarketManager(str(tmp_path / "market"))
    assert callable(getattr(manager, "publish_app", None)), "Conversation publishing is not exposed"
    with pytest.raises(ValueError):
        asyncio.run(manager.publish_app(str(tmp_path / "missing.html"), "session"))
    assert manager.list_apps() == {"apps": []}


def test_publish_rejects_non_application_files(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("notes")
    manager = AppMarketManager(str(tmp_path / "market"))
    assert callable(getattr(manager, "publish_app", None)), "Conversation publishing is not exposed"
    with pytest.raises(ValueError):
        asyncio.run(manager.publish_app(str(source), "session"))
    assert not (tmp_path / "scripts").exists()


def test_republishing_same_entry_updates_one_application(tmp_path):
    page = tmp_path / "app.html"
    page.write_text("<html>v1</html>")
    manager = AppMarketManager(str(tmp_path / "market"))
    assert callable(getattr(manager, "publish_app", None)), "Conversation publishing is not exposed"
    first = asyncio.run(manager.publish_app(str(page), "session"))
    second = asyncio.run(manager.publish_app(str(page), "session"))
    assert first["app_id"] == second["app_id"]
    assert len(manager.list_apps()["apps"]) == 1


def test_publish_uses_standard_html_metadata_and_local_icon(tmp_path):
    page = tmp_path / "gomoku.html"
    page.write_text('<html><head><title>五子棋 · Gomoku</title><meta name="application-name" content="五子棋 · Gomoku"><meta name="description" content="与 WorkSwarm AI 对弈，支持难度选择与对局存档。"><link rel="icon" href="gomoku.svg"></head></html>')
    (tmp_path / "gomoku.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="24"/></svg>')
    manager = AppMarketManager(str(tmp_path / "market"))
    result = asyncio.run(manager.publish_app(str(page), "session"))
    app = manager.list_apps()["apps"][0]
    assert app["name"] == "五子棋 · Gomoku"
    assert app["description"] == "与 WorkSwarm AI 对弈，支持难度选择与对局存档。"
    assert app["icon_url"].startswith("data:image/svg+xml;base64,")
    assert app["app_id"] == result["app_id"]


def test_publish_does_not_read_icons_outside_application(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (tmp_path / "private.svg").write_text("private")
    page = root / "index.html"
    page.write_text('<title>安全测试</title><link rel="icon" href="../private.svg">')
    manager = AppMarketManager(str(tmp_path / "market"))
    asyncio.run(manager.publish_app(str(page), "session"))
    assert manager.list_apps()["apps"][0]["icon_url"] == ""
