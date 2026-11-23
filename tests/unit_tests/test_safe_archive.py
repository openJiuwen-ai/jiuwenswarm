# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""安全归档公共层单测：收集过滤、环境模板扫描、确定性打包、安全解包。"""

from __future__ import annotations

import hashlib
import stat
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.common.safe_archive import (
    ALLOWED_ENV_TEMPLATES,
    EXCLUDED_DIRS,
    EXCLUDED_FILES,
    ArchiveLimits,
    SafeArchiveError,
    collect_archive_files,
    copy_files_digest,
    extract_archive_safely,
    pack_directory_single_root,
    validate_archive_root_name,
    validate_environment_template,
    write_zip_entries,
)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "SKILL.md").write_text("# demo\n")
    return root


# ---------------------------------------------------------------- 收集与过滤


def test_collect_excludes_redundant_dirs_and_sensitive_files(source):
    # 显式字面量而非引用被测常量：集合收缩（漏掉 .git / token.json 等）时
    # 本用例必须失败，剔除规则才有回归兜底。
    assert EXCLUDED_DIRS == {
        ".git",
        ".archive",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        "out",
        ".venv",
    }
    assert EXCLUDED_FILES == {
        "id_rsa",
        "id_ed25519",
        "credentials.json",
        "token.json",
        "tokens.json",
        ".DS_Store",
    }
    sensitive_files = (
        "id_rsa",
        "id_ed25519",
        "credentials.json",
        "token.json",
        "tokens.json",
        ".DS_Store",
    )
    for name in sensitive_files:
        (source / name).write_text("synthetic")
    for suffix in (".key", ".p12", ".pfx"):
        (source / f"item{suffix}").write_bytes(b"synthetic")
    (source / ".env").write_text("A=1")
    (source / ".env.local").write_text("A=1")
    for name in (".git", ".archive", "__pycache__", "node_modules", "out", ".venv"):
        (source / name).mkdir()
        (source / name / "payload.bin").write_bytes(b"x")
    (source / ".env.example").write_text("API_TOKEN=${API_TOKEN}")

    files, excluded = collect_archive_files(source)

    assert {p.relative_to(source).as_posix() for p in files} == {
        "SKILL.md",
        ".env.example",
    }
    assert ".env" in excluded and ".env.local" in excluded
    assert "item.key" in excluded and "item.p12" in excluded and "item.pfx" in excluded
    for name in sensitive_files:
        assert name in excluded
    for name in (".git", ".archive", "__pycache__", "node_modules", "out", ".venv"):
        assert f"{name}/" in excluded


def test_collect_exclude_paths_only_drops_listed(source):
    dist = source / "dist"
    dist.mkdir()
    artifact = dist / "result.zip"
    artifact.write_bytes(b"old artifact")
    (dist / "note.txt").write_text("business")

    files, excluded = collect_archive_files(source, exclude_paths=(artifact,))

    assert {p.relative_to(source).as_posix() for p in files} == {
        "SKILL.md",
        "dist/note.txt",
    }
    assert "dist/result.zip" in excluded


