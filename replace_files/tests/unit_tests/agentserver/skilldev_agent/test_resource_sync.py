# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile
from pathlib import Path

import pytest

from jiuwenclaw.agentserver.skilldev_agent.adapter import SkillDevDeepAdapter
from jiuwenclaw.agentserver.skilldev_agent.utils import resource_sync
from jiuwenclaw.agentserver.skilldev_agent.utils.resource_sync import (
    build_current_ref_file_hint_lines,
    build_current_ref_skill_hint_lines,
    build_current_tool_spec_hint_lines,
    delete_uploaded_resources,
    load_resource_state,
    record_direct_imported_skills,
    resource_state_path,
    save_resource_state,
    write_uploaded_resources,
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _state(workspace: Path) -> dict:
    return json.loads(resource_state_path(workspace).read_text(encoding="utf-8"))


def test_resource_state_path_and_bad_json_fallback(tmp_path: Path) -> None:
    path = resource_state_path(tmp_path)
    assert path == tmp_path / "resources" / "resource_state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad json", encoding="utf-8")

    assert load_resource_state(tmp_path) == {}


def test_record_direct_imported_skills_deduplicates(tmp_path: Path) -> None:
    record_direct_imported_skills(
        tmp_path,
        [
            {"filename": "example.skill"},
            {"name": "example.skill"},
            {"filename": "other.zip"},
        ],
    )

    assert _state(tmp_path)["direct_imported_skills"] == ["example.skill", "other.zip"]


