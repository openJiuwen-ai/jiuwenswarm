import io
import os
import tempfile
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import (
    SkillManager,
    _safe_child_path,
    _safe_path_name,
)

pytestmark = pytest.mark.usefixtures("allow_macos_pytest_temp_sources")


class SkillManagerHarness(SkillManager):
    def set_mock_remote_import(self, mock_func):
        self._import_skill_from_remote_archive = mock_func

    def register_imported_skill(self, name: str, origin: str):
        self._add_local_skill({"name": name, "origin": origin, "source": "local"})
        self._refresh_agent_data_indexes()


class _FakeClawHubResponse:
    def __init__(self, content: bytes):
        self.content = content

    @staticmethod
    def raise_for_status():
        return None


class _FakeClawHubClient:
    def __init__(self, content: bytes):
        self._content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, *, params, headers):
        assert url == "https://clawhub.ai/api/v1/download"
        assert params == {"slug": "demo-skill"}
        assert headers["Authorization"] == "Bearer test-token"
        return _FakeClawHubResponse(self._content)


def _zip_bytes(entries: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.mark.parametrize("name", ["../evil", "nested/skill", r"C:\tmp\skill", ".", "..", ""])
def test_safe_path_name_rejects_path_like_names(name):
    with pytest.raises(ValueError):
        _safe_path_name(name, "skill")


def test_safe_child_path_stays_under_base(tmp_path):
    child = _safe_child_path(tmp_path, "good-skill", "skill")

    assert child == (tmp_path / "good-skill").resolve()
    with pytest.raises(ValueError):
        _safe_child_path(tmp_path, "../evil", "skill")


@pytest.mark.asyncio
async def test_import_local_rejects_skill_name_path_traversal(tmp_path):
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: ../evil\ndescription: demo\n---\nbody\n",
        encoding="utf-8",
    )

    result = await manager.handle_skills_import_local({"path": str(src)})

    assert result["success"] is False
    assert "invalid skill name" in result["detail"]
    assert not (tmp_path / "evil").exists()


@pytest.mark.asyncio
async def test_uninstall_rejects_skill_name_path_traversal(tmp_path):
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))

    result = await manager.handle_skills_uninstall({"name": "../evil"})

    assert result["success"] is False
    assert "invalid skill name" in result["detail"]


@pytest.mark.asyncio
async def test_clawhub_download_rejects_zip_slip_archive(tmp_path, monkeypatch):
    manager = SkillManagerHarness(workspace_dir=str(tmp_path / "workspace"))
    await manager.handle_skills_clawhub_set_token({"token": "test-token"})
    zip_content = _zip_bytes(
        {
            "demo-skill/SKILL.md": "---\nname: demo-skill\nversion: 1.0.0\n---\nbody\n",
            "../evil.txt": b"x",
        }
    )

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.httpx.AsyncClient",
        lambda timeout: _FakeClawHubClient(zip_content),
    )

    result = await manager.handle_skills_clawhub_download({"slug": "demo-skill"})

    assert result["success"] is False
    assert "ZIP" in result["detail"] or "zip" in result["detail"]
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "workspace" / "skills" / "demo-skill").exists()


@pytest.mark.asyncio
async def test_teamskills_publish_rejects_zip_slip_source_zip(tmp_path):
    manager = SkillManagerHarness(workspace_dir=str(tmp_path / "workspace"))
    src_zip = tmp_path / "bad.zip"
    src_zip.write_bytes(
        _zip_bytes(
            {
                "demo-skill/SKILL.md": "---\nname: demo-skill\nversion: 1.0.0\n---\nbody\n",
                "../evil.txt": b"x",
            }
        )
    )

    result = await manager.handle_skills_team_skills_hub_publish(
        {
            "file": str(src_zip),
            "version": "1.0.0",
            "token": "test-token",
        }
    )

    assert result["success"] is False
    assert "非法路径" in result["detail"] or "越界" in result["detail"]
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.asyncio
async def test_import_local_supports_remote_obs_zip(tmp_path):
    manager = SkillManagerHarness(workspace_dir=str(tmp_path / "workspace"))

    async def _fake_remote_import(*, download_url, force, checksum_sha256=""):  # noqa: ANN001
        assert force is False
        assert checksum_sha256 == ""
        assert download_url == "https://demo-bucket.obs.cn-north-4.myhuaweicloud.com/skills/remote-demo.zip"
        dest = tmp_path / "workspace" / "skills" / "remote-demo"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "SKILL.md").write_text(
            "---\nname: remote-demo\ndescription: test skill\nversion: 1.0.0\n---\nbody\n",
            encoding="utf-8",
        )
        manager.register_imported_skill("remote-demo", download_url)
        return {"success": True, "skill": {"name": "remote-demo"}}

    manager.set_mock_remote_import(_fake_remote_import)

    result = await manager.handle_skills_import_local(
        {"path": "https://demo-bucket.obs.cn-north-4.myhuaweicloud.com/skills/remote-demo.zip"}
    )

    assert result["success"] is True
    assert result["skill"]["name"] == "remote-demo"
    assert (tmp_path / "workspace" / "skills" / "remote-demo" / "SKILL.md").is_file()
    assert manager.get_local_skills()[0]["origin"].startswith("https://demo-bucket.obs.")