def test_collect_rejects_symlinked_file(source, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("x")
    (source / "link").symlink_to(outside)
    with pytest.raises(SafeArchiveError, match="UNSAFE_PATH"):
        collect_archive_files(source)


def test_collect_rejects_symlinked_dir(source, tmp_path):
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (source / "dirlink").symlink_to(outside)
    with pytest.raises(SafeArchiveError, match="UNSAFE_PATH"):
        collect_archive_files(source)


@pytest.mark.parametrize(
    "limits",
    [
        ArchiveLimits(max_files=0),
        ArchiveLimits(file_bytes=1),
        ArchiveLimits(total_bytes=1),
        ArchiveLimits(max_depth=0),
    ],
)
def test_collect_limits(source, limits):
    with pytest.raises(SafeArchiveError, match="LIMIT_EXCEEDED"):
        collect_archive_files(source, limits=limits)


# ------------------------------------------------------------ 顶层目录名校验


@pytest.mark.parametrize("name", ["demo-skill", "a.b_c-1", "专家团"])
def test_root_name_accepted(name):
    assert validate_archive_root_name(name) == name


@pytest.mark.parametrize("name", ["", "   ", ".", "..", "a/b", "a\\b", "a\x01b"])
def test_root_name_rejected(name):
    with pytest.raises(SafeArchiveError, match="UNSAFE_PATH"):
        validate_archive_root_name(name)


# ------------------------------------------------------------ 环境模板凭证扫描


@pytest.mark.parametrize(
    "line",
    [
        "API_TOKEN=synthetic-private-value",
        "DATABASE_URL=postgresql://user:synthetic-password@example.com/db",
        "SERVICE_URL=https://example.com?token=synthetic-secret",
    ],
)
def test_env_template_rejects_literal_secret(tmp_path, line):
    template = tmp_path / ".env.example"
    template.write_text(line + "\n")
    with pytest.raises(SafeArchiveError, match="CREDENTIAL_VALUE"):
        validate_environment_template(template)


@pytest.mark.parametrize(
    "line",
    [
        "API_TOKEN=${API_TOKEN}",
        "AUTHORIZATION=Bearer ${AUTH_TOKEN}",
        "# token=inside-comment",
        "PLAIN=value",
        "SERVICE_URL=https://example.com?token=${TOKEN}",
    ],
)
def test_env_template_allows_placeholder(tmp_path, line):
    template = tmp_path / ".env.example"
    template.write_text(line + "\n")
    validate_environment_template(template)


def test_env_templates_are_the_only_allowed_dotenv_names():
    assert ALLOWED_ENV_TEMPLATES == {".env.example", ".env.template"}


# ------------------------------------------------------------------ 复制与摘要


def test_copy_files_digest_with_optional_destination(source, tmp_path):
    files, _ = collect_archive_files(source)
    digest = copy_files_digest(source, files, ArchiveLimits(), tmp_path / "copy")
    assert (tmp_path / "copy" / "SKILL.md").read_text() == "# demo\n"
    assert copy_files_digest(source, files, ArchiveLimits()) == digest


def test_copy_files_digest_rejects_vanished_source(source):
    """收集与复制之间源文件消失：收敛为 UNSAFE_PATH 而非裸 OSError。"""
    files, _ = collect_archive_files(source)
    (source / "SKILL.md").unlink()
    with pytest.raises(SafeArchiveError, match="UNSAFE_PATH"):
        copy_files_digest(source, files, ArchiveLimits())


# ---------------------------------------------------------------------- 打包


def test_pack_single_root_layout_and_digest(source, tmp_path):
    scripts = source / "scripts"
    scripts.mkdir()
    run = scripts / "run.sh"
    run.write_text("#!/bin/sh\n")
    run.chmod(0o755)

    zip_path = tmp_path / "out" / "demo.zip"
    zip_path.parent.mkdir()
    packed = pack_directory_single_root(source, zip_path, root_name="my-expert")

    assert packed.root_name == "my-expert"
    assert packed.files == ("my-expert/SKILL.md", "my-expert/scripts/run.sh")
    assert packed.excluded == ()
    assert zip_path.is_file()
    assert packed.artifact.size_bytes == zip_path.stat().st_size
    assert packed.artifact.sha256 == hashlib.sha256(zip_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == list(packed.files)


def test_pack_is_deterministic_and_defaults_root_to_dir_name(source, tmp_path):
    first = pack_directory_single_root(source, tmp_path / "a.zip")
    second = pack_directory_single_root(source, tmp_path / "b.zip")
    assert first.root_name == "source"
    assert first.artifact.sha256 == second.artifact.sha256
    assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()


def test_pack_filters_secrets_and_reports_excluded(source, tmp_path):
    (source / ".env").write_text("TOKEN=synthetic")
    (source / "id_rsa").write_text("synthetic")

    packed = pack_directory_single_root(source, tmp_path / "a.zip")

    assert packed.files == ("source/SKILL.md",)
    assert packed.excluded == (".env", "id_rsa")
    with zipfile.ZipFile(tmp_path / "a.zip") as archive:
        assert archive.namelist() == ["source/SKILL.md"]


def test_pack_rejects_empty_source(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SafeArchiveError, match="EMPTY_SOURCE"):
        pack_directory_single_root(empty, tmp_path / "e.zip")


def test_pack_rejects_bad_root_name(source, tmp_path):
    with pytest.raises(SafeArchiveError, match="UNSAFE_PATH"):
        pack_directory_single_root(source, tmp_path / "a.zip", root_name="../evil")


def test_pack_rejects_symlinked_source(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "f.txt").write_text("x")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(SafeArchiveError, match="UNSAFE_PATH"):
        pack_directory_single_root(link, tmp_path / "a.zip")


def test_pack_rejects_credential_in_env_template(source, tmp_path):
    (source / ".env.example").write_text("API_TOKEN=synthetic-private-value\n")
    with pytest.raises(SafeArchiveError, match="CREDENTIAL_VALUE"):
        pack_directory_single_root(source, tmp_path / "a.zip")


def test_pack_rejects_non_utf8_env_template(source, tmp_path):
    """模板非 UTF-8：读取失败收敛为 INVALID_PACKAGE 而非裸 UnicodeDecodeError。"""
    (source / ".env.example").write_bytes(b"API_TOKEN=\xff\xfe\x00binary\n")
    with pytest.raises(SafeArchiveError, match="INVALID_PACKAGE"):
        pack_directory_single_root(source, tmp_path / "a.zip")


def test_write_zip_entries_mixed_files_and_bytes(source, tmp_path):
    entries = {
        "root/plugin.yaml": b"key: value\n",
        "root/a.txt": source / "SKILL.md",
    }
    artifact = write_zip_entries(entries, tmp_path / "m.zip")
    with zipfile.ZipFile(tmp_path / "m.zip") as archive:
        assert archive.namelist() == ["root/a.txt", "root/plugin.yaml"]
        assert archive.read("root/plugin.yaml") == b"key: value\n"
        assert archive.read("root/a.txt") == b"# demo\n"
    assert artifact.sha256 == hashlib.sha256((tmp_path / "m.zip").read_bytes()).hexdigest()
    assert artifact.size_bytes == (tmp_path / "m.zip").stat().st_size


@pytest.mark.parametrize(
    ("entries", "limits"),
    [
        ({"root/a.txt": b"0123456789"}, ArchiveLimits(file_bytes=4)),
        ({"root/a.txt": b"0123456789"}, ArchiveLimits(total_bytes=4)),
        ({"root/a.txt": b"x", "root/b.txt": b"y"}, ArchiveLimits(max_files=1)),
        ({"root/a/b/c.txt": b"x"}, ArchiveLimits(max_depth=2)),
        ({"root/a.txt": b"0123456789" * 100}, ArchiveLimits(zip_bytes=10)),
    ],
)
def test_write_zip_entries_limits_cleanup_partial_zip(tmp_path, entries, limits):
    zip_path = tmp_path / "l.zip"
    with pytest.raises(SafeArchiveError, match="LIMIT_EXCEEDED"):
        write_zip_entries(entries, zip_path, limits=limits)
    assert not zip_path.exists()


# ---------------------------------------------------------------------- 解包


def test_extract_roundtrip_preserves_content_and_exec_mode(source, tmp_path):
    scripts = source / "scripts"
    scripts.mkdir()
    run = scripts / "run.sh"
    run.write_text("#!/bin/sh\n")
    run.chmod(0o755)
    zip_path = tmp_path / "p.zip"
    pack_directory_single_root(source, zip_path, root_name="demo")

    dest = tmp_path / "dest"
    extracted = extract_archive_safely(zip_path, dest)

    assert extracted.root_name == "demo"
    assert extracted.files == ("demo/SKILL.md", "demo/scripts/run.sh")
    assert (dest / "demo" / "SKILL.md").read_text() == "# demo\n"
    assert (dest / "demo" / "scripts" / "run.sh").stat().st_mode & 0o777 == 0o755
    assert extracted.total_bytes == len("# demo\n") + len("#!/bin/sh\n")


def test_extract_multiple_top_level_has_no_root_name(tmp_path):
    zip_path = tmp_path / "multi.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("a/f.txt", "1")
        archive.writestr("b/f.txt", "2")
    extracted = extract_archive_safely(zip_path, tmp_path / "dest")
    assert extracted.root_name is None
    assert extracted.files == ("a/f.txt", "b/f.txt")


def test_extract_normalizes_backslash_separators(tmp_path):
    zip_path = tmp_path / "bs.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("root\\sub\\f.txt", "x")
    extracted = extract_archive_safely(zip_path, tmp_path / "dest")
    assert extracted.files == ("root/sub/f.txt",)
    assert (tmp_path / "dest" / "root" / "sub" / "f.txt").read_text() == "x"


@pytest.mark.parametrize(
    "arcname",
    ["../evil.txt", "root/../../evil.txt", "/abs/evil.txt", "C:/evil.txt"],
)
def test_extract_rejects_path_traversal(tmp_path, arcname):
    zip_path = tmp_path / "slip.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(arcname, "payload")
    with pytest.raises(SafeArchiveError, match="UNSAFE_PATH"):
        extract_archive_safely(zip_path, tmp_path / "dest")
    assert not (tmp_path / "evil.txt").exists()
    assert not Path(str(tmp_path / "dest")).parent.joinpath("evil.txt").exists()


def test_extract_rejects_symlink_entry(tmp_path):
    zip_path = tmp_path / "sym.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        info = zipfile.ZipInfo("root/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
        archive.writestr("root/ok.txt", "fine")
    dest = tmp_path / "dest"
    with pytest.raises(SafeArchiveError, match="UNSAFE_PATH"):
        extract_archive_safely(zip_path, dest)
    assert not (dest / "root" / "link").exists()
    assert not (dest / "root" / "link").is_symlink()


def test_extract_restores_mode_without_setuid(tmp_path):
    """权限位二值化：只保留可执行语义（0o755/0o644），丢弃其余全部位。"""
    zip_path = tmp_path / "mode.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        info = zipfile.ZipInfo("root/run.sh")
        info.external_attr = (stat.S_IFREG | 0o4777) << 16
        archive.writestr(info, "#!/bin/sh\n")
    extract_archive_safely(zip_path, tmp_path / "dest")
    mode = (tmp_path / "dest" / "root" / "run.sh").stat().st_mode
    assert mode & 0o777 == 0o755  # 保留可执行，丢弃全局写位
    assert not mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)


def test_extract_rejects_oversized_zip(tmp_path):
    zip_path = tmp_path / "big.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("root/f.txt", "x" * 100)
    with pytest.raises(SafeArchiveError, match="LIMIT_EXCEEDED"):
        extract_archive_safely(zip_path, tmp_path / "dest", limits=ArchiveLimits(zip_bytes=10))


def test_extract_rejects_too_many_entries(tmp_path):
    zip_path = tmp_path / "many.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for index in range(5):
            archive.writestr(f"root/f{index}.txt", "x")
    with pytest.raises(SafeArchiveError, match="LIMIT_EXCEEDED"):
        extract_archive_safely(zip_path, tmp_path / "dest", limits=ArchiveLimits(max_files=3))


def test_extract_rejects_deep_nesting(tmp_path):
    zip_path = tmp_path / "deep.zip"
    deep = "root/" + "/".join(f"d{i}" for i in range(10)) + "/f.txt"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(deep, "x")
    with pytest.raises(SafeArchiveError, match="LIMIT_EXCEEDED"):
        extract_archive_safely(zip_path, tmp_path / "dest", limits=ArchiveLimits(max_depth=5))


def test_extract_enforces_file_and_total_limits(tmp_path):
    zip_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("root/big.txt", "A" * 100000)
    with pytest.raises(SafeArchiveError, match="LIMIT_EXCEEDED"):
        extract_archive_safely(
            zip_path, tmp_path / "dest1", limits=ArchiveLimits(file_bytes=1000)
        )
    with pytest.raises(SafeArchiveError, match="LIMIT_EXCEEDED"):
        extract_archive_safely(
            zip_path, tmp_path / "dest2", limits=ArchiveLimits(total_bytes=1000)
        )


def test_extract_handles_directory_entries(tmp_path):
    zip_path = tmp_path / "dirs.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("root/empty/", "")
        archive.writestr("root/f.txt", "x")
    dest = tmp_path / "dest"
    extracted = extract_archive_safely(zip_path, dest)
    assert extracted.files == ("root/f.txt",)
    assert extracted.root_name == "root"
    assert (dest / "root" / "empty").is_dir()


def test_extract_single_top_level_file_has_no_root_name(tmp_path):
    """仅含单个顶层文件（无目录）的包不算单根，root_name 必须为 None。"""
    zip_path = tmp_path / "flat.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("readme.txt", "x")
    extracted = extract_archive_safely(zip_path, tmp_path / "dest")
    assert extracted.files == ("readme.txt",)
    assert extracted.root_name is None


def test_extract_converges_corrupt_zip_to_invalid_package(tmp_path):
    zip_path = tmp_path / "broken.zip"
    zip_path.write_bytes(b"PK\x03\x04 not really a zip payload")
    with pytest.raises(SafeArchiveError) as exc:
        extract_archive_safely(zip_path, tmp_path / "dest")
    assert exc.value.code == "INVALID_PACKAGE"


def test_extract_converges_missing_zip_to_invalid_package(tmp_path):
    with pytest.raises(SafeArchiveError) as exc:
        extract_archive_safely(tmp_path / "absent.zip", tmp_path / "dest")
    assert exc.value.code == "INVALID_PACKAGE"


def test_write_zip_entries_rejects_symlink_swapped_source(source, tmp_path):
    """收集与写入之间源文件被替换为 symlink 时，写入侧必须拒绝。"""
    target = tmp_path / "outside.txt"
    target.write_text("sensitive")
    victim = source / "a.txt"
    victim.write_text("# demo\n")
    entries = {"root/a.txt": victim}
    victim.unlink()
    victim.symlink_to(target)
    with pytest.raises(SafeArchiveError) as exc:
        write_zip_entries(entries, tmp_path / "s.zip")
    assert exc.value.code == "UNSAFE_PATH"
    assert not (tmp_path / "s.zip").exists()


# ------------------------------------------------- marketplace 薄层别名兼容


def test_marketplace_aliases_point_to_common_layer():
    from jiuwenswarm.server.runtime.marketplace import asset_package_builder

    assert asset_package_builder.PackageBuildError is SafeArchiveError
    assert asset_package_builder.PackageLimits is ArchiveLimits
    assert asset_package_builder.collect_publish_files is collect_archive_files
