# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``publish_powerpoint_asset`` and the links the task pane downloads published files from."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import jsonschema
import pytest

from jiuwenswarm.common.file_download_token import validate_file_download_token
from jiuwenswarm.office import runtime
from jiuwenswarm.office.assets import MAX_ASSET_BYTES, OfficeAssetStore, PublishPowerPointAssetTool, referenced_handles
from jiuwenswarm.office.contract import POWERPOINT_PACKAGE, load_contract

PNG = b"\x89PNG\r\n\x1a\nimage-bytes"


def _session(session_id: str = "deck-a") -> SimpleNamespace:
    return SimpleNamespace(get_session_id=lambda: session_id)


def _pptx(names: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "<x/>")
    return buffer.getvalue()


@pytest.fixture
def project(tmp_path, monkeypatch, sessions):
    """The conversation ``deck-a``'s project, the agent workspace and its uploads are where files may come from."""
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.runtime.session import session_metadata

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(session_metadata, "get_session_metadata",
                        lambda sid, **_: {"project_dir": str(project)} if sid == "deck-a" else {})
    monkeypatch.setattr(utils, "get_agent_workspace_dir", lambda: tmp_path / "workspace")
    return project


async def _publish(path, session_id: str = "deck-a", store: OfficeAssetStore | None = None) -> dict:
    tool = PublishPowerPointAssetTool(store)
    return json.loads(await tool.invoke({"absolute_path": str(path)}, session=_session(session_id)))


def test_files_come_from_the_project_the_workspace_and_the_conversation_s_uploads(project, tmp_path, sessions):
    assert runtime.asset_roots("deck-a") == (project, tmp_path / "workspace", sessions / "deck-a" / "uploads")


async def test_a_published_image_is_kept_with_its_conversation(project, sessions):
    (project / "hero.png").write_bytes(PNG)

    published = await _publish(project / "hero.png")

    assert published == {"handle": published["handle"], "purpose": "input_image", "mime_type": "image/png",
                         "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}
    assert (sessions / "deck-a" / "office_assets" / f"{published['handle']}.bin").read_bytes() == PNG


async def test_a_published_descriptor_is_what_each_tool_taking_an_asset_accepts(project):
    """The model passes the descriptor on as it got it: under a key in a patch, or as an import's asset."""
    (project / "hero.png").write_bytes(PNG)
    (project / "deck.pptx").write_bytes(_pptx(["[Content_Types].xml", "ppt/presentation.xml"]))
    image, deck = await _publish(project / "hero.png"), await _publish(project / "deck.pptx")
    schemas = {tool.name: tool.input_schema for tool in load_contract(POWERPOINT_PACKAGE)}

    jsonschema.validate({**image, "key": "hero"}, schemas["presentation_apply_patch"]["properties"]["assets"]["items"])
    jsonschema.validate({"asset": deck}, schemas["import_slides"])


async def test_an_image_the_user_attached_to_the_conversation_can_be_published(project, sessions):
    uploads = sessions / "deck-a" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "logo.png").write_bytes(PNG)
    assert (await _publish(uploads / "logo.png"))["purpose"] == "input_image"

    foreign = sessions / "deck-b" / "uploads"
    foreign.mkdir(parents=True)
    (foreign / "logo.png").write_bytes(PNG)
    with pytest.raises(ValueError, match="outside"):
        await _publish(foreign / "logo.png")


async def test_files_outside_the_roots_symlinks_and_relative_paths_are_refused(project, tmp_path):
    (tmp_path / "outside.png").write_bytes(PNG)
    (project / "link.png").symlink_to(tmp_path / "outside.png")
    with pytest.raises(ValueError, match="outside"):
        await _publish(tmp_path / "outside.png")
    with pytest.raises(ValueError, match="symbolic link"):
        await _publish(project / "link.png")
    with pytest.raises(ValueError, match="absolute"):
        await _publish("hero.png")
    with pytest.raises(ValueError, match="does not exist"):
        await _publish(project / "missing.png")


@pytest.mark.parametrize("project_dir", ["/", "home"])
async def test_a_project_at_the_filesystem_root_or_the_home_directory_opens_no_file(
        project, tmp_path, monkeypatch, project_dir):
    from jiuwenswarm.server.runtime.session import session_metadata

    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Downloads" / "private.png").write_bytes(PNG)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    root = home if project_dir == "home" else Path("/")
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda sid, **_: {"project_dir": str(root)})

    with pytest.raises(ValueError, match="outside"):
        await _publish(home / "Downloads" / "private.png")


async def test_only_png_jpeg_and_pptx_content_is_published(project):
    (project / "deck.pptx").write_bytes(_pptx(["[Content_Types].xml", "ppt/presentation.xml"]))
    (project / "notes.pptx").write_bytes(_pptx(["[Content_Types].xml", "word/document.xml"]))
    (project / "photo.png").write_bytes(b"\xff\xd8\xffjpeg")
    (project / "fake.png").write_bytes(b"GIF89a")
    (project / "empty.png").write_bytes(b"")

    assert (await _publish(project / "deck.pptx"))["purpose"] == "input_pptx"
    assert (await _publish(project / "photo.png"))["mime_type"] == "image/jpeg"
    for name in ("notes.pptx", "fake.png", "empty.png"):
        with pytest.raises(ValueError):
            await _publish(project / name)
    with open(project / "huge.png", "wb") as huge:
        huge.write(PNG)
        huge.truncate(MAX_ASSET_BYTES + 1)
    with pytest.raises(ValueError, match="MB"):
        await _publish(project / "huge.png")


async def test_a_download_link_is_given_only_for_an_asset_of_the_conversation(project, sessions):
    (project / "hero.png").write_bytes(PNG)
    store = OfficeAssetStore()
    handle = (await _publish(project / "hero.png", store=store))["handle"]

    url = store.download_url("deck-a", handle)

    assert url.startswith("/file-api/download?token=")
    token = parse_qs(urlparse(url).query)["token"][0]
    payload = validate_file_download_token(token, session_id="deck-a")
    assert payload["path"] == str(sessions / "deck-a" / "office_assets" / f"{handle}.bin")
    with pytest.raises(ValueError, match="no asset"):
        store.download_url("deck-b", handle)
    with pytest.raises(ValueError, match="invalid asset handle"):
        store.download_url("deck-a", "../deck-b/office_assets/" + handle)


def test_handles_are_found_anywhere_in_the_arguments() -> None:
    first, second = "oa_" + "a" * 32, "oa_" + "b" * 32
    arguments = {"assets": [{"key": "hero", "asset": {"handle": first}}], "asset": {"handle": second},
                 "note": {"handle": "not-a-handle"}}
    assert referenced_handles(arguments) == {first, second}
