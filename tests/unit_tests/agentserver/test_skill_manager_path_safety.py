import io
import ntpath
import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path, PureWindowsPath

import pytest

from jiuwenswarm.server.runtime.skill import skill_manager
from jiuwenswarm.server.runtime.skill.skill_manager import (
    SkillManager,
    _copytree_long,
    _exists_long,
    _long_path,
    _mkdir_long,
    _safe_child_path,
    _safe_path_name,
    _safe_rmtree,
    _write_bytes_long,
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


def _deep_member_rel() -> str:
    """构造解压后总长必超 Windows MAX_PATH(260) 的成员相对路径。

    6 层 × 51 字符 ≈ 311 字符，叠加任意临时目录基路径后必超 260；
    单段 51 字符同时不触 NTFS 的 255 字符单文件名上限。
    """
    return "/".join(f"L{i}" + "x" * 50 for i in range(6))


def _force_normal_path_failures(monkeypatch) -> None:
    r"""模拟 LongPathsEnabled=0 机器：普通路径 mkdir/write_bytes/copytree 必败。

    开发机注册表常为 1，普通路径也能超 260，长路径用例在此类机器上兜底分支
    根本不执行（修复退化也无法暴露）；这里让普通路径 API 直接抛错，强制
    走 \\?\ 前缀兜底分支。带前缀的调用放行给真实实现。
    """
    real_write = Path.write_bytes
    real_mkdir = Path.mkdir
    real_copytree = shutil.copytree

    # Windows 真实超长失败的异常形态是结构化的：winerror=3(PATH NOT FOUND)，
    # errno 2，类型 FileNotFoundError，str 前缀 "[WinError 3]"。必须用 4 参
    # OSError(2, msg, None, 3) 构造——2 参 OSError(3, msg) 不解析 winerror，
    # str 是 "[Errno 3]"、类型还是 ProcessLookupError，与真实故障完全不同，
    # 会绕过 _copytree_long 按 winerror/str 判定 WinError 3/206 的分支。
    def fake_write(self, data):  # noqa: ANN001
        if not str(self).startswith("\\\\?\\"):
            raise OSError(2, "simulated MAX_PATH(260) failure", None, 3)
        return real_write(self, data)

    def fake_mkdir(self, *args, **kwargs):  # noqa: ANN001
        if not str(self).startswith("\\\\?\\"):
            raise OSError(2, "simulated MAX_PATH(260) failure", None, 3)
        return real_mkdir(self, *args, **kwargs)

    def fake_copytree(src, *args, **kwargs):  # noqa: ANN001
        # py3.11 copytree 内部递归传 DirEntry 对象（str() 是 "<DirEntry ...>" 表示
        # 形式），取 .path 才是真实路径；同时透传 7 个位置参数
        real_src = getattr(src, "path", src)
        if not str(real_src).startswith("\\\\?\\"):
            raise OSError(2, "simulated MAX_PATH(260) failure", None, 3)
        return real_copytree(src, *args, **kwargs)

    monkeypatch.setattr(Path, "write_bytes", fake_write)
    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    monkeypatch.setattr(shutil, "copytree", fake_copytree)


def _rel_parts(rel: str) -> tuple[str, ...]:
    """归一成员相对路径的本地目录分隔（zip 内为 /，本地为 os.sep）。"""
    return tuple(rel.split("/"))


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH(260) 限制专属场景")
def test_mkdir_and_write_bytes_long_fallback_on_normal_path_failure(tmp_path, monkeypatch):
    r"""模拟 LongPathsEnabled=0 机器：普通路径 mkdir/write 必败，兜底必须接管。"""
    _force_normal_path_failures(monkeypatch)

    target = tmp_path / "deep" / "nested" / "SKILL.md"
    _mkdir_long(target.parent)
    _write_bytes_long(target, b"long path content")
    assert _long_path(target).read_bytes() == b"long path content"


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH(260) 限制专属场景")
def test_copytree_long_fallback_on_normal_path_failure(tmp_path, monkeypatch):
    r"""安装链路兜底：普通 copytree 必败时，两侧 \?\ 加前缀重试须完成拷贝。

    对应检视缺口：解压成功不等于安装成功——解压到 staging 后仍要 copytree
    到 skills 目录，普通路径在 >260 时同样抛 WinError 3/206。
    """
    src = tmp_path / "staging"
    deep_dir = src.joinpath(*_rel_parts(_deep_member_rel()))
    # 源树也超 260：用被测兜底助手搭建（monkeypatch 前置会让普通 API 必败）
    _mkdir_long(deep_dir)
    _write_bytes_long(deep_dir / "SKILL.md", b"install me")
    (src / "top.md").write_text("top", encoding="utf-8")

    _force_normal_path_failures(monkeypatch)

    dest = tmp_path / "skills" / "demo"
    _copytree_long(src, dest, ignore=shutil.ignore_patterns("*.tmp"))

    deep_local = dest.joinpath(*_rel_parts(_deep_member_rel()))
    assert _long_path(deep_local / "SKILL.md").read_text(encoding="utf-8") == "install me"

    assert _long_path(dest / "top.md").read_text(encoding="utf-8") == "top"


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH(260) 限制专属场景")
def test_extract_then_install_survives_forced_long_path_failures(tmp_path, monkeypatch):
    r"""端到端（解压 → 安装拷贝）：普通路径 API 全程失败时整链仍须成功。

    复现完整故障链：解压到 staging → copytree 到安装目录，两段任一用普通
    路径都会在 LongPathsEnabled=0 机器上失败；修复后兜底接力完成安装。
    """
    _force_normal_path_failures(monkeypatch)

    rel = _deep_member_rel()
    staging = tmp_path / "staging"
    SkillManager._safe_extract_zip_bytes_to_dir(
        _zip_bytes({"SKILL.md": "demo", f"{rel}/SKILL.md": "deep content"}), staging
    )

    dest = tmp_path / "skills" / "demo"
    _copytree_long(staging, dest)

    assert _long_path(dest.joinpath(*_rel_parts(rel)) / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "deep content"
    assert _long_path(dest / "SKILL.md").read_text(encoding="utf-8") == "demo"


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH(260) 限制专属场景")
def test_safe_extract_zip_bytes_to_dir_survives_long_paths(tmp_path):
    r"""复现原故障：技能包层级深 + 文件名长时解压报 FileNotFoundError。

    修复前普通 mkdir/write 超 260 直接失败且无兜底；修复后 \?\ 前缀兜底
    让解压成功。越界校验先于落盘执行，恶意成员仍被拒绝（见 zip-slip 用例）。
    """
    rel = _deep_member_rel()
    content = "deep content"
    dest = tmp_path / "dest"
    SkillManager._safe_extract_zip_bytes_to_dir(
        _zip_bytes({"SKILL.md": "demo", f"{rel}/SKILL.md": content}), dest
    )
    # 普通路径读同样超限，用同款 \?\ 兜底断言内容落盘正确
    assert _long_path(dest / rel / "SKILL.md").read_text(encoding="utf-8") == content


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH(260) 限制专属场景")
def test_safe_extract_tar_to_dir_survives_long_paths(tmp_path):
    """tar 与 zip 走同一组长路径兜底（安全解压家族两条成员落盘路径）。"""
    rel = _deep_member_rel()
    content = "deep content"
    tar_path = tmp_path / "deep.tar"
    with tarfile.open(tar_path, "w") as tf:
        info = tarfile.TarInfo(f"{rel}/SKILL.md")
        data = content.encode("utf-8")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    dest = tmp_path / "dest"
    SkillManager._safe_extract_tar_to_dir(tar_path, dest)
    assert _long_path(dest / rel / "SKILL.md").read_text(encoding="utf-8") == content


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH(260) 限制专属场景")
def test_exists_long_probes_over_max_path_correctly(tmp_path):
    r"""_exists_long 对超长路径给出真实存在性结果.

    LongPathsEnabled=0 机器上 Path.exists() 对超长路径恒 False（假阴性），
    =1 开发机则普通探测同样可用；_exists_long（长路径形式探测）在两类
    机器上都必须与磁盘事实一致。安装守卫（已安装判断、残留清理、回滚
    条件）统一走它，与 _copytree_long/_safe_rmtree 保持同一套语义。
    """
    target = tmp_path / _deep_member_rel()
    _mkdir_long(target)
    _write_bytes_long(target / "SKILL.md", b"x")
    assert _exists_long(target) is True
    assert _exists_long(tmp_path / "no_such_dir") is False
    assert _exists_long(tmp_path) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH(260) 限制专属场景")
def test_zip_slip_rejected_under_forced_long_path_fallback(tmp_path, monkeypatch):
    r"""强制兜底环境下越界校验仍先于 \?\ 前缀落盘.

    \?\ 前缀会关闭 Win32 路径规范化（不折叠 ..），若校验与落盘路径错位，
    越界成员可能逃逸 dest。此处让普通路径 API 全程失败、落盘必须走前缀
    兜底，断言恶意成员仍被拒绝且 dest 外无游离文件。
    """
    _force_normal_path_failures(monkeypatch)
    rel = _deep_member_rel()
    dest = tmp_path / "dest"
    with pytest.raises(RuntimeError, match="非法路径"):
        SkillManager._safe_extract_zip_bytes_to_dir(
            _zip_bytes(
                {
                    "SKILL.md": "demo",
                    f"{rel}/SKILL.md": "deep content",
                    f"{rel}/../../evil.txt": "evil",
                }
            ),
            dest,
        )
    assert not list(tmp_path.rglob("evil.txt"))


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH(260) 限制专属场景")
def test_tar_slip_rejected_under_forced_long_path_fallback(tmp_path, monkeypatch):
    r"""tar 与 zip 同链路：强制兜底下越界成员仍被拒绝、无游离落盘."""
    _force_normal_path_failures(monkeypatch)
    rel = _deep_member_rel()
    tar_path = tmp_path / "slip.tar"
    with tarfile.open(tar_path, "w") as tf:
        for name, data in (("SKILL.md", b"demo"), (f"{rel}/../../evil.txt", b"evil")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    dest = tmp_path / "dest"
    with pytest.raises(RuntimeError, match="非法路径"):
        SkillManager._safe_extract_tar_to_dir(tar_path, dest)
    assert not list(tmp_path.rglob("evil.txt"))


def test_long_path_converts_unc_to_device_namespace():
    r"""UNC 路径须转成 \\?\UNC\server\share 设备命名空间，而非非法的 \\?\\\server\share."""
    unc = PureWindowsPath(r"\\server\share\skills\x")
    result = _long_path(unc)
    if os.name == "nt":
        assert str(result).lower() == r"\\?\unc\server\share\skills\x"
    else:
        # 非 Windows 不触达 UNC 分支，原样返回。用字符串比较：Path(unc) 会
        # 继承 PureWindowsPath 的 _parts（drive+root 单段），而 result 是对
        # str(unc) 重新做 POSIX 词法解析，两者 parts 不同、对象比较恒 False。
        assert str(result) == str(unc)


def test_long_path_prefix_construction_cross_platform(monkeypatch):
    r"""\?\ 前缀拼接语义在 Linux CI 上同样可验证（检视缺口：仅 Windows 用例覆盖）.

    生产实现仅 os.name 分支与 os.path.splitdrive 两处平台相关；把 os.name
    指为 nt、os.path 换成 ntpath 后，任何平台都能验证前缀拼接逻辑本身。
    同时把模块内的 Path 替换为 PureWindowsPath：pathlib 具体路径类按运行时
    os.name 派发，打桩环境下在非 Windows 上实例化 WindowsPath 的行为随
    Python 版本不稳；PureWindowsPath 是纯词法对象，str() 与具体类一致，
    不做任何系统调用，拼接断言跨平台安全。
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "path", ntpath)
    monkeypatch.setattr(skill_manager, "Path", PureWindowsPath)
    # 盘符分支
    assert str(_long_path(r"D:\deep\x")) == r"\\?\D:\deep\x"
    # 已带 \?\ 前缀的路径透传，不二次加工
    assert str(_long_path(r"\\?\D:\deep\x")) == r"\\?\D:\deep\x"
    # 正斜杠输入先归一为反斜杠再拼前缀
    assert str(_long_path("D:/deep/x")) == r"\\?\D:\deep\x"
    # UNC 走设备命名空间转换
    assert str(_long_path(r"\\server\share\skills\x")) == r"\\?\UNC\server\share\skills\x"


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH(260) 限制专属场景")
def test_safe_rmtree_handles_over_max_path_top_level(tmp_path):
    r"""顶层路径本身超 MAX_PATH 时 exists() 返回 False，_safe_rmtree 不得假报成功."""
    target = tmp_path / _deep_member_rel()
    _mkdir_long(target)
    _write_bytes_long(target / "SKILL.md", b"x")
    assert _long_path(target).exists()
    assert _safe_rmtree(target) is True
    assert not _long_path(target).exists()


def test_copytree_long_preserves_file_exists_error(tmp_path):
    """目标目录已存在时保持 copytree 报错语义，兜底不得静默合并新旧目录."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _mkdir_long(src)
    _write_bytes_long(src / "SKILL.md", b"new")
    _mkdir_long(dst)
    _write_bytes_long(dst / "SKILL.md", b"old")
    with pytest.raises(FileExistsError):
        _copytree_long(src, dst)
    # 旧内容保持原样，未被新包合并覆盖
    assert _long_path(dst / "SKILL.md").read_bytes() == b"old"


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH(260) 限制专属场景")
def test_copytree_long_rejects_existing_over_max_path_dest(tmp_path, monkeypatch):
    r"""超长目标已存在时首轮抛 WinError 3 而非 FileExistsError，兜底不得静默合并.

    LongPathsEnabled=0 机器上 exists() 对超长路径假阴性，copytree 探不到
    目标已存在，抛 FileNotFoundError(WinError 3)——而这正是兜底分支的入口
    异常；若不还原拒绝语义，新包会被静默合并进残留旧目录。
    """
    _force_normal_path_failures(monkeypatch)

    src = tmp_path / "src"
    _mkdir_long(src)
    _write_bytes_long(src / "SKILL.md", b"new")

    dst = tmp_path / _deep_member_rel()
    _mkdir_long(dst)
    _write_bytes_long(dst / "SKILL.md", b"old")

    with pytest.raises(FileExistsError) as exc_info:
        _copytree_long(src, dst)
    # 残留旧目录内容不得被动过
    assert _long_path(dst / "SKILL.md").read_bytes() == b"old"
    assert "Destination already exists" in str(exc_info.value)


def test_copytree_long_resumes_after_partial_first_pass(tmp_path, monkeypatch):
    r"""「半拷贝 + 续拷」分支：首轮拷到一半失败，兜底必须接力补齐剩余文件.

    检视缺口：强制失败桩让首轮立即抛错，最高风险的半拷贝场景未被覆盖。
    这里让首轮真实拷出 a.txt 后在 b.txt 上失败（模拟深层路径超限的
    WinError 3），兜底续拷须补齐全部文件。os.name 与 _long_path 打桩后，
    用例在 Linux CI 与 Windows 上语义一致。
    """
    monkeypatch.setattr(os, "name", "nt")
    # 恒等桩：不构造任何 pathlib 对象。os.name 打桩后 pathlib 具体路径类
    # 的派发在非 Windows 上行为随 Python 版本不稳（WindowsPath.__init__ 的
    # NotImplementedError 检查读取运行时 os.name），恒等返回避免该耦合。
    monkeypatch.setattr(skill_manager, "_long_path", lambda p: p)
    real_copyfile = shutil.copyfile
    calls = {"n": 0}

    def flaky_copyfile(s, d, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 2:
            # 与真实 Win32 超长失败同构：winerror=3 的 FileNotFoundError。
            # 4 参构造在非 Windows 上不派生 winerror，显式归一让桩异常在
            # 两端是同一种形态；续拷分支本身不读 winerror（existed=False
            # 短路 + OSError 无条件兜底重试），归一是保证用例前提稳定。
            raise _structured_oserror(3)
        return real_copyfile(s, d, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", flaky_copyfile)

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (src / name).write_text(name, encoding="utf-8")

    _copytree_long(src, dst)

    assert sorted(p.name for p in dst.iterdir()) == ["a.txt", "b.txt", "c.txt"]
    assert (dst / "c.txt").read_text(encoding="utf-8") == "c.txt"


def test_copytree_long_honors_caller_dirs_exist_ok(tmp_path):
    """调用方显式传 dirs_exist_ok=True 时按原语义合并，不抛 FileExistsError."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _mkdir_long(src)
    _write_bytes_long(src / "NEW.md", b"n")
    _mkdir_long(dst)
    _write_bytes_long(dst / "OLD.md", b"o")
    _copytree_long(src, dst, dirs_exist_ok=True)
    assert _long_path(dst / "NEW.md").read_bytes() == b"n"
    assert _long_path(dst / "OLD.md").read_bytes() == b"o"


def _structured_oserror(winerror: int) -> OSError:
    """构造带指定 winerror 的 OSError，跨平台归一.

    4 参 OSError(errno, msg, None, winerror) 仅 Windows 构建派生 winerror
    属性（官方文档 "Under Windows only"），非 Windows 上裸读该属性直接
    AttributeError。统一用 getattr 探测、缺失时补实例属性，保证
    _is_max_path_failure 的结构化分支在两端都被真实覆盖。
    """
    exc = OSError(2, "simulated", None, winerror)
    if getattr(exc, "winerror", None) != winerror:
        try:
            exc.winerror = winerror
        except (AttributeError, TypeError):
            pass
    return exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_structured_oserror(3), True),
        (_structured_oserror(206), True),
        (_structured_oserror(32), False),
        (_structured_oserror(39), False),
        (_structured_oserror(2067), False),
        (OSError("[WinError 3] 系统找不到指定的路径。"), True),
        (OSError("[WinError 206] 文件名或扩展名太长。"), True),
        (OSError("[WinError 32] 另一个程序正在使用此文件，进程无法访问。"), False),
        (OSError("[WinError 39] 磁盘空间不足。"), False),
        (OSError(2, "No such file or directory"), False),
    ],
)
def test_is_max_path_failure_error_code_classification(exc, expected):
    r"""WinError 3/206 判定按错误码整值/整段匹配.

    检视缺口：裸子串 "WinError 3" in msg 会把 WinError 32/39（共享冲突、
    磁盘满）等以 3 为前缀的错误码误判成路径过长，进而把无关失败包装成
    FileExistsError；文本形态必须按 "[WinError N]" 整段匹配，数字后跟
    其他数字（如 2067）也不得命中。
    """
    assert skill_manager._is_max_path_failure(exc) is expected
