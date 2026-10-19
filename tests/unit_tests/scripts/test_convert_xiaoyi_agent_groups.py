# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package_bundle
from scripts import convert_xiaoyi_agent_groups as converter


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_avatar(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (512, 512), color).save(path, format="PNG")


def _write_source_group(
    parent: Path,
    package_id: str,
    *,
    team_avatar: bool,
) -> Path:
    group = parent / package_id
    _write_json(
        group / "manifest.json",
        {
            "schemaVersion": "0.0.1",
            "name": package_id,
            "package_type": "agent_group",
            "instruction": "团长在小艺 Work 中按任务选择最少必要成员。",
            "agents": ["leader", "member"],
            "skills": [],
            "metadata": {
                "categoryId": "Life",
                "description": "在小艺 Work 中把需求变成可执行方案。",
                "displayName": "生活规划专家团",
                "dispatchContract": "xiaoyi.expert-team.dynamic-scheduled.v1",
                "quickPrompts": ["帮我规划一下", "给我一个执行清单"],
                "routingPolicy": {"selection": "minimal_sufficient"},
                "tags": ["生活", "规划"],
            },
        },
    )
    for member_id, name, description in (
        ("leader", "顾主理", "生活规划主理人"),
        ("member", "程规划", "执行规划师"),
    ):
        _write_json(
            group / "agents" / member_id / "manifest.json",
            {
                "schemaVersion": "0.0.1",
                "packageType": "agent_template",
                "name": name,
                "description": description,
                "agentCard": {
                    "id": member_id,
                    "name": name,
                    "description": description,
                },
                "metadata": {
                    "categoryId": "Life",
                    "deliverables": ["方案"],
                    "quickPrompts": [f"让{name}处理"],
                    "tags": ["规划"],
                },
                "persona": {"dir": "persona"},
                "skills": (
                    []
                    if member_id == "leader"
                    else [{"dir": "skills/sample", "mode": "all"}]
                ),
            },
        )
        persona = group / "agents" / member_id / "persona" / "ROLE.md"
        persona.parent.mkdir(parents=True)
        persona.write_text(f"# {name}\n", encoding="utf-8")
        avatar = group / "avatars" / f"{member_id}.png"
        _write_avatar(
            avatar,
            (38, 107, 72) if member_id == "leader" else (70, 112, 180),
        )
    (group / "agents" / "leader" / "AGENT.md").write_text(
        "# Leader\n\n请按小艺 Work 内嵌预览约束检查页面。\n",
        encoding="utf-8",
    )
    skill = group / "agents" / "member" / "skills" / "sample" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: sample\ndescription: sample skill\n---\n# Sample\n",
        encoding="utf-8",
    )
    (group / "README.md").write_text("# XiaoYi source\n", encoding="utf-8")
    (group / ".DS_Store").write_bytes(b"junk")
    if team_avatar:
        _write_avatar(group / "avatars" / "expert.png", (188, 96, 70))
        manifest = json.loads((group / "manifest.json").read_text(encoding="utf-8"))
        manifest["metadata"]["avatar"] = "avatars/expert.png"
        _write_json(group / "manifest.json", manifest)
    return group


def _zip_tree(source: Path, output: Path, *, include_root: bool) -> None:
    with zipfile.ZipFile(output, "w") as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                relative = path.relative_to(source.parent if include_root else source)
                archive.write(path, relative.as_posix())