@pytest.mark.asyncio
async def test_import_local_supports_remote_obs_tar_gz(tmp_path):
    manager = SkillManagerHarness(workspace_dir=str(tmp_path / "workspace"))

    async def _fake_remote_import(*, download_url, force, checksum_sha256=""):  # noqa: ANN001
        assert force is False
        assert checksum_sha256 == ""
        assert download_url == "https://demo-bucket.obs.cn-north-4.myhuaweicloud.com/skills/remote-tar-demo.tgz"
        dest = tmp_path / "workspace" / "skills" / "remote-tar-demo"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "SKILL.md").write_text(
            "---\nname: remote-tar-demo\ndescription: test skill\nversion: 1.0.0\n---\nbody\n",
            encoding="utf-8",
        )
        manager.register_imported_skill("remote-tar-demo", download_url)
        return {"success": True, "skill": {"name": "remote-tar-demo"}}

    manager.set_mock_remote_import(_fake_remote_import)

    result = await manager.handle_skills_import_local(
        {"path": "https://demo-bucket.obs.cn-north-4.myhuaweicloud.com/skills/remote-tar-demo.tgz"}
    )

    assert result["success"] is True
    assert result["skill"]["name"] == "remote-tar-demo"
    assert (tmp_path / "workspace" / "skills" / "remote-tar-demo" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_import_local_rejects_untrusted_remote_zip_host(tmp_path):
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))

    result = await manager.handle_skills_import_local({"path": "https://example.com/skills/demo.zip"})

    assert result["success"] is False
    assert "example.com" in result["detail"]


# ---------------------------------------------------------------------------
# 本地导入源安全加固：结构校验 / 基础黑名单 / 符号链接 / 平台路径判定
# ---------------------------------------------------------------------------


def _write_valid_skill(dir_path: Path, name: str = "good-skill") -> Path:
    """写入一个结构合规的 SKILL.md（frontmatter 含 name 与 description）。"""
    dir_path.mkdir(parents=True, exist_ok=True)
    skill_file = dir_path / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {name}\ndescription: demo skill\n---\nbody\n",
        encoding="utf-8",
    )
    return skill_file


def _manager(tmp_path) -> SkillManager:
    return SkillManager(workspace_dir=str(tmp_path / "workspace"))


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")


@pytest.mark.asyncio
async def test_import_local_nonexistent_path_reports_missing(tmp_path):
    manager = _manager(tmp_path)

    result = await manager.handle_skills_import_local({"path": str(tmp_path / "nope")})

    assert result["success"] is False
    assert "路径不存在" in result["detail"]


@pytest.mark.asyncio
async def test_import_local_rejects_dir_without_skill_md(tmp_path):
    manager = _manager(tmp_path)
    src = tmp_path / "src"
    src.mkdir()

    result = await manager.handle_skills_import_local({"path": str(src)})

    assert result["success"] is False
    assert "未找到 SKILL.md" in result["detail"]


