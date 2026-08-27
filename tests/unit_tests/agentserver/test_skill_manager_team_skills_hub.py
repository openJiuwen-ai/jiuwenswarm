from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager, SkillRpcError

_TEAM_SKILLS_HUB_ZIP_URL = "https://openjiuwen-market.obs.ap-southeast-1.myhuaweicloud.com/plugins/demo.zip"


class TeamSkillsHubHarnessSkillManager(SkillManager):
    """公开受保护方法供单测（勿命名为 Test*，否则 pytest 会当成测试类收集）。"""

    def get_team_skills_hub_base_url(self) -> str:
        return self._get_team_skills_hub_base_url()

    def set_mock_get_data(self, mock_func) -> None:
        self._team_skills_hub_http_get_data = mock_func

    def set_mock_post_data(self, mock_func) -> None:
        self._team_skills_hub_http_post_data = mock_func

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
                    "publisher_name": "example-publisher",
                    "category_name": "Productivity",
                    "install_count": 42,
                    "plugin_type": "swarmskill",
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
    assert payload["skills"][0]["version"] == "1.2.3"
    assert payload["skills"][0]["updated_at"] == 123
    assert payload["skills"][0]["author"] == "example-publisher"
    assert payload["skills"][0]["is_team_skill"] is True
    assert payload["skills"][0]["plugin_type"] == "swarmskill"


@pytest.mark.asyncio
async def test_handle_skills_swarm_skills_hub_recommend_maps_response(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_SKILLS_HUB_SYSTEM_TOKEN", "sys-token-demo")
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))

    async def _fake_post_data(path, **kwargs):  # noqa: ANN001
        assert path == "/api/v1/recommend"
        assert kwargs["json_body"]["user_id"] == "u-1"
        assert kwargs["json_body"]["top_k"] == 5
        assert kwargs["headers"]["X-System-Token"] == "sys-token-demo"
        return {
            "request_id": "r1",
            "user_id": "u-1",
            "source": "user_history",
            "category_id": "",
            "items": [{"asset_id": "demo-skill", "score": 0.9}],
        }

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        assert path == "/api/v1/plugins"
        assert kwargs["params"]["asset_id"] == "demo-skill"
        return {
            "items": [
                {
                    "asset_id": "demo-skill",
                    "name": "demo-skill",
                    "display_name": "Demo Skill",
                    "short_desc": "desc",
                    "latest_version": "1.2.3",
                    "update_time": 123,
                    "plugin_type": "skill",
                    "tags": ["office", "productivity"],
                }
            ]
        }

    manager.set_mock_post_data(_fake_post_data)
    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_swarm_skills_hub_recommend(
        {"user_id": "u-1", "top_k": 5, "enrich": True}
    )
    assert payload["success"] is True
    assert payload["source"] == "user_history"
    assert payload["count"] == 1
    assert payload["plugin_type"] == ""
    assert payload["skills"][0]["asset_id"] == "demo-skill"
    assert payload["skills"][0]["display_name"] == "Demo Skill"
    assert payload["skills"][0]["score"] == 0.9
    assert payload["skills"][0]["plugin_type"] == "skill"
    assert payload["skills"][0]["tags"] == ["office", "productivity"]


@pytest.mark.asyncio
async def test_handle_skills_swarm_skills_hub_recommend_filters_plugin_type(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_SKILLS_HUB_SYSTEM_TOKEN", "sys-token-demo")
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))

    async def _fake_post_data(path, **kwargs):  # noqa: ANN001
        assert path == "/api/v1/recommend"
        # top_k=2 with plugin_type filter over-fetches (2 * 5)
        assert kwargs["json_body"]["top_k"] == 10
        return {
            "request_id": "r1",
            "user_id": "",
            "source": "topk_install",
            "category_id": "",
            "items": [
                {"asset_id": "a-skill", "score": 0.9},
                {"asset_id": "b-swarm", "score": 0.8},
                {"asset_id": "c-skill", "score": 0.7},
            ],
        }

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        assert path == "/api/v1/plugins"
        asset_id = kwargs["params"]["asset_id"]
        plugin_type = {
            "a-skill": "skill",
            "b-swarm": "swarmskill",
            "c-skill": "skill",
        }[asset_id]
        return {
            "items": [
                {
                    "asset_id": asset_id,
                    "name": asset_id,
                    "display_name": asset_id,
                    "short_desc": "d",
                    "latest_version": "1.0.0",
                    "update_time": 1,
                    "plugin_type": plugin_type,
                }
            ]
        }

    manager.set_mock_post_data(_fake_post_data)
    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_swarm_skills_hub_recommend(
        {"top_k": 2, "plugin_type": "swarmskill"}
    )
    assert payload["success"] is True
    assert payload["plugin_type"] == "swarmskill"
    assert payload["count"] == 1
    assert payload["skills"][0]["asset_id"] == "b-swarm"
    assert payload["skills"][0]["plugin_type"] == "swarmskill"

    payload_alias = await manager.handle_skills_swarm_skills_hub_recommend(
        {"top_k": 2, "skill_type": "teamskills"}
    )
    assert payload_alias["success"] is True
    assert payload_alias["plugin_type"] == "swarmskill"
    assert payload_alias["count"] == 1
    assert payload_alias["skills"][0]["asset_id"] == "b-swarm"