def test_converts_nested_and_direct_deliveries_to_workswarm_packages(
    tmp_path: Path,
) -> None:
    nested_group = _write_source_group(
        tmp_path / "nested-source",
        "nested-team",
        team_avatar=False,
    )
    nested_team_zip = tmp_path / "nested-team.zip"
    _zip_tree(nested_group, nested_team_zip, include_root=True)
    wrapper = tmp_path / "wrapper.zip"
    with zipfile.ZipFile(wrapper, "w") as archive:
        archive.write(nested_team_zip, nested_team_zip.name)

    direct_bundle = tmp_path / "direct-bundle"
    direct_group = _write_source_group(
        direct_bundle,
        "direct-team",
        team_avatar=True,
    )
    (direct_bundle / "__MACOSX").mkdir()
    (direct_bundle / "__MACOSX" / "._manifest.json").write_bytes(b"junk")
    (direct_bundle / "头像").mkdir()
    (direct_bundle / "头像" / "运营头像.png").write_bytes(b"unrelated")
    direct_zip = tmp_path / "direct.zip"
    _zip_tree(direct_bundle, direct_zip, include_root=False)

    output = tmp_path / "output"
    report = converter.convert_sources(
        [wrapper, direct_zip],
        output_dir=output,
    )

    assert report["groupCount"] == 2
    assert report["skillsModified"] is False
    assert {item["id"] for item in report["groups"]} == {
        "direct-team",
        "nested-team",
    }
    for package_id in ("direct-team", "nested-team"):
        archive_path = output / f"{package_id}.zip"
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            assert names
            assert all(name.startswith(f"{package_id}/") for name in names)
            assert not any("__MACOSX" in name or ".DS_Store" in name for name in names)
            extract_root = tmp_path / "extract" / package_id
            archive.extractall(extract_root)
        package_root = extract_root / package_id
        manifest = json.loads(
            (package_root / "manifest.json").read_text(encoding="utf-8")
        )
        member = json.loads(
            (package_root / "agents" / "member" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["version"] == "1.0.0"
        assert manifest["package_type"] == "agent_group"
        assert manifest["display_name"] == {
            "zh": "生活规划专家团",
            "en": "生活规划专家团",
        }
        assert manifest["tags"] == [
            {"zh": "生活", "en": "生活"},
            {"zh": "规划", "en": "规划"},
        ]
        assert len(manifest["quick_inputs"]) == 2
        assert "WorkSwarm" in manifest["instruction"]
        assert "小艺 Work" not in json.dumps(manifest, ensure_ascii=False)
        assert "metadata" not in manifest and "schemaVersion" not in manifest
        assert member["package_type"] == "agent_template"
        assert member["version"] == "1.0.0"
        assert member["avatar"] == "avatar.webp"
        assert "packageType" not in member and "agentCard" not in member
        optimized_avatar = package_root / "agents" / "member" / "avatar.webp"
        assert optimized_avatar.is_file()
        with Image.open(optimized_avatar) as image:
            assert max(image.size) <= 256
        if package_id == "direct-team":
            assert manifest["avatar"] == "avatar.webp"
            assert (package_root / "avatar.webp").is_file()
        assert "WorkSwarm AgentGroup" in (package_root / "README.md").read_text(
            encoding="utf-8"
        )
        leader_instruction = (
            package_root / "agents" / "leader" / "AGENT.md"
        ).read_text(encoding="utf-8")
        assert "小艺 Work 内嵌预览" in leader_instruction
        readme = (package_root / "README.md").read_text(encoding="utf-8")
        assert "## 来源包原始说明" in readme
        assert "# XiaoYi source" in readme
        assert (
            package_root / "agents" / "member" / "skills" / "sample" / "SKILL.md"
        ).read_bytes() == (
            direct_group / "agents" / "member" / "skills" / "sample" / "SKILL.md"
        ).read_bytes()
        loaded = load_agent_group_package_bundle(package_root)
        assert set(loaded.templates) == {"leader", "member"}
    assert (
        json.loads((output / "conversion-report.json").read_text(encoding="utf-8"))[
            "groupCount"
        ]
        == 2
    )
    assert report["groups"][0]["removedMetadataKeys"]


def test_directory_input_is_deterministic_and_requires_overwrite(
    tmp_path: Path,
) -> None:
    group = _write_source_group(
        tmp_path / "source", "directory-team", team_avatar=False
    )
    output = tmp_path / "output"

    converter.convert_sources([group], output_dir=output)
    first = (output / "directory-team.zip").read_bytes()

    with pytest.raises(converter.ConversionError, match="output already exists"):
        converter.convert_sources([group], output_dir=output)

    converter.convert_sources([group], output_dir=output, overwrite=True)
    assert (output / "directory-team.zip").read_bytes() == first


def test_preserves_supported_svg_avatars(tmp_path: Path) -> None:
    group = _write_source_group(tmp_path / "source", "svg-team", team_avatar=False)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
        '<rect width="64" height="64" fill="#2563eb"/></svg>'
    ).encode()
    (group / "avatar.svg").write_bytes(svg)
    root_manifest_path = group / "manifest.json"
    root_manifest = json.loads(root_manifest_path.read_text(encoding="utf-8"))
    root_manifest["avatar"] = "avatar.svg"
    _write_json(root_manifest_path, root_manifest)

    (group / "avatars" / "member.png").unlink()
    member_dir = group / "agents" / "member"
    (member_dir / "avatar.svg").write_bytes(svg)
    member_manifest_path = member_dir / "manifest.json"
    member_manifest = json.loads(member_manifest_path.read_text(encoding="utf-8"))
    member_manifest["avatar"] = "avatar.svg"
    _write_json(member_manifest_path, member_manifest)

    output = tmp_path / "output"
    report = converter.convert_sources([group], output_dir=output)
    with zipfile.ZipFile(output / "svg-team.zip") as archive:
        archive.extractall(tmp_path / "extract")
    package_root = tmp_path / "extract" / "svg-team"
    converted_root = json.loads(
        (package_root / "manifest.json").read_text(encoding="utf-8")
    )
    converted_member = json.loads(
        (package_root / "agents" / "member" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert converted_root["avatar"] == "avatar.svg"
    assert converted_member["avatar"] == "avatar.svg"
    assert (package_root / "avatar.svg").read_bytes() == svg
    assert (package_root / "agents" / "member" / "avatar.svg").read_bytes() == svg
    assert report["groups"][0]["avatarOptimization"]["format"] == "SVG"
    load_agent_group_package_bundle(package_root)


def test_avatar_pixel_budget_and_decompression_bomb_are_conversion_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "avatar.png"
    source.write_bytes(b"placeholder")

    class OversizedImage:
        size = (converter._AVATAR_MAX_PIXELS + 1, 1)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(converter.Image, "open", lambda _path: OversizedImage())
    with pytest.raises(converter.ConversionError, match="pixel budget"):
        converter._optimized_avatar(source, destination=tmp_path / "avatar.webp")

    def raise_bomb(_path):
        raise Image.DecompressionBombError("unsafe dimensions")

    monkeypatch.setattr(converter.Image, "open", raise_bomb)
    with pytest.raises(converter.ConversionError, match="invalid avatar image"):
        converter._optimized_avatar(source, destination=tmp_path / "avatar.webp")


def test_palette_avatar_transparency_is_preserved(tmp_path: Path) -> None:
    source = tmp_path / "palette.png"
    image = Image.new("P", (8, 8), 0)
    palette = [0, 0, 0, 255, 0, 0] + [0, 0, 0] * 254
    image.putpalette(palette)
    image.info["transparency"] = 0
    image.save(source, format="PNG", transparency=0)

    destination = tmp_path / "avatar.webp"
    converter._optimized_avatar(source, destination=destination)

    with Image.open(destination) as converted:
        assert converted.mode == "RGBA"
        assert converted.getchannel("A").getextrema()[0] == 0


def test_rejects_archive_path_traversal(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("../escape.txt", "unsafe")

    with pytest.raises(converter.ConversionError, match="escapes extraction root"):
        converter.convert_sources([source], output_dir=tmp_path / "output")


@pytest.mark.parametrize(
    "member_name",
    [r"C:\\absolute.txt", "C:drive-relative.txt", r"\\server\\share.txt"],
)
def test_rejects_windows_drive_and_absolute_archive_paths(
    tmp_path: Path,
    member_name: str,
) -> None:
    source = tmp_path / "unsafe-windows.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(member_name, "unsafe")

    with pytest.raises(converter.ConversionError, match="escapes extraction root"):
        converter._safe_extract_zip(source, tmp_path / "extract")


def test_safe_extract_streams_and_enforces_archive_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "bounded.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("a.txt", "abc")
        archive.writestr("b.txt", "def")

    def reject_whole_member_read(*_args, **_kwargs):
        raise AssertionError("ZipFile.read must not be used for extraction")

    monkeypatch.setattr(zipfile.ZipFile, "read", reject_whole_member_read)
    converter._safe_extract_zip(source, tmp_path / "streamed")
    assert (tmp_path / "streamed" / "a.txt").read_text() == "abc"

    monkeypatch.setattr(converter, "_MAX_ARCHIVE_FILE_COUNT", 1)
    with pytest.raises(converter.ConversionError, match="too many files"):
        converter._safe_extract_zip(source, tmp_path / "too-many")


def test_safe_extract_rejects_single_total_and_path_depth_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    single = tmp_path / "single.zip"
    with zipfile.ZipFile(single, "w") as archive:
        archive.writestr("large.txt", "1234")
    monkeypatch.setattr(converter, "_MAX_ARCHIVE_SINGLE_FILE_BYTES", 3)
    with pytest.raises(converter.ConversionError, match="member is too large"):
        converter._safe_extract_zip(single, tmp_path / "single-out")

    monkeypatch.setattr(converter, "_MAX_ARCHIVE_SINGLE_FILE_BYTES", 64)
    monkeypatch.setattr(converter, "_MAX_ARCHIVE_TOTAL_BYTES", 5)
    total = tmp_path / "total.zip"
    with zipfile.ZipFile(total, "w") as archive:
        archive.writestr("one.txt", "123")
        archive.writestr("two.txt", "456")
    with pytest.raises(converter.ConversionError, match="uncompressed size"):
        converter._safe_extract_zip(total, tmp_path / "total-out")

    monkeypatch.setattr(converter, "_MAX_ARCHIVE_TOTAL_BYTES", 512)
    monkeypatch.setattr(converter, "_MAX_ARCHIVE_PATH_DEPTH", 2)
    deep = tmp_path / "deep.zip"
    with zipfile.ZipFile(deep, "w") as archive:
        archive.writestr("one/two/three.txt", "ok")
    with pytest.raises(converter.ConversionError, match="path is too deep"):
        converter._safe_extract_zip(deep, tmp_path / "deep-out")


def test_preserves_shared_skill_mapping_semantics(tmp_path: Path) -> None:
    group = _write_source_group(tmp_path / "source", "shared-team", team_avatar=False)
    shared_skill = group / "skills" / "shared" / "SKILL.md"
    shared_skill.parent.mkdir(parents=True)
    shared_skill.write_text(
        "---\nname: shared\ndescription: shared skill\n---\n# Shared\n",
        encoding="utf-8",
    )
    manifest_path = group / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"] = [{"dir": "skills/shared", "mode": "all"}]
    _write_json(manifest_path, manifest)

    output = tmp_path / "output"
    converter.convert_sources([group], output_dir=output)
    with zipfile.ZipFile(output / "shared-team.zip") as archive:
        archive.extractall(tmp_path / "extract-shared")
    converted = json.loads(
        (tmp_path / "extract-shared" / "shared-team" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert converted["skills"] == [{"dir": "skills/shared", "mode": "all"}]


def test_rejects_duplicate_group_ids_across_inputs(tmp_path: Path) -> None:
    first = _write_source_group(tmp_path / "first", "duplicate-team", team_avatar=False)
    second = _write_source_group(
        tmp_path / "second", "duplicate-team", team_avatar=True
    )

    with pytest.raises(converter.ConversionError, match="duplicate AgentGroup name"):
        converter.convert_sources([first, second], output_dir=tmp_path / "output")