@pytest.mark.asyncio
async def test_import_local_rejects_skill_md_without_frontmatter(tmp_path):
    manager = _manager(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text("just plain body\n", encoding="utf-8")

    result = await manager.handle_skills_import_local({"path": str(src)})

    assert result["success"] is False
    assert "frontmatter" in result["detail"]
    assert not (tmp_path / "workspace" / "skills" / "just").exists()


@pytest.mark.asyncio
async def test_import_local_rejects_skill_md_missing_description(tmp_path):
    manager = _manager(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: good-skill\n---\nbody\n", encoding="utf-8")

    result = await manager.handle_skills_import_local({"path": str(src)})

    assert result["success"] is False
    assert "description" in result["detail"]


@pytest.mark.asyncio
async def test_import_local_reports_invalid_frontmatter_yaml(tmp_path):
    manager = _manager(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: [unterminated\ndescription: x\n---\nbody\n",
        encoding="utf-8",
    )

    result = await manager.handle_skills_import_local({"path": str(src)})

    assert result["success"] is False
    assert "frontmatter YAML 无效" in result["detail"]


@pytest.mark.asyncio
async def test_import_local_reports_deeply_nested_frontmatter_as_invalid_yaml(tmp_path):
    manager = _manager(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    nested = "[" * 2000 + "0" + "]" * 2000
    (src / "SKILL.md").write_text(
        f"---\nname: good-skill\ndescription: x\nnested: {nested}\n---\nbody\n",
        encoding="utf-8",
    )

    result = await manager.handle_skills_import_local({"path": str(src)})

    assert result["success"] is False
    assert "frontmatter YAML 无效" in result["detail"]


@pytest.mark.asyncio
async def test_import_local_rejects_content_before_frontmatter(tmp_path):
    manager = _manager(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "preamble\n---\nname: good-skill\ndescription: x\n---\nbody\n",
        encoding="utf-8",
    )

    result = await manager.handle_skills_import_local({"path": str(src)})

    assert result["success"] is False
    assert "frontmatter" in result["detail"]


@pytest.mark.asyncio
async def test_import_local_rejects_invalid_skill_name(tmp_path):
    manager = _manager(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: ../evil\ndescription: x\n---\nbody\n",
        encoding="utf-8",
    )

    result = await manager.handle_skills_import_local({"path": str(src)})

    assert result["success"] is False
    assert "invalid skill name" in result["detail"]
    assert not (tmp_path / "evil").exists()


@pytest.mark.asyncio
async def test_import_local_single_valid_skill_md_succeeds(tmp_path):
    manager = _manager(tmp_path)
    src = tmp_path / "standalone.md"
    src.write_text(
        "---\nname: single-skill\ndescription: x\n---\nbody\n",
        encoding="utf-8",
    )

    result = await manager.handle_skills_import_local({"path": str(src)})

    assert result["success"] is True
    assert result["skill"]["name"] == "single-skill"
    assert (tmp_path / "workspace" / "skills" / "single-skill" / "standalone.md").is_file()


@pytest.mark.asyncio
async def test_import_local_rejects_bare_md_file(tmp_path):
    manager = _manager(tmp_path)
    src = tmp_path / "plain.md"
    src.write_text("just text\n", encoding="utf-8")

    result = await manager.handle_skills_import_local({"path": str(src)})

    assert result["success"] is False
    assert "frontmatter" in result["detail"]


@pytest.mark.asyncio
async def test_import_local_rejects_forbidden_root(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    forbidden = tmp_path / "forbidden"
    _write_valid_skill(forbidden / "src")
    monkeypatch.setenv("IMPORT_LOCAL_FORBIDDEN_DIRS", str(forbidden))

    result = await manager.handle_skills_import_local({"path": str(forbidden / "src")})

    assert result["success"] is False
    assert "禁止导入" in result["detail"]
    assert not (tmp_path / "workspace" / "skills" / "good-skill").exists()


@pytest.mark.asyncio
async def test_import_local_rejects_forbidden_root_through_symlink_alias(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    forbidden = tmp_path / "forbidden"
    _write_valid_skill(forbidden / "src")
    alias = tmp_path / "forbidden-alias"
    _symlink_or_skip(forbidden, alias, target_is_directory=True)
    monkeypatch.setenv("IMPORT_LOCAL_FORBIDDEN_DIRS", str(alias))

    result = await manager.handle_skills_import_local({"path": str(forbidden / "src")})

    assert result["success"] is False
    assert "禁止导入" in result["detail"]
    assert not (tmp_path / "workspace" / "skills" / "good-skill").exists()


@pytest.mark.asyncio
async def test_import_local_expands_service_home(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    service_home = tmp_path / "service-home"
    _write_valid_skill(service_home / "skill")
    monkeypatch.setenv("HOME", str(service_home))
    monkeypatch.setenv("USERPROFILE", str(service_home))

    result = await manager.handle_skills_import_local({"path": "~/skill"})

    assert result["success"] is True
    assert result["skill"]["name"] == "good-skill"


@pytest.mark.asyncio
async def test_import_local_expanded_home_still_rejects_sensitive_dir(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    service_home = tmp_path / "service-home"
    _write_valid_skill(service_home / ".ssh" / "skill")
    monkeypatch.setenv("HOME", str(service_home))
    monkeypatch.setenv("USERPROFILE", str(service_home))

    result = await manager.handle_skills_import_local({"path": "~/.ssh/skill"})

    assert result["success"] is False
    assert "禁止导入" in result["detail"]


@pytest.mark.asyncio
async def test_import_local_rejects_symlink_source(tmp_path):
    manager = _manager(tmp_path)
    target = tmp_path / "real-skill"
    _write_valid_skill(target)
    link = tmp_path / "link-skill"
    _symlink_or_skip(target, link, target_is_directory=True)

    result = await manager.handle_skills_import_local({"path": str(link)})

    assert result["success"] is False
    assert "符号链接" in result["detail"]
    assert not (tmp_path / "workspace" / "skills" / "good-skill").exists()


@pytest.mark.asyncio
async def test_import_local_rejects_symlink_inside_skill_dir(tmp_path):
    manager = _manager(tmp_path)
    src = tmp_path / "skill-src"
    _write_valid_skill(src)
    sensitive = tmp_path / "sensitive.txt"
    sensitive.write_text("secret\n", encoding="utf-8")
    _symlink_or_skip(sensitive, src / "scripts-link")

    result = await manager.handle_skills_import_local({"path": str(src)})

    assert result["success"] is False
    assert "符号链接" in result["detail"]
    assert not (tmp_path / "workspace" / "skills" / "good-skill").exists()


def test_assert_import_local_source_safe_rejects_relative_path(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(ValueError, match="仅支持绝对路径"):
        manager._assert_import_local_source_safe("skills/../../etc/passwd")


def test_assert_import_local_source_safe_rejects_file_scheme(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(ValueError, match="协议"):
        manager._assert_import_local_source_safe("file:///etc/passwd")


def test_assert_import_local_source_safe_rejects_windows_style_paths(tmp_path):
    manager = _manager(tmp_path)
    for bad in (r"C:\Windows\win.ini", r"C:foo"):
        with pytest.raises(ValueError):
            manager._assert_import_local_source_safe(bad)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only forbidden-root contract")
def test_windows_forbidden_roots_allow_regular_user_profiles():
    roots = SkillManager._get_import_local_forbidden_roots()
    root_keys = {os.path.normcase(str(root)) for root in roots}
    users_root = Path(os.environ.get("SystemDrive", "C:") + os.sep) / "Users"

    assert os.path.normcase(str(users_root)) not in root_keys
    assert os.path.normcase(str(users_root / "Default")) in root_keys
    assert os.path.normcase(str(users_root / "Public")) in root_keys


def test_assert_import_local_source_safe_rejects_unc_path_on_posix(tmp_path):
    manager = _manager(tmp_path)
    if os.name == "nt":
        pytest.skip("UNC 路径在 Windows 服务端为合法绝对路径")
    with pytest.raises(ValueError):
        manager._assert_import_local_source_safe(r"\\server\share\skill")


def test_import_local_from_path_trusted_flag_bypasses_source_checks(tmp_path):
    manager = _manager(tmp_path)
    with tempfile.TemporaryDirectory() as td:
        skill_dir = Path(td) / "remote-skill"
        skill_dir.mkdir()
        # 缺 description：非 trusted 结构校验拒绝；trusted 远端流走宽松解析。
        (skill_dir / "SKILL.md").write_text(
            "---\nname: remote-skill\n---\nbody\n",
            encoding="utf-8",
        )

        result = manager._import_local_from_path(
            skill_dir, force=False, origin="https://allowed-host/x.zip"
        )
        assert result["success"] is False
        assert "description" in result["detail"]

        result = manager._import_local_from_path(
            skill_dir,
            force=False,
            origin="https://allowed-host/x.zip",
            source_trusted=True,
        )
        assert result["success"] is True
        assert result["skill"]["name"] == "remote-skill"
        assert (tmp_path / "workspace" / "skills" / "remote-skill" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_team_skills_hub_validate_rejects_forbidden_root(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    forbidden = tmp_path / "forbidden"
    _write_valid_skill(forbidden / "src", name="team-skill")
    monkeypatch.setenv("IMPORT_LOCAL_FORBIDDEN_DIRS", str(forbidden))

    result = await manager.handle_skills_team_skills_hub_validate({"path": str(forbidden / "src")})

    assert result["success"] is False
    assert "禁止导入" in result["detail"]


@pytest.mark.asyncio
async def test_team_skills_hub_pack_rejects_forbidden_root(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    forbidden = tmp_path / "forbidden"
    _write_valid_skill(forbidden / "src", name="team-skill")
    monkeypatch.setenv("IMPORT_LOCAL_FORBIDDEN_DIRS", str(forbidden))

    result = await manager.handle_skills_team_skills_hub_pack({"path": str(forbidden / "src")})

    assert result["success"] is False
    assert "禁止导入" in result["detail"]
    assert not list((tmp_path / "forbidden" / "src").glob("*.zip"))


@pytest.mark.asyncio
async def test_team_skills_hub_validate_rejects_missing_description(tmp_path):
    manager = _manager(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: team-skill\nroles:\n  - id: a\n  - id: b\n---\nbody\n",
        encoding="utf-8",
    )

    result = await manager.handle_skills_team_skills_hub_validate({"path": str(src)})

    assert result["success"] is False
    assert "description" in result["detail"]
