import io
import shutil
import zipfile

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import (
    SkillManager,
    _safe_child_path,
    _safe_path_name,
)


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
        "---\nname: ../evil\n---\nbody\n",
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