@pytest.mark.asyncio
async def test_handle_skills_swarm_skills_hub_recommend_drops_unlisted(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_SKILLS_HUB_SYSTEM_TOKEN", "sys-token-demo")
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))

    async def _fake_post_data(path, **kwargs):  # noqa: ANN001
        assert path == "/api/v1/recommend"
        return {
            "request_id": "r1",
            "user_id": "",
            "source": "topk_install",
            "category_id": "",
            "items": [
                {"asset_id": "online-skill", "score": 0.9},
                {"asset_id": "offline-skill", "score": 0.8},
            ],
        }

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        assert path == "/api/v1/plugins"
        asset_id = kwargs["params"]["asset_id"]
        if asset_id == "offline-skill":
            return {"items": []}
        return {
            "items": [
                {
                    "asset_id": "online-skill",
                    "name": "online-skill",
                    "display_name": "Online Skill",
                    "short_desc": "ok",
                    "latest_version": "1.0.0",
                    "update_time": 1,
                    "plugin_type": "skill",
                }
            ]
        }

    manager.set_mock_post_data(_fake_post_data)
    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_swarm_skills_hub_recommend({"top_k": 5, "enrich": True})
    assert payload["success"] is True
    assert payload["count"] == 1
    assert payload["skills"][0]["asset_id"] == "online-skill"
    assert payload["skills"][0]["display_name"] == "Online Skill"


@pytest.mark.asyncio
async def test_handle_skills_swarm_skills_hub_recommend_keeps_item_on_enrich_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TEAM_SKILLS_HUB_SYSTEM_TOKEN", "sys-token-demo")
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))

    async def _fake_post_data(path, **kwargs):  # noqa: ANN001
        return {
            "request_id": "r1",
            "user_id": "",
            "source": "topk_install",
            "items": [{"asset_id": "flaky-skill", "score": 0.5}],
        }

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        raise RuntimeError("hub timeout")

    manager.set_mock_post_data(_fake_post_data)
    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_swarm_skills_hub_recommend({"top_k": 3, "enrich": True})
    assert payload["success"] is True
    assert payload["count"] == 1
    assert payload["skills"][0]["asset_id"] == "flaky-skill"


@pytest.mark.asyncio
async def test_handle_skills_swarm_skills_hub_recommend_unauth_posts_without_headers(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TEAM_SKILLS_HUB_SYSTEM_TOKEN", raising=False)
    monkeypatch.delenv("TEAM_SKILLS_HUB_USER_TOKEN", raising=False)
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))

    async def _fake_post_data(path, **kwargs):  # noqa: ANN001
        assert path == "/api/v1/recommend"
        headers = kwargs.get("headers") or {}
        assert "Authorization" not in headers
        assert "X-System-Token" not in headers
        assert kwargs["json_body"]["top_k"] == 3
        return {
            "request_id": "r-unauth",
            "user_id": "",
            "source": "topk_install",
            "items": [{"asset_id": "cold-skill", "score": 1.0}],
        }

    async def _should_not_list(path, **kwargs):  # noqa: ANN001
        raise AssertionError(f"unauth path must POST recommend, not GET {path}")

    manager.set_mock_post_data(_fake_post_data)
    manager.set_mock_get_data(_should_not_list)
    payload = await manager.handle_skills_swarm_skills_hub_recommend({"top_k": 3, "enrich": False})
    assert payload["success"] is True
    assert payload["source"] == "topk_install"
    assert payload["user_id"] == ""
    assert payload["count"] == 1
    assert payload["skills"][0]["asset_id"] == "cold-skill"


