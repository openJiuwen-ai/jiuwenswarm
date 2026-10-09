# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the per-session named-asset registry (session_assets)."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime import session_assets as sa


@pytest.fixture
def manager(monkeypatch, tmp_path):
    monkeypatch.setattr(sa, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    return sa.SessionAssetManager()


def _file(tmp_path, name: str) -> str:
    path = tmp_path / "files" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    return str(path)


@pytest.mark.asyncio
async def test_register_names_kinds_and_is_idempotent(manager, tmp_path):
    photo, clip = _file(tmp_path, "serum.png"), _file(tmp_path, "clip.mp4")
    items = [{"path": photo}, {"path": clip, "source": "generated"}, {"path": str(tmp_path / "missing.png")}]
    result = await manager.handle_session_assets_register({"session_id": "s1", "items": items})
    assets = result["assets"]
    assert [(a["name"], a["kind"], a["source"]) for a in assets] == [
        ("serum", "image", "upload"),
        ("clip", "video", "generated"),
    ]  # the missing file was skipped
    again = await manager.handle_session_assets_register({"session_id": "s1", "items": [{"path": photo}]})
    assert len(again["assets"]) == 2  # same path is not registered twice
    assert (await manager.handle_session_assets_list({"session_id": "s1"}))["assets"] == again["assets"]
    assert (await manager.handle_session_assets_list({"session_id": "other"}))["assets"] == []


@pytest.mark.asyncio
async def test_default_names_are_made_unique(manager, tmp_path):
    first, second = _file(tmp_path, "a/fox.png"), _file(tmp_path, "b/fox.png")
    result = await manager.handle_session_assets_register(
        {"session_id": "s1", "items": [{"path": first}, {"path": second}]}
    )
    assert [a["name"] for a in result["assets"]] == ["fox", "fox 2"]


@pytest.mark.asyncio
async def test_rename_rules(manager, tmp_path):
    result = await manager.handle_session_assets_register(
        {"session_id": "s1", "items": [{"path": _file(tmp_path, "one.png")}, {"path": _file(tmp_path, "two.png")}]}
    )
    one, two = result["assets"]
    renamed = await manager.handle_session_assets_rename({"session_id": "s1", "asset_id": one["asset_id"], "name": "  Oat  Serum "})
    assert renamed["assets"][0]["name"] == "Oat Serum"  # trimmed, inner whitespace collapsed
    for bad in ("", "a@b", "x" * 61):
        with pytest.raises(sa.SessionAssetError) as exc:
            await manager.handle_session_assets_rename({"session_id": "s1", "asset_id": one["asset_id"], "name": bad})
        assert exc.value.code == "INVALID_NAME"
    with pytest.raises(sa.SessionAssetError) as exc:  # names are unique per session, case-insensitively
        await manager.handle_session_assets_rename({"session_id": "s1", "asset_id": two["asset_id"], "name": "OAT serum"})
    assert exc.value.code == "NAME_CONFLICT"
    with pytest.raises(sa.SessionAssetError) as exc:
        await manager.handle_session_assets_rename({"session_id": "s1", "asset_id": "nope", "name": "x"})
    assert exc.value.code == "ASSET_NOT_FOUND"
    # a rename persists across manager instances (state is on disk, per session)
    assert (await sa.SessionAssetManager().handle_session_assets_list({"session_id": "s1"}))["assets"][0]["name"] == "Oat Serum"


@pytest.mark.asyncio
async def test_session_id_is_validated(manager):
    for bad in ("", "../escape", "a/b"):
        with pytest.raises(sa.SessionAssetError) as exc:
            await manager.handle_session_assets_list({"session_id": bad})
        assert exc.value.code == "INVALID_PARAMS"