def test_ref_files_are_synchronized_without_stale_deletion(tmp_path: Path) -> None:
    asyncio.run(
        write_uploaded_resources(
            tmp_path,
            {
                "files": [
                    {"filename": "keep.txt", "base64Data": _b64("keep")},
                    {"filename": "drop.txt", "base64Data": _b64("drop")},
                ]
            },
        )
    )
    ref_dir = tmp_path / "resources" / "ref-files"
    assert (ref_dir / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (ref_dir / "drop.txt").is_file()

    asyncio.run(
        write_uploaded_resources(
            tmp_path,
            {"files": [{"filename": "keep.txt", "base64Data": _b64("keep-updated")}]},
        )
    )

    assert (ref_dir / "keep.txt").read_text(encoding="utf-8") == "keep-updated"
    assert (ref_dir / "drop.txt").is_file()
    assert _state(tmp_path)["ref_files"] == [
        {"filename": "drop.txt"},
        {"filename": "keep.txt"},
    ]


def test_ref_files_empty_round_does_not_touch_state_or_disk(tmp_path: Path) -> None:
    asyncio.run(
        write_uploaded_resources(
            tmp_path,
            {"files": [{"filename": "a.txt", "base64Data": _b64("a")}]},
        )
    )
    ref_dir = tmp_path / "resources" / "ref-files"
    assert (ref_dir / "a.txt").is_file()
    state_before = _state(tmp_path)

    asyncio.run(write_uploaded_resources(tmp_path, {}))

    assert (ref_dir / "a.txt").is_file()
    assert _state(tmp_path) == state_before


def test_ref_skills_reject_unsupported_suffix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不支持的文件类型"):
        asyncio.run(
            write_uploaded_resources(
                tmp_path,
                {"skill_packages": [{"filename": "bad.txt", "base64Data": _b64("x")}]},
            )
        )


def test_direct_imported_packages_are_not_written_as_references(tmp_path: Path) -> None:
    record_direct_imported_skills(tmp_path, [{"filename": "imported.skill"}])

    asyncio.run(
        write_uploaded_resources(
            tmp_path,
            {
                "files": [{"filename": "imported.skill", "base64Data": _b64("not a zip")}],
                "skill_packages": [{"filename": "imported.skill", "base64Data": _b64("not a zip")}],
            },
        )
    )

    assert not (tmp_path / "resources" / "ref-files" / "imported.skill").exists()
    assert not (tmp_path / "resources" / "ref-skills" / "imported.skill").exists()
    state = _state(tmp_path)
    assert state["direct_imported_skills"] == ["imported.skill"]
    assert state.get("ref_files", []) == []
    assert state.get("ref_skills", []) == []


def test_existing_url_resource_is_rewritten_each_upload_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref_dir = tmp_path / "resources" / "ref-files"
    ref_dir.mkdir(parents=True)
    (ref_dir / "remote.txt").write_text("existing", encoding="utf-8")
    save_resource_state(
        tmp_path,
        {"ref_files": [{"filename": "remote.txt", "url": "https://example.com/remote.txt"}]},
    )
    calls: list[tuple[str, str]] = []

    async def fake_download(url: str, dest: str) -> None:
        calls.append((url, dest))
        Path(dest).write_text("downloaded", encoding="utf-8")

    monkeypatch.setattr(resource_sync, "download_file", fake_download)

    asyncio.run(
        write_uploaded_resources(
            tmp_path,
            {"files": [{"filename": "remote.txt", "url": "https://example.com/remote.txt"}]},
        )
    )

    assert calls == [("https://example.com/remote.txt", str(ref_dir / "remote.txt"))]
    assert (ref_dir / "remote.txt").read_text(encoding="utf-8") == "downloaded"
    assert _state(tmp_path)["ref_files"] == [
        {"filename": "remote.txt", "url": "https://example.com/remote.txt"}
    ]


def test_existing_file_without_state_is_written_from_param(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref_dir = tmp_path / "resources" / "ref-files"
    ref_dir.mkdir(parents=True)
    (ref_dir / "remote.txt").write_text("stale", encoding="utf-8")
    calls: list[tuple[str, str]] = []

    async def fake_download(url: str, dest: str) -> None:
        calls.append((url, dest))
        Path(dest).write_text("fresh", encoding="utf-8")

    monkeypatch.setattr(resource_sync, "download_file", fake_download)

    asyncio.run(
        write_uploaded_resources(
            tmp_path,
            {"files": [{"filename": "remote.txt", "url": "https://example.com/remote.txt"}]},
        )
    )

    assert calls == [
        ("https://example.com/remote.txt", str(ref_dir / "remote.txt"))
    ]
    assert (ref_dir / "remote.txt").read_text(encoding="utf-8") == "fresh"


def test_tool_specs_are_synchronized(tmp_path: Path) -> None:
    tools_dir = tmp_path / "resources" / "available-tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "old__tool.json").write_text("{}", encoding="utf-8")
    save_resource_state(
        tmp_path,
        {"tool_specs": [{"filename": "old__tool.json", "pluginId": "old", "toolName": "tool"}]},
    )

    asyncio.run(
        write_uploaded_resources(
            tmp_path,
            {
                "tool_spec_files": [
                    {
                        "pluginId": "new",
                        "toolName": "search",
                        "description": "Search",
                        "arguments": {"type": "object", "properties": {}},
                    }
                ]
            },
        )
    )

    assert not (tools_dir / "old__tool.json").exists()
    assert (tools_dir / "new__search.json").is_file()
    assert _state(tmp_path)["tool_specs"] == [
        {"filename": "new__search.json", "pluginId": "new", "toolName": "search"}
    ]

    asyncio.run(write_uploaded_resources(tmp_path, {}))

    assert not (tools_dir / "new__search.json").exists()
    assert _state(tmp_path)["tool_specs"] == []


def _zip_b64(files: dict[str, str] | None = None) -> str:
    files = files or {"readme.txt": "hello"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_build_current_ref_file_hint_lines_single_and_multiple(tmp_path: Path) -> None:
    ref_dir = tmp_path / "resources" / "ref-files"
    ref_dir.mkdir(parents=True)

    lines = build_current_ref_file_hint_lines(
        tmp_path,
        [
            {"filename": "requirement.md", "base64Data": _b64("# req")},
            {"name": "notes.txt", "base64": _b64("note")},
        ],
    )

    assert lines == [
        f"- requirement.md -> 本地路径: {(ref_dir / 'requirement.md').resolve()}",
        f"- notes.txt -> 本地路径: {(ref_dir / 'notes.txt').resolve()}",
    ]


def test_build_current_ref_file_hint_lines_zip_includes_extract_dir(tmp_path: Path) -> None:
    ref_dir = tmp_path / "resources" / "ref-files"

    lines = build_current_ref_file_hint_lines(
        tmp_path,
        [{"filename": "demo.zip", "base64Data": _zip_b64()}],
    )

    assert lines == [
        f"- demo.zip -> 本地路径: {(ref_dir / 'demo.zip').resolve()}",
        f"  - 解压目录 -> {(ref_dir / 'demo').resolve()}",
    ]


def test_build_current_ref_file_hint_lines_url_zip_extracts_to_ref_dir(tmp_path: Path) -> None:
    ref_dir = tmp_path / "resources" / "ref-files"

    lines = build_current_ref_file_hint_lines(
        tmp_path,
        [{"filename": "remote.zip", "url": "https://example.com/remote.zip"}],
    )

    assert lines == [
        f"- remote.zip -> 本地路径: {(ref_dir / 'remote.zip').resolve()}",
        f"  - 解压目录 -> {ref_dir.resolve()}",
    ]


def test_build_current_ref_file_hint_lines_skips_direct_import_and_empty_payload(
    tmp_path: Path,
) -> None:
    record_direct_imported_skills(tmp_path, [{"filename": "imported.skill"}])

    lines = build_current_ref_file_hint_lines(
        tmp_path,
        [
            {"filename": "imported.skill", "base64Data": _b64("x")},
            {"filename": "empty.txt"},
            "not-a-dict",
        ],
    )

    assert lines == []


def test_build_current_ref_file_hint_lines_image_appends_source_url(tmp_path: Path) -> None:
    ref_dir = tmp_path / "resources" / "ref-files"
    ref_dir.mkdir(parents=True)
    image_url = "https://xiaoyi.example.com/files/222.jpg"

    lines = build_current_ref_file_hint_lines(
        tmp_path,
        [
            {
                "filename": "222.jpg",
                "url": image_url,
                "mime": "image/jpeg",
            }
        ],
    )

    assert lines == [
        f"- 222.jpg -> 本地路径: {(ref_dir / '222.jpg').resolve()} ; 可下载url: {image_url}",
    ]


def test_build_current_ref_file_hint_lines_non_image_does_not_append_url(tmp_path: Path) -> None:
    ref_dir = tmp_path / "resources" / "ref-files"
    ref_dir.mkdir(parents=True)

    lines = build_current_ref_file_hint_lines(
        tmp_path,
        [
            {
                "filename": "notes.txt",
                "url": "https://xiaoyi.example.com/files/notes.txt",
                "mime": "text/plain",
            }
        ],
    )

    assert lines == [f"- notes.txt -> 本地路径: {(ref_dir / 'notes.txt').resolve()}"]


def test_build_resource_hint_header_and_file_lines(tmp_path: Path) -> None:
    ref_dir = tmp_path / "resources" / "ref-files"
    ref_dir.mkdir(parents=True)
    lines = build_current_ref_file_hint_lines(
        tmp_path,
        [{"filename": "requirement.md", "base64Data": _b64("req")}],
    )

    hint = SkillDevDeepAdapter._build_resource_hint(
        tmp_path,
        {"files": [{"filename": "requirement.md", "base64Data": _b64("req")}]},
        "task-1",
        added_file_lines=lines,
        removed_file_lines=[],
    )

    assert "【用户需求】" not in hint
    assert "【工作区信息】" in hint
    assert "【本轮上传资源索引（已落盘，可直接读取）】" in hint
    assert "【本轮已移除资源】" not in hint
    assert "【执行要求】" in hint
    assert "用户上传资源已写入" not in hint
    assert f"- requirement.md -> 本地路径: {(ref_dir / 'requirement.md').resolve()}" in hint


def test_build_current_ref_file_hint_lines_includes_reuploaded_files(tmp_path: Path) -> None:
    ref_dir = tmp_path / "resources" / "ref-files"
    lines = build_current_ref_file_hint_lines(
        tmp_path,
        [{"filename": "a.txt", "base64Data": _b64("same")}],
    )

    assert lines == [f"- a.txt -> 本地路径: {(ref_dir / 'a.txt').resolve()}"]


def test_build_current_ref_file_hint_lines_empty_round_returns_empty(tmp_path: Path) -> None:
    assert build_current_ref_file_hint_lines(tmp_path, []) == []


def test_build_resource_hint_includes_removed_section_when_only_tool_deletions(tmp_path: Path) -> None:
    hint = SkillDevDeepAdapter._build_resource_hint(
        tmp_path,
        {},
        "task-1",
        added_file_lines=[],
        removed_file_lines=[],
        added_skill_lines=[],
        removed_skill_lines=[],
        added_tool_lines=[],
        removed_tool_lines=["- old/tool -> 已移除（用户本轮未再上传）"],
    )

    assert "【工作区信息】" in hint
    assert "【本轮上传资源索引（已落盘，可直接读取）】" not in hint
    assert "【本轮已移除资源】" not in hint
    assert "【本轮已移除可用工具】" in hint
    assert "- old/tool -> 已移除（用户本轮未再上传）" in hint
    assert "【执行要求】" in hint


def test_build_current_ref_skill_hint_lines_current_round_only(tmp_path: Path) -> None:
    ref_dir = tmp_path / "resources" / "ref-skills"
    lines = build_current_ref_skill_hint_lines(
        tmp_path,
        [{"filename": "new.skill", "base64Data": _b64("x")}],
    )

    assert lines == [
        f"- new.skill -> 本地路径: {(ref_dir / 'new.skill').resolve()}",
        f"  - 解压目录 -> {(ref_dir / 'new').resolve()}",
    ]


def test_build_current_ref_skill_hint_lines_includes_reuploaded(tmp_path: Path) -> None:
    ref_dir = tmp_path / "resources" / "ref-skills"
    lines = build_current_ref_skill_hint_lines(
        tmp_path,
        [{"filename": "keep.zip", "base64Data": _zip_b64()}],
    )

    assert lines == [
        f"- keep.zip -> 本地路径: {(ref_dir / 'keep.zip').resolve()}",
        f"  - 解压目录 -> {(ref_dir / 'keep').resolve()}",
    ]


def test_build_current_tool_spec_hint_lines_add_and_remove(tmp_path: Path) -> None:
    tools_dir = tmp_path / "resources" / "available-tools"
    added_lines, removed_lines = build_current_tool_spec_hint_lines(
        tmp_path,
        [{"pluginId": "new", "toolName": "search"}],
        previous_tool_specs=[{"filename": "old__tool.json", "pluginId": "old", "toolName": "tool"}],
    )

    assert added_lines == [f"- new/search -> 本地路径: {(tools_dir / 'new__search.json').resolve()}"]
    assert removed_lines == ["- old/tool -> 已移除（用户本轮未再上传）"]


def test_build_resource_hint_includes_skill_and_tool_sections(tmp_path: Path) -> None:
    hint = SkillDevDeepAdapter._build_resource_hint(
        tmp_path,
        {},
        "task-1",
        added_file_lines=[],
        removed_file_lines=[],
        added_skill_lines=["- demo.skill -> 本地路径: X", "  - 解压目录 -> Y"],
        removed_skill_lines=["- old.skill -> 已移除（用户本轮未再上传）"],
        added_tool_lines=["- a/b -> 本地路径: Z"],
        removed_tool_lines=["- c/d -> 已移除（用户本轮未再上传）"],
    )

    assert "【本轮新增参考 Skill 包】" in hint
    assert "【本轮已移除参考 Skill 包】" in hint
    assert "【本轮新增可用工具】" in hint
    assert "【本轮已移除可用工具】" in hint


def test_delete_plain_file_and_not_found(tmp_path: Path) -> None:
    asyncio.run(
        write_uploaded_resources(
            tmp_path,
            {"files": [{"filename": "spec.pdf", "base64Data": _b64("pdf")}]},
        )
    )
    ref_dir = tmp_path / "resources" / "ref-files"
    assert (ref_dir / "spec.pdf").is_file()

    result = delete_uploaded_resources(
        tmp_path,
        [
            {"type": "file", "filename": "spec.pdf"},
            {"type": "file", "filename": "missing.txt"},
        ],
    )

    assert result["ok"] is True
    assert result["deleted"] == [{"type": "file", "filename": "spec.pdf"}]
    assert result["notFound"] == [{"type": "file", "filename": "missing.txt"}]
    assert result["errors"] == []
    assert not (ref_dir / "spec.pdf").exists()
    assert all(e.get("filename") != "spec.pdf" for e in _state(tmp_path).get("ref_files", []))


def test_delete_zip_with_stem_extract_dir(tmp_path: Path) -> None:
    asyncio.run(
        write_uploaded_resources(
            tmp_path,
            {
                "skill_packages": [
                    {"filename": "ref.zip", "base64Data": _zip_b64({"readme.txt": "hi"})}
                ]
            },
        )
    )
    ref_dir = tmp_path / "resources" / "ref-skills"
    assert (ref_dir / "ref.zip").is_file()
    assert (ref_dir / "ref").is_dir()
    assert (ref_dir / "ref" / "readme.txt").is_file()

    result = delete_uploaded_resources(
        tmp_path,
        [{"type": "skill", "filename": "ref.zip"}],
    )

    assert result["ok"] is True
    assert result["deleted"] == [{"type": "skill", "filename": "ref.zip"}]
    assert result["notFound"] == []
    assert not (ref_dir / "ref.zip").exists()
    assert not (ref_dir / "ref").exists()


def test_delete_zip_flat_extracted_members(tmp_path: Path) -> None:
    """URL-style extract (members at dest root) is cleaned via zip namelist."""
    ref_dir = tmp_path / "resources" / "ref-files"
    ref_dir.mkdir(parents=True)
    zip_bytes = base64.b64decode(_zip_b64({"flat.txt": "flat-content", "subdir/a.txt": "a"}))
    archive = ref_dir / "bundle.zip"
    archive.write_bytes(zip_bytes)
    (ref_dir / "flat.txt").write_text("flat-content", encoding="utf-8")
    sub = ref_dir / "subdir"
    sub.mkdir()
    (sub / "a.txt").write_text("a", encoding="utf-8")
    # Unrelated file must survive
    (ref_dir / "keep.txt").write_text("keep", encoding="utf-8")
    save_resource_state(
        tmp_path,
        {"ref_files": [{"filename": "bundle.zip", "url": "https://example.com/bundle.zip"}]},
    )

    result = delete_uploaded_resources(
        tmp_path,
        [{"type": "file", "filename": "bundle.zip"}],
    )

    assert result["ok"] is True
    assert result["deleted"] == [{"type": "file", "filename": "bundle.zip"}]
    assert not archive.exists()
    assert not (ref_dir / "flat.txt").exists()
    assert not (sub / "a.txt").exists()
    assert (ref_dir / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_delete_tool_agent_cli_definitions(tmp_path: Path) -> None:
    asyncio.run(
        write_uploaded_resources(
            tmp_path,
            {
                "tool_spec_files": [
                    {"pluginId": "bundle.a", "toolName": "do_x", "description": "x"},
                    {"pluginId": "bundle.b", "toolName": "do_y", "description": "y"},
                ],
                "agent_definitions": [
                    {"agentId": "reviewer", "name": "Reviewer"},
                    {"agentId": "writer", "name": "Writer"},
                ],
                "cli_definitions": [
                    {"name": "my-cli", "command": "echo"},
                    {"name": "other-cli", "command": "ls"},
                ],
            },
        )
    )
    tools_dir = tmp_path / "resources" / "available-tools"
    assert (tools_dir / "bundle.a__do_x.json").is_file()

    result = delete_uploaded_resources(
        tmp_path,
        [
            {"type": "toolDefinition", "pluginId": "bundle.a", "toolName": "do_x"},
            {"type": "agentDefinition", "agentId": "reviewer"},
            {"type": "cliDefinition", "name": "my-cli"},
            {"type": "toolDefinition", "pluginId": "missing", "toolName": "nope"},
        ],
    )

    assert result["ok"] is True
    assert {"type": "toolDefinition", "pluginId": "bundle.a", "toolName": "do_x"} in result[
        "deleted"
    ]
    assert {"type": "agentDefinition", "agentId": "reviewer"} in result["deleted"]
    assert {"type": "cliDefinition", "name": "my-cli"} in result["deleted"]
    assert result["notFound"] == [
        {"type": "toolDefinition", "pluginId": "missing", "toolName": "nope"}
    ]
    assert not (tools_dir / "bundle.a__do_x.json").exists()
    assert (tools_dir / "bundle.b__do_y.json").is_file()

    agents = json.loads(
        (tmp_path / "resources" / "agents" / "available_agents.json").read_text(
            encoding="utf-8"
        )
    )
    assert [a["agentId"] for a in agents] == ["writer"]

    clis = json.loads(
        (tmp_path / "resources" / "clis" / "available_clis.json").read_text(encoding="utf-8")
    )
    assert [c["name"] for c in clis] == ["other-cli"]

    tool_specs = _state(tmp_path).get("tool_specs", [])
    assert all(
        not (e.get("pluginId") == "bundle.a" and e.get("toolName") == "do_x")
        for e in tool_specs
    )


def test_delete_illegal_filename_is_error(tmp_path: Path) -> None:
    result = delete_uploaded_resources(
        tmp_path,
        [{"type": "file", "filename": "../escape.txt"}],
    )
    assert result["ok"] is False
    assert result["deleted"] == []
    assert result["notFound"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["type"] == "file"
    assert "illegal" in result["errors"][0]["error"]


def test_delete_batch_mixed_ok_with_partial_not_found(tmp_path: Path) -> None:
    asyncio.run(
        write_uploaded_resources(
            tmp_path,
            {"files": [{"filename": "a.txt", "base64Data": _b64("a")}]},
        )
    )
    result = delete_uploaded_resources(
        tmp_path,
        [
            {"type": "file", "filename": "a.txt"},
            {"type": "skill", "filename": "gone.zip"},
        ],
    )
    assert result["ok"] is True
    assert result["deleted"] == [{"type": "file", "filename": "a.txt"}]
    assert result["notFound"] == [{"type": "skill", "filename": "gone.zip"}]
    assert result["errors"] == []