@pytest.mark.asyncio
async def test_handle_skills_swarm_skills_hub_recommend_hide_internal_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_SKILLS_HUB_SYSTEM_TOKEN", "sys-token-demo")
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))

    async def _boom(path, **kwargs):  # noqa: ANN001
        raise RuntimeError("secret-internal")

    manager.set_mock_post_data(_boom)
    payload = await manager.handle_skills_swarm_skills_hub_recommend({"top_k": 3, "enrich": False})
    assert payload["success"] is False
    assert payload["detail_key"] == "skills.swarmskillshub.errors.recommendFailed"


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_install_success(tmp_path):
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    zip_bytes = _build_skill_zip_bytes(skill_name="demo-skill")

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        assert path == "/api/v1/artifacts/demo-skill"
        assert kwargs.get("params") is None
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
    dest = tmp_path / "skills" / "demo-skill"
    assert (dest / "SKILL.md").is_file()
    assert (dest / ".archive" / "versions" / "index.json").is_file()
    index = json.loads((dest / ".archive" / "versions" / "index.json").read_text(encoding="utf-8"))
    assert index["current_version"] == "1.0.0"
    assert index["installed_asset_id"] == "demo-skill"
    assert len(index["versions"]) == 1
    storage_id = index["versions"][0]["storage_id"]
    assert (dest / ".archive" / "versions" / "content" / storage_id / "SKILL.md").is_file()
    plugins = manager._state.get("installed_plugins", [])
    assert plugins and "version" not in plugins[0]


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
async def test_handle_skills_team_skills_hub_install_idempotent_same_asset(tmp_path):
    """相同 installed_asset_id 再次安装应直接成功，不请求远端."""
    from jiuwenswarm.server.runtime.skill.archive_store import write_skillhub_first_install_index

    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    dest = tmp_path / "skills" / "demo-skill"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: test\n---\nbody\n",
        encoding="utf-8",
    )
    content = dest / ".archive" / "versions" / "content" / "ver-abc"
    content.mkdir(parents=True)
    shutil.copy2(dest / "SKILL.md", content / "SKILL.md")
    write_skillhub_first_install_index(
        dest,
        version="1.0.0",
        asset_id="demo-skill",
        storage_id="ver-abc",
        checksum_sha256="abc",
    )

    async def _should_not_call(*_a, **_k):  # noqa: ANN001
        raise AssertionError("idempotent install must not hit remote")

    manager.set_mock_get_data(_should_not_call)
    manager.set_mock_download(_should_not_call)

    payload = await manager.handle_skills_team_skills_hub_install({"asset_id": "demo-skill"})
    assert payload["success"] is True
    assert payload["skill"]["path"] == str(dest)


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_install_name_conflict_even_with_force(tmp_path):
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    zip_bytes = _build_skill_zip_bytes(skill_name="demo-skill")
    local = tmp_path / "skills" / "demo-skill"
    local.mkdir(parents=True, exist_ok=True)
    (local / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: local\n---\nbody\n",
        encoding="utf-8",
    )

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

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skills_team_skills_hub_install(
            {"asset_id": "other-asset", "force": True}
        )
    assert exc_info.value.code == "SKILL_NAME_CONFLICT"
    assert (local / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: demo-skill")


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_pack_excludes_archive(tmp_path):
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    skill = tmp_path / "skills" / "pack-demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: pack-demo\ndescription: pack me\n---\nbody\n",
        encoding="utf-8",
    )
    (skill / "archive").mkdir()
    (skill / "archive" / "note.txt").write_text("business", encoding="utf-8")
    hidden = skill / ".archive" / "versions"
    hidden.mkdir(parents=True)
    (hidden / "index.json").write_text("{}", encoding="utf-8")

    payload = await manager.handle_skills_team_skills_hub_pack(
        {"path": str(skill), "output": str(tmp_path / "out")}
    )
    assert payload["success"] is True
    zip_path = Path(payload["path"])
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
    assert "pack-demo/plugin.yaml" in names
    assert "pack-demo/pack-demo/SKILL.md" in names
    assert "pack-demo/pack-demo/archive/note.txt" in names
    assert not any(n == ".archive" or n.startswith(".archive/") for n in names)


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_publish_writes_remote_metadata(tmp_path, monkeypatch):
    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    skill = tmp_path / "skills" / "pub-demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: pub-demo\ndescription: publish me\n---\nbody\n",
        encoding="utf-8",
    )

    async def _fake_publish_request(**kwargs):  # noqa: ANN001
        return {"asset_id": "asset-999", "name": "pub-demo", "version": "1.0.0"}

    monkeypatch.setattr(manager, "_teamskills_hub_publish_request", _fake_publish_request)

    payload = await manager.handle_skills_team_skills_hub_publish(
        {
            "path": str(skill),
            "version": "1.0.0",
            "token": "tok",
        }
    )
    assert payload["success"] is True
    assert payload["skill_id"] == "asset-999"
    index = json.loads(
        (skill / ".archive" / "versions" / "index.json").read_text(encoding="utf-8")
    )
    assert index["remote_asset_id"] == "asset-999"
    assert index["last_published_version"] == "1.0.0"
    assert index["current_version"] is None
    assert index["installed_asset_id"] is None


@pytest.mark.asyncio
async def test_handle_skills_team_skills_hub_publish_version_conflict(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.skill.archive_store import update_publish_remote_metadata

    manager = TeamSkillsHubHarnessSkillManager(workspace_dir=str(tmp_path))
    skill = tmp_path / "skills" / "pub-demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: pub-demo\ndescription: publish me\n---\nbody\n",
        encoding="utf-8",
    )
    update_publish_remote_metadata(
        skill, remote_asset_id="asset-1", last_published_version="1.0.0"
    )

    async def _should_not_publish(**kwargs):  # noqa: ANN001
        raise AssertionError("conflict must short-circuit before remote publish")

    monkeypatch.setattr(manager, "_teamskills_hub_publish_request", _should_not_publish)

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skills_team_skills_hub_publish(
            {"path": str(skill), "version": "1.0.0", "token": "tok", "force": False}
        )
    assert exc_info.value.code == "SKILL_PUBLISH_VERSION_CONFLICT"


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
