from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

_TEAM_SKILLS_HUB_ZIP_URL = "https://openjiuwen-market.obs.ap-southeast-1.myhuaweicloud.com/plugins/demo.zip"


class TeamSkillsHubHarnessSkillManager(SkillManager):
    """公开受保护方法供单测（勿命名为 Test*，否则 pytest 会当成测试类收集）。"""

    def get_team_skills_hub_base_url(self) -> str:
        return self._get_team_skills_hub_base_url()

    def set_mock_get_data(self, mock_func) -> None:
        self._team_skills_hub_http_get_data = mock_func

    def set_mock_download(self, mock_func) -> None:
        self._download_zip_and_verify = mock_func

    def call_assert_team_skills_hub_download_url_allowed(self, url: str) -> None:
        self._assert_team_skills_hub_download_url_allowed(url)

    def call_safe_extract_zip_to_dir(self, zip_path, out_dir) -> None:
        self._safe_extract_zip_to_dir(zip_path, out_dir)


def _build_skill_zip_bytes(*, skill_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            ("---\n" f"name: {skill_name}\n" "description: test skill\n" "version: 1.0.0\n" "---\n" "body\n"),
        )
    return buf.getvalue()


def _build_skill_zip_bytes_flat_root(*, skill_name: str) -> bytes:
    """SKILL.md 在 zip 根目录（与 Team Hub 常见扁平包一致），用于覆盖 copytree 误带 skill.zip 的场景。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "SKILL.md",
            ("---\n" f"name: {skill_name}\n" "description: test skill\n" "version: 1.0.0\n" "---\n" "body\n"),
        )
    return buf.getvalue()


def test_get_team_skills_hub_base_url_default(monkeypatch):
    monkeypatch.delenv("TEAM_SKILLS_HUB_BASE_URL", raising=False)
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir="dummy")
    assert manager.get_team_skills_hub_base_url() == "https://teamskills.openjiuwen.com"


def test_get_team_skills_hub_base_url_env_override(monkeypatch):
    monkeypatch.setenv("TEAM_SKILLS_HUB_BASE_URL", "https://example.com/custom/hub/")
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir="dummy")
    assert manager.get_team_skills_hub_base_url() == "https://example.com/custom/hub"


def test_get_team_skills_hub_base_url_default_without_override(monkeypatch):
    """未配置 TEAM_SKILLS_HUB_BASE_URL 时应回退默认值。"""
    monkeypatch.delenv("TEAM_SKILLS_HUB_BASE_URL", raising=False)
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir="dummy")
    assert manager.get_team_skills_hub_base_url() == "https://teamskills.openjiuwen.com"


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_info(tmp_path, monkeypatch):
    monkeypatch.delenv("TEAM_SKILLS_HUB_BASE_URL", raising=False)
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        assert path == "/api/v1/artifacts/demo-skill"
        assert kwargs.get("params") == {"version": "1.0.0"}
        return {
            "asset_id": "demo-skill",
            "name": "demo-skill",
            "display_name": "Demo Skill",
            "download_url": _TEAM_SKILLS_HUB_ZIP_URL,
        }

    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_team_skills_hub_info({"asset_id": "demo-skill", "version": "1.0.0"})
    assert payload["success"] is True
    assert payload["asset_id"] == "demo-skill"
    assert payload["version"] == "1.0.0"
    assert payload["data"]["display_name"] == "Demo Skill"


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_search_maps_response(tmp_path):
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        assert path == "/api/v1/plugins"
        return {
            "items": [
                {
                    "asset_id": "demo-skill",
                    "name": "demo-skill",
                    "display_name": "Demo Skill",
                    "short_desc": "desc",
                    "latest_version": "1.2.3",
                    "update_time": 123,
                }
            ]
        }

    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_team_skills_hub_search({"q": "demo", "limit": 10})
    assert payload["success"] is True
    assert payload["count"] == 1
    assert payload["skills"][0]["asset_id"] == "demo-skill"
    assert payload["skills"][0]["display_name"] == "Demo Skill"


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_install_success(tmp_path):
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    zip_bytes = _build_skill_zip_bytes(skill_name="demo-skill")

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        assert path == "/api/v1/artifacts/demo-skill"
        return {
            "download_url": _TEAM_SKILLS_HUB_ZIP_URL,
            "checksum_sha256": "",
            "version": "1.0.0",
        }

    async def _fake_download(url, **kwargs):  # noqa: ANN001
        assert url == _TEAM_SKILLS_HUB_ZIP_URL
        return zip_bytes

    manager.set_mock_get_data(_fake_get_data)
    manager.set_mock_download(_fake_download)

    payload = await manager.handle_skills_team_skills_hub_install({"asset_id": "demo-skill"})
    assert payload["success"] is True
    assert payload["skill"]["name"] == "demo-skill"
    assert (tmp_path / "skills" / "demo-skill" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_install_flat_zip_does_not_copy_staging_zip(tmp_path):
    """扁平 zip（根目录 SKILL.md）安装后目标目录不应残留暂存的 skill.zip。"""
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    zip_bytes = _build_skill_zip_bytes_flat_root(skill_name="flat-demo")

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        return {
            "download_url": _TEAM_SKILLS_HUB_ZIP_URL,
            "checksum_sha256": "",
            "version": "1.0.0",
        }

    async def _fake_download(url, **kwargs):  # noqa: ANN001
        return zip_bytes

    manager.set_mock_get_data(_fake_get_data)
    manager.set_mock_download(_fake_download)

    payload = await manager.handle_skills_team_skills_hub_install({"asset_id": "flat-demo"})
    assert payload["success"] is True
    dest = tmp_path / "skills" / "flat-demo"
    assert (dest / "SKILL.md").is_file()
    assert not (dest / "skill.zip").exists()


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_install_duplicate_without_force(tmp_path):
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    zip_bytes = _build_skill_zip_bytes(skill_name="demo-skill")
    (tmp_path / "skills" / "demo-skill").mkdir(parents=True, exist_ok=True)

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        return {
            "download_url": _TEAM_SKILLS_HUB_ZIP_URL,
            "checksum_sha256": "",
            "version": "1.0.0",
        }

    async def _fake_download(url, **kwargs):  # noqa: ANN001
        return zip_bytes

    manager.set_mock_get_data(_fake_get_data)
    manager.set_mock_download(_fake_download)

    payload = await manager.handle_skills_team_skills_hub_install({"asset_id": "demo-skill", "force": False})
    assert payload["success"] is False
    assert "已安装" in payload["detail"]


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_install_invalid_zip(tmp_path):
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        return {
            "download_url": _TEAM_SKILLS_HUB_ZIP_URL,
            "checksum_sha256": "",
            "version": "1.0.0",
        }

    async def _fake_download(url, **kwargs):  # noqa: ANN001
        return b"not-a-zip"

    manager.set_mock_get_data(_fake_get_data)
    manager.set_mock_download(_fake_download)

    payload = await manager.handle_skills_team_skills_hub_install({"asset_id": "demo-skill"})
    assert payload["success"] is False
    assert payload["detail_key"] == "skills.teamskillshub.errors.installFailed"
    assert "zip" in payload["detail"].lower()


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_install_download_failure(tmp_path):
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        return {
            "download_url": _TEAM_SKILLS_HUB_ZIP_URL,
            "checksum_sha256": "",
            "version": "1.0.0",
        }

    async def _fake_download(url, **kwargs):  # noqa: ANN001
        raise RuntimeError("download failed")

    manager.set_mock_get_data(_fake_get_data)
    manager.set_mock_download(_fake_download)

    payload = await manager.handle_skills_team_skills_hub_install({"asset_id": "demo-skill"})
    assert payload["success"] is False
    assert payload["detail"] == "download failed"
    assert payload["detail_key"] == "skills.teamskillshub.errors.installFailed"


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_search_hide_internal_error(tmp_path):
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        raise RuntimeError("internal endpoint detail")

    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_team_skills_hub_search({"q": "demo", "limit": 10})
    assert payload["success"] is False
    assert payload["detail"] == "internal endpoint detail"
    assert payload["detail_key"] == "skills.teamskillshub.errors.searchFailed"


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_install_rejects_untrusted_download_host(tmp_path):
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        return {
            "download_url": "https://example.com/demo.zip",
            "checksum_sha256": "",
            "version": "1.0.0",
        }

    async def _fake_download(url, **kwargs):  # noqa: ANN001
        raise AssertionError("should not download when host is untrusted")

    manager.set_mock_get_data(_fake_get_data)
    manager.set_mock_download(_fake_download)

    payload = await manager.handle_skills_team_skills_hub_install({"asset_id": "demo-skill"})
    assert payload["success"] is False
    assert "host" in payload["detail"] and "白名单" in payload["detail"]
    assert payload["detail_key"] == "skills.teamskillshub.errors.installFailed"


def test_team_skills_hub_allowed_download_hosts_support_suffix_rule(monkeypatch):
    monkeypatch.setenv("TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS", ".myhuaweicloud.com")
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir="dummy")
    manager.call_assert_team_skills_hub_download_url_allowed(
        "https://openjiuwen-market.obs.ap-southeast-1.myhuaweicloud.com/plugins/demo.zip"
    )


def test_team_skills_hub_allowed_download_hosts_support_wildcard_region(monkeypatch):
    monkeypatch.setenv(
        "TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS",
        "openjiuwen-market.obs.*.myhuaweicloud.com",
    )
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir="dummy")
    manager.call_assert_team_skills_hub_download_url_allowed(
        "https://openjiuwen-market.obs.ap-east-1.myhuaweicloud.com/plugins/demo.zip"
    )


def test_safe_extract_zip_to_dir_rejects_zip_slip(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("../evil.txt", b"x")
    zip_path = tmp_path / "bad.zip"
    zip_path.write_bytes(buf.getvalue())
    out = tmp_path / "out"
    out.mkdir()
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir="dummy")
    with pytest.raises(RuntimeError, match="非法路径|越界"):
        manager.call_safe_extract_zip_to_dir(zip_path, out)


def test_safe_extract_zip_to_dir_writes_skill(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "demo-skill/SKILL.md",
            "---\nname: demo-skill\ndescription: x\nversion: 1.0.0\n---\n",
        )
    zip_path = tmp_path / "ok.zip"
    zip_path.write_bytes(buf.getvalue())
    out = tmp_path / "out"
    out.mkdir()
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir="dummy")
    manager.call_safe_extract_zip_to_dir(zip_path, out)
    assert (out / "demo-skill" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_install_rejects_zip_slip(tmp_path):
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("../evil.txt", b"x")
    bad_zip = buf.getvalue()

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        return {
            "download_url": _TEAM_SKILLS_HUB_ZIP_URL,
            "checksum_sha256": "",
            "version": "1.0.0",
        }

    async def _fake_download(url, **kwargs):  # noqa: ANN001
        return bad_zip

    manager.set_mock_get_data(_fake_get_data)
    manager.set_mock_download(_fake_download)

    payload = await manager.handle_skills_team_skills_hub_install({"asset_id": "demo-skill"})
    assert payload["success"] is False
    assert "非法路径" in payload["detail"] or "越界" in payload["detail"]
    assert payload["detail_key"] == "skills.teamskillshub.errors.installFailed"
    assert not (tmp_path / "evil.txt").exists()


# ---------------------------------------------------------------------------
# skills.teamskillshub.init —— path 路径穿越防护
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_init_success_under_skills_dir(tmp_path):
    """默认 path（.）在 skills 目录内创建脚手架。"""
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    payload = await manager.handle_skills_team_skills_hub_init({"name": "demo-skill"})
    assert payload["success"] is True
    target = skills_dir / "demo-skill"
    assert payload["path"] == str(target)
    assert (target / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_init_success_with_relative_subdir(tmp_path):
    """相对子路径作为父目录时应在 skills 目录内创建（父目录需存在）。"""
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    skills_dir = tmp_path / "skills"
    (skills_dir / "group").mkdir(parents=True, exist_ok=True)

    payload = await manager.handle_skills_team_skills_hub_init(
        {"name": "demo-skill", "path": "group"}
    )
    assert payload["success"] is True
    assert (skills_dir / "group" / "demo-skill" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_init_rejects_absolute_posix_path(tmp_path):
    """POSIX 风格绝对/盘符相对路径必须被拒绝，且不创建任何内容。

    在 POSIX 平台 ``/etc`` 为绝对路径，命中绝对路径分支；在 Windows 平台
    ``/etc`` 是盘符相对路径，解析到 ``<盘符>:/etc``，命中越界分支。两者均被拒。
    """
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    payload = await manager.handle_skills_team_skills_hub_init(
        {"name": "evil", "path": "/etc/evil-target"}
    )
    assert payload["success"] is False
    assert "相对路径" in payload["detail"] or "越界" in payload["detail"]
    # 关键：未在 skills 目录外创建任何内容
    assert not (tmp_path / "evil").exists()
    skills_dir = tmp_path / "skills"
    assert not any((skills_dir / "etc").rglob("evil")) if (skills_dir / "etc").exists() else True


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_init_rejects_absolute_windows_path(tmp_path):
    """Windows 盘符绝对路径必须被拒绝，且不在该目录创建任何内容。"""
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    import tempfile

    foreign_dir = Path(tempfile.gettempdir()) / "jiuwenswarm_init_traversal_marker"
    foreign_dir.mkdir(parents=True, exist_ok=True)
    try:
        payload = await manager.handle_skills_team_skills_hub_init(
            {"name": "evil", "path": str(foreign_dir)}
        )
        assert payload["success"] is False
        assert "相对路径" in payload["detail"]
        # 关键：未在越界目录下创建子目录或文件
        assert not (foreign_dir / "evil").exists()
    finally:
        if foreign_dir.exists():
            _safe_rmtree_for_test(foreign_dir)


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_init_rejects_traversal(tmp_path):
    """相对穿越路径（..）解析后逃逸 skills 目录，必须被拒绝。"""
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    payload = await manager.handle_skills_team_skills_hub_init(
        {"name": "evil", "path": "../../.."}
    )
    assert payload["success"] is False
    assert "越界" in payload["detail"]
    assert not (tmp_path.parent / "evil").exists()


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_init_rejects_home_abs_path(tmp_path, monkeypatch):
    """~ 展开后为绝对路径，必须被拒绝（路径穿越变种）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    payload = await manager.handle_skills_team_skills_hub_init(
        {"name": "evil", "path": "~"}
    )
    assert payload["success"] is False
    assert "相对路径" in payload["detail"] or "越界" in payload["detail"]
    assert not (tmp_path / "evil").exists()


def _safe_rmtree_for_test(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
