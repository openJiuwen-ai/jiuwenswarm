# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import hashlib
import zipfile

import pytest

from jiuwenswarm.server.runtime.marketplace.asset_package_builder import (
    PackageBuildError,
    PackageLimits,
    build_asset_package,
    collect_publish_files,
)
from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishIdentity


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: Test skill\nversion: 1.0.0\n---\n# Test\n"
    )
    return root


def test_excludes_credentials_runtime_and_outputs_preserves_template(source):
    for name in [".env", ".env.local", "id_rsa", "credentials.json", "token.json"]:
        (source / name).write_text("synthetic-secret")
    for name in ["out", ".archive", ".git", "__pycache__"]:
        (source / name).mkdir()
        (source / name / "payload.zip").write_bytes(b"output")
    (source / ".env.example").write_text("API_TOKEN=${API_TOKEN}")
    files, excluded = collect_publish_files(source)
    assert {p.relative_to(source).as_posix() for p in files} == {
        "SKILL.md",
        ".env.example",
    }
    assert ".env" in excluded and "out/" in excluded


def test_rejects_symlink_to_external_file(source, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("outside")
    (source / "link").symlink_to(secret)
    with pytest.raises(PackageBuildError, match="UNSAFE_PATH"):
        collect_publish_files(source)


def test_deterministic_skill_snapshot_without_source_mutation(source, tmp_path):
    original = (source / "SKILL.md").read_bytes()
    options = dict(
        source=source,
        identity=PublishIdentity("skill", "test-skill", "2.0.0"),
        metadata={"description": "New description", "tags": ["test"]},
    )
    a = build_asset_package(**options, output_dir=tmp_path / "a")
    b = build_asset_package(**options, output_dir=tmp_path / "b")
    assert (
        a.artifact_sha256
        == b.artifact_sha256
        == hashlib.sha256(a.artifact_path.read_bytes()).hexdigest()
    )
    assert (source / "SKILL.md").read_bytes() == original
    with zipfile.ZipFile(a.artifact_path) as archive:
        assert set(archive.namelist()) == {
            "test-skill/plugin.yaml",
            "test-skill/test-skill/SKILL.md",
        }
        assert b"2.0.0" in archive.read("test-skill/test-skill/SKILL.md")
        assert b"New description" in archive.read("test-skill/plugin.yaml")
    assert not list((tmp_path / "a").glob("*/snapshot"))


def test_output_must_be_outside_source(source):
    with pytest.raises(PackageBuildError, match="OUTPUT_INSIDE_SOURCE"):
        build_asset_package(
            source,
            PublishIdentity("skill", "test-skill", "1.0.0"),
            {},
            source / "output",
        )


@pytest.mark.parametrize(
    "limits",
    [
        PackageLimits(max_files=0),
        PackageLimits(file_bytes=8),
        PackageLimits(total_bytes=8),
        PackageLimits(max_depth=0),
    ],
)
def test_resource_limits_reject_before_publishing(source, tmp_path, limits):
    with pytest.raises(PackageBuildError, match="LIMIT_EXCEEDED"):
        build_asset_package(
            source,
            PublishIdentity("skill", "test-skill", "1.0.0"),
            {},
            tmp_path / "out",
            limits=limits,
        )


def test_source_change_during_normalization_is_rejected(source, tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.marketplace import asset_publish_adapters

    original = asset_publish_adapters.normalize_and_validate

    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        (source / "new-file.txt").write_text("concurrent change")
        return result

    monkeypatch.setattr(asset_publish_adapters, "normalize_and_validate", changing)
    with pytest.raises(PackageBuildError, match="SOURCE_CHANGED"):
        build_asset_package(
            source,
            PublishIdentity("skill", "test-skill", "1.0.0"),
            {},
            tmp_path / "out",
        )
    assert not list((tmp_path / "out").rglob("*.zip"))


@pytest.mark.parametrize(
    "line",
    [
        "API_TOKEN=synthetic-private-value",
        "DATABASE_URL=postgresql://user:synthetic-password@example.com/db",
        "SERVICE_URL=https://example.com?token=synthetic-secret",
    ],
)
def test_rejects_literal_secret_in_environment_template(source, tmp_path, line):
    (source / ".env.example").write_text(line + "\n")
    with pytest.raises(PackageBuildError, match="CREDENTIAL_VALUE"):
        build_asset_package(
            source,
            PublishIdentity("skill", "test-skill", "1.0.0"),
            {},
            tmp_path / "out",
        )


def test_explicit_output_exclusion_keeps_other_business_files(source, tmp_path):
    output = source / "dist" / "result.zip"
    output.parent.mkdir()
    output.write_bytes(b"old artifact")
    (output.parent / "note.txt").write_text("business")
    package = build_asset_package(
        source,
        PublishIdentity("skill", "test-skill", "1.0.0"),
        {},
        tmp_path / "out",
        exclude_paths=(output,),
    )
    with zipfile.ZipFile(package.artifact_path) as archive:
        assert "test-skill/test-skill/dist/note.txt" in archive.namelist()
        assert not any(name.endswith("result.zip") for name in archive.namelist())


def test_standard_temporary_source_with_system_symlink_ancestors(tmp_path):
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary)
        (source / "SKILL.md").write_text(
            "---\nname: demo\ndescription: example\n---\nbody\n"
        )
        package = build_asset_package(
            source, PublishIdentity("skill", "demo", "1.0.0"), {}, tmp_path / "out"
        )
        assert package.artifact_path.is_file()


def test_preserves_executable_scripts_without_other_permission_bits(source, tmp_path):
    script = source / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o775)
    package = build_asset_package(
        source, PublishIdentity("skill", "test-skill", "1.0.0"), {}, tmp_path / "out"
    )
    with zipfile.ZipFile(package.artifact_path) as archive:
        assert (
            archive.getinfo("test-skill/test-skill/run.sh").external_attr >> 16
        ) & 0o777 == 0o755
