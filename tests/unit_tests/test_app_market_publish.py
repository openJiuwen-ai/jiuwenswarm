"""Regression tests for publishing generated applications from a conversation."""
import asyncio
import json
from pathlib import Path

import pytest

from jiuwenswarm.extensions.app_market.app_market.models import AppManifest
from jiuwenswarm.extensions.app_market.manager import AppMarketError, AppMarketManager


def test_pinned_apps_sort_first_then_newest_registration(tmp_path):
    manager = AppMarketManager(str(tmp_path / "market"))

    def register(app_id: str, created_at: str) -> None:
        manager.storage.save_manifest(app_id, AppManifest(
            app_id=app_id,
            name=app_id,
            version="1.0.0",
            run_type="static",
            work_dir=str(tmp_path / app_id),
            created_at=created_at,
        ))

    register("old", "2026-01-01T00:00:00Z")
    register("new", "2026-03-01T00:00:00Z")
    register("mid", "2026-02-01T00:00:00Z")

    assert [app["app_id"] for app in manager.list_apps()["apps"]] == ["new", "mid", "old"]
    listed = manager.list_apps()["apps"]
    assert all(app["pinned"] is False for app in listed)

    assert manager.set_app_pinned("old", "", True) == {"app_id": "old", "pinned": True}
    assert [app["app_id"] for app in manager.list_apps()["apps"]] == ["old", "new", "mid"]
    assert manager.storage.load_manifest("old").pinned is True

    assert manager.set_app_pinned("old", "", False) == {"app_id": "old", "pinned": False}
    assert [app["app_id"] for app in manager.list_apps()["apps"]] == ["new", "mid", "old"]
    assert manager.storage.load_manifest("old").pinned is False

    with pytest.raises(AppMarketError, match="Application not found"):
        manager.set_app_pinned("old", "another-owner", True)


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


def test_publish_version_defaults_and_republish_preserves(tmp_path):
    page = tmp_path / "app.html"
    page.write_text("<html>app</html>")
    manager = AppMarketManager(str(tmp_path / "market"))

    assert asyncio.run(manager.publish_app(str(page), "session"))["version"] == "1.0.0"

    explicit = asyncio.run(manager.publish_app(str(page), "session", version="1.4.0"))
    assert explicit["version"] == "1.4.0"

    preserved = asyncio.run(manager.publish_app(str(page), "session"))
    assert preserved["version"] == "1.4.0"
    assert manager.list_apps()["apps"][0]["version"] == "1.4.0"


def test_publish_reads_meta_version_and_rejects_conflicting_override(tmp_path):
    page = tmp_path / "app.html"
    page.write_text('<html><head><meta name="application-version" content="2.1.0"></head></html>')
    manager = AppMarketManager(str(tmp_path / "market"))

    assert asyncio.run(manager.publish_app(str(page), "session"))["version"] == "2.1.0"

    page.write_text('<html><head><meta name="application-version" content="2.2.0"></head></html>')
    assert asyncio.run(manager.publish_app(str(page), "session"))["version"] == "2.2.0"

    with pytest.raises(ValueError, match="version must match"):
        asyncio.run(manager.publish_app(str(page), "session", version="3.0.0"))
    assert manager.list_apps()["apps"][0]["version"] == "2.2.0"
    assert asyncio.run(manager.publish_app(str(page), "session", version="2.2.0"))["version"] == "2.2.0"


def test_publish_rejects_invalid_version_without_replacing_app(tmp_path):
    page = tmp_path / "app.html"
    page.write_text("<html>app</html>")
    manager = AppMarketManager(str(tmp_path / "market"))
    first = asyncio.run(manager.publish_app(str(page), "session", version="1.0.0"))

    with pytest.raises(ValueError, match="version"):
        asyncio.run(manager.publish_app(str(page), "session", version="release-one"))

    app = manager.list_apps()["apps"][0]
    assert app["app_id"] == first["app_id"]
    assert app["version"] == "1.0.0"


def test_publish_rejects_invalid_version_meta(tmp_path):
    page = tmp_path / "app.html"
    page.write_text('<html><head><meta name="application-version" content="v2"></head></html>')
    manager = AppMarketManager(str(tmp_path / "market"))

    with pytest.raises(ValueError, match="application-version"):
        asyncio.run(manager.publish_app(str(page), "session"))

    assert manager.list_apps() == {"apps": []}


def test_republish_preserves_pinned_flag_and_created_at(tmp_path):
    page = tmp_path / "app.html"
    page.write_text("<html>app</html>")
    manager = AppMarketManager(str(tmp_path / "market"))
    first = asyncio.run(manager.publish_app(str(page), "session"))
    app_id = first["app_id"]
    original = manager.storage.load_manifest(app_id)
    original_created_at = original.created_at
    assert original.pinned is False

    manager.set_app_pinned(app_id, "", True)
    assert manager.storage.load_manifest(app_id).pinned is True

    page.write_text("<html>app v2</html>")
    asyncio.run(manager.publish_app(str(page), "session", version="2.0.0"))

    republished = manager.storage.load_manifest(app_id)
    assert republished.pinned is True, "republishing must not silently drop the user pin"
    assert republished.version == "2.0.0"
    assert republished.created_at == original_created_at, (
        "created_at is the registration time and must survive republishing"
    )
    listed = manager.list_apps()["apps"]
    assert listed[0]["pinned"] is True


def test_republish_after_unpin_keeps_pinned_false(tmp_path):
    page = tmp_path / "app.html"
    page.write_text("<html>app</html>")
    manager = AppMarketManager(str(tmp_path / "market"))
    app_id = asyncio.run(manager.publish_app(str(page), "session"))["app_id"]

    manager.set_app_pinned(app_id, "", True)
    manager.set_app_pinned(app_id, "", False)
    page.write_text("<html>app v2</html>")
    asyncio.run(manager.publish_app(str(page), "session", version="2.0.0"))

    assert manager.storage.load_manifest(app_id).pinned is False


def test_invalid_role_declaration_does_not_modify_existing_registration(tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<html>WorkSwarmApp</html>", encoding="utf-8")
    manager = AppMarketManager(str(tmp_path / "market"))
    first = asyncio.run(manager.publish_app(str(page), "session"))
    assert manager.storage.load_manifest(first["app_id"]).permissions == ["ai"]
    declaration = {
        "schema_version": 1,
        "session_groups": [{
            "id": "guide",
            "mode": "agent",
            "session_description": "One session per game.",
            "roles": [{"id": "guide", "name": "Guide", "description": "Helps", "avatar": "../avatar.png"}],
        }],
    }
    (tmp_path / "app-ai-roles.json").write_text(json.dumps(declaration), encoding="utf-8")

    with pytest.raises(ValueError, match=r"app-ai-roles\.json.*avatar.*relative"):
        asyncio.run(manager.publish_app(str(page), "session", version="2.0.0"))

    assert manager.list_apps()["apps"][0]["version"] == "1.0.0"
