from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.skill.skill_manager import (
    ERROR_SKILLHUB_DETAIL_FAILED,
    ERROR_SKILLHUB_DETAIL_NOT_FOUND,
    SkillManager,
)


class SwarmSkillsHubDetailHarness(SkillManager):
    """公开受保护方法供单测."""

    def set_mock_get_data(self, mock_func) -> None:
        self._team_skills_hub_http_get_data = mock_func


def _plugin_list_payload(*, asset_id: str = "demo-skill", public_latest_version: str = "2.0.0") -> dict:
    return {
        "items": [
            {
                "asset_id": asset_id,
                "public_latest_version": public_latest_version,
                "latest_version": "2.1.0-pending",
                "like_count": 32,
                "star_count": 18,
                "review_count": 10,
                "average_rating": 4.8,
                "create_time": 1785830400000,
                "file_path": "/internal/should-not-leak",
            }
        ]
    }


def _version_detail_payload(*, asset_id: str = "demo-skill", version: str = "2.0.0") -> dict:
    return {
        "asset_id": asset_id,
        "version": version,
        "asset_type": "plugin",
        "plugin_type": "skill",
        "name": "document-review",
        "display_name": "Document Review",
        "short_desc": "Review documents and produce structured feedback.",
        "detail_desc": "# Document Review\n\nUse this Skill.",
        "icon_uri": "https://skillhub.example/assets/demo-skill/icon.png",
        "publisher_id": "publisher-001",
        "publisher_name": "OpenJiuwen",
        "tags": ["document", "review"],
        "category_id": "office",
        "category_name": "Office",
        "certification": None,
        "changelog": "Improve structured review output.",
        "install_count": 120,
        "view_count": 540,
        "update_time": 1786435200000,
        "review_summary": {
            "status": "APPROVED",
            "score": 92,
            "risk_level": "LOW",
            "failed_count": 0,
            "summary": "Suitable for document review scenarios.",
        },
        "review_sections": None,
        "file_path": "/internal/artifact.zip",
        "download_url": "https://example.invalid/secret",
    }


@pytest.mark.asyncio
async def test_swarmskillshub_detail_success_whitelist_merge(tmp_path):
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))
    calls: list[tuple[str, dict | None]] = []

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        calls.append((path, kwargs.get("params")))
        if path == "/api/v1/plugins":
            return _plugin_list_payload()
        assert path == "/api/v1/plugins/demo-skill/versions/2.0.0"
        return _version_detail_payload()

    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_swarm_skills_hub_detail({"asset_id": "demo-skill"})

    assert payload["success"] is True
    assert payload["asset_id"] == "demo-skill"
    assert payload["version"] == "2.0.0"
    data = payload["data"]
    assert data["version"] == "2.0.0"
    assert data["detail_desc"].startswith("# Document Review")
    assert data["like_count"] == 32
    assert data["star_count"] == 18
    assert data["review_count"] == 10
    assert data["average_rating"] == 4.8
    assert data["create_time"] == 1785830400000
    assert data["install_count"] == 120
    assert data["view_count"] == 540
    assert data["review_sections"] == []
    assert isinstance(data["review_summary"], dict)
    assert data["review_summary"]["status"] == "APPROVED"
    assert data["certification"] is None
    assert "file_path" not in data
    assert "download_url" not in data
    assert calls[0] == ("/api/v1/plugins", {"asset_id": "demo-skill", "page": 1, "page_size": 1})
    assert calls[1][0] == "/api/v1/plugins/demo-skill/versions/2.0.0"


@pytest.mark.asyncio
async def test_swarmskillshub_detail_uses_public_latest_not_latest_version(tmp_path):
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))
    seen_versions: list[str] = []

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        if path == "/api/v1/plugins":
            return _plugin_list_payload(public_latest_version="1.0.0")
        assert "/versions/" in path
        version = path.rsplit("/", 1)[-1]
        seen_versions.append(version)
        return _version_detail_payload(version=version)

    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_swarm_skills_hub_detail({"asset_id": "demo-skill"})
    assert payload["success"] is True
    assert payload["version"] == "1.0.0"
    assert seen_versions == ["1.0.0"]


@pytest.mark.asyncio
async def test_swarmskillshub_detail_empty_public_latest_not_found(tmp_path):
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        assert path == "/api/v1/plugins"
        return {
            "items": [
                {
                    "asset_id": "demo-skill",
                    "public_latest_version": "",
                    "latest_version": "",
                    "like_count": 0,
                    "star_count": 0,
                    "review_count": 0,
                    "average_rating": None,
                    "create_time": None,
                }
            ]
        }

    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_swarm_skills_hub_detail({"asset_id": "demo-skill"})
    assert payload["success"] is False
    assert payload["code"] == ERROR_SKILLHUB_DETAIL_NOT_FOUND
    assert payload["detail_key"] == "skills.swarmskillshub.errors.detailNotFound"


@pytest.mark.asyncio
async def test_swarmskillshub_detail_falls_back_to_latest_version_when_public_missing(tmp_path):
    """搜索命中无 public_latest_version 的资产时，回退 latest_version（对齐官网）。"""
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))
    seen_versions: list[str] = []

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        if path == "/api/v1/plugins":
            return {
                "items": [
                    {
                        "asset_id": "demo-skill",
                        "public_latest_version": None,
                        "latest_version": "1.0.0",
                        "like_count": 1,
                        "star_count": 0,
                        "review_count": 0,
                        "average_rating": None,
                        "create_time": None,
                    }
                ]
            }
        assert path == "/api/v1/plugins/demo-skill/versions/1.0.0"
        seen_versions.append("1.0.0")
        return _version_detail_payload(version="1.0.0")

    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_swarm_skills_hub_detail({"asset_id": "demo-skill"})
    assert payload["success"] is True
    assert payload["version"] == "1.0.0"
    assert seen_versions == ["1.0.0"]
    assert payload["data"]["detail_desc"].startswith("# Document Review")


@pytest.mark.asyncio
async def test_swarmskillshub_detail_asset_missing_not_found(tmp_path):
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        return {"items": [{"asset_id": "other-skill", "public_latest_version": "1.0.0"}]}

    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_swarm_skills_hub_detail({"asset_id": "demo-skill"})
    assert payload["success"] is False
    assert payload["code"] == ERROR_SKILLHUB_DETAIL_NOT_FOUND


@pytest.mark.asyncio
async def test_swarmskillshub_detail_rejects_unsafe_asset_id(tmp_path):
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))
    payload = await manager.handle_skills_swarm_skills_hub_detail({"asset_id": "../evil"})
    assert payload["success"] is False
    assert payload["code"] == ERROR_SKILLHUB_DETAIL_FAILED
    assert payload["detail_key"] == "skills.swarmskillshub.errors.detailFailed"


@pytest.mark.asyncio
async def test_swarmskillshub_detail_ignores_frontend_market_url_and_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_SKILLS_HUB_BASE_URL", "https://backend-hub.example")
    monkeypatch.setenv("TEAM_SKILLS_HUB_SYSTEM_TOKEN", "server-system-token")
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))
    seen: dict = {}

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        seen["base_url"] = kwargs.get("base_url")
        seen["token"] = kwargs.get("token")
        seen["system_token"] = kwargs.get("system_token")
        if path == "/api/v1/plugins":
            return _plugin_list_payload()
        return _version_detail_payload()

    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_swarm_skills_hub_detail(
        {
            "asset_id": "demo-skill",
            "market_url": "https://evil.example",
            "token": "frontend-token",
            "system_token": "frontend-system",
            "version": "9.9.9",
        }
    )
    assert payload["success"] is True
    assert seen["base_url"] == "https://backend-hub.example"
    assert seen["system_token"] == "server-system-token"
    assert not seen.get("token")
    assert payload["version"] == "2.0.0"


@pytest.mark.asyncio
async def test_swarmskillshub_detail_upstream_failure_mapped(tmp_path):
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        raise RuntimeError("Team Skills Hub API 错误 HTTP 500: secret-upstream-body")

    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_swarm_skills_hub_detail({"asset_id": "demo-skill"})
    assert payload["success"] is False
    assert payload["code"] == ERROR_SKILLHUB_DETAIL_FAILED
    assert payload["detail_key"] == "skills.swarmskillshub.errors.detailFailed"
    assert "secret-upstream-body" not in payload["detail"]


def test_req_method_swarmskillshub_detail_registered() -> None:
    assert ReqMethod.SKILLS_SWARMSKILLS_HUB_DETAIL.value == "skills.swarmskillshub.detail"


def _make_skill_zip_bytes(*, with_readme: bool = True, with_skill_md: bool = True) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if with_skill_md:
            zf.writestr(
                "demo-skill/SKILL.md",
                "---\nname: document-review\ndescription: demo\n---\n\n# From SKILL body\n",
            )
        if with_readme:
            zf.writestr("demo-skill/README.md", "# From README\n\nOfficial long description.\n")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_swarmskillshub_detail_skips_artifact_when_detail_desc_present(tmp_path, monkeypatch):
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))
    artifact_called = False

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        nonlocal artifact_called
        if path.startswith("/api/v1/artifacts/"):
            artifact_called = True
            raise AssertionError("should not download artifact when detail_desc present")
        if path == "/api/v1/plugins":
            return _plugin_list_payload()
        return _version_detail_payload()

    async def _unexpected_download(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("should not download zip")

    manager.set_mock_get_data(_fake_get_data)
    monkeypatch.setattr(manager, "_download_zip_and_verify", _unexpected_download)
    payload = await manager.handle_skills_swarm_skills_hub_detail({"asset_id": "demo-skill"})
    assert payload["success"] is True
    assert payload["data"]["detail_desc"].startswith("# Document Review")
    assert artifact_called is False


@pytest.mark.asyncio
async def test_swarmskillshub_detail_hydrates_from_readme_when_detail_desc_empty(tmp_path, monkeypatch):
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        if path == "/api/v1/plugins":
            return _plugin_list_payload()
        if path == "/api/v1/plugins/demo-skill/versions/2.0.0":
            payload = _version_detail_payload()
            payload["detail_desc"] = ""
            return payload
        assert path == "/api/v1/artifacts/demo-skill"
        assert kwargs.get("params") == {"version": "2.0.0"}
        return {
            "download_url": "https://cdn.example/demo.zip",
            "checksum_sha256": "",
            "version": "2.0.0",
        }

    async def _fake_download(url, **kwargs):  # noqa: ANN001
        assert url == "https://cdn.example/demo.zip"
        return _make_skill_zip_bytes(with_readme=True, with_skill_md=True)

    manager.set_mock_get_data(_fake_get_data)
    monkeypatch.setattr(manager, "_assert_team_skills_hub_download_url_allowed", lambda url: None)
    monkeypatch.setattr(manager, "_download_zip_and_verify", _fake_download)
    payload = await manager.handle_skills_swarm_skills_hub_detail({"asset_id": "demo-skill"})
    assert payload["success"] is True
    assert payload["data"]["detail_desc"].startswith("# From README")


@pytest.mark.asyncio
async def test_swarmskillshub_detail_hydrates_from_skill_md_body_without_readme(tmp_path, monkeypatch):
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        if path == "/api/v1/plugins":
            return _plugin_list_payload()
        if "/versions/" in path:
            payload = _version_detail_payload()
            payload["detail_desc"] = "   "
            return payload
        return {"download_url": "https://cdn.example/demo.zip", "checksum_sha256": ""}

    async def _fake_download(url, **kwargs):  # noqa: ANN001
        return _make_skill_zip_bytes(with_readme=False, with_skill_md=True)

    manager.set_mock_get_data(_fake_get_data)
    monkeypatch.setattr(manager, "_assert_team_skills_hub_download_url_allowed", lambda url: None)
    monkeypatch.setattr(manager, "_download_zip_and_verify", _fake_download)
    payload = await manager.handle_skills_swarm_skills_hub_detail({"asset_id": "demo-skill"})
    assert payload["success"] is True
    assert "# From SKILL body" in payload["data"]["detail_desc"]


@pytest.mark.asyncio
async def test_swarmskillshub_detail_hydrates_readme_at_package_root(tmp_path, monkeypatch):
    """README 在包根、SKILL.md 在子目录时仍应回填 README（对齐官网）."""
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        if path == "/api/v1/plugins":
            return _plugin_list_payload()
        if "/versions/" in path:
            payload = _version_detail_payload()
            payload["detail_desc"] = ""
            return payload
        return {"download_url": "https://cdn.example/demo.zip", "checksum_sha256": ""}

    def _zip_readme_at_root() -> bytes:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("pkg/readme.md", "# Root README\n\nLong official docs.\n")
            zf.writestr(
                "pkg/nested/SKILL.md",
                "---\nname: document-review\ndescription: short\n---\n\nshort body\n",
            )
        return buf.getvalue()

    async def _fake_download(url, **kwargs):  # noqa: ANN001
        return _zip_readme_at_root()

    manager.set_mock_get_data(_fake_get_data)
    monkeypatch.setattr(manager, "_assert_team_skills_hub_download_url_allowed", lambda url: None)
    monkeypatch.setattr(manager, "_download_zip_and_verify", _fake_download)
    payload = await manager.handle_skills_swarm_skills_hub_detail({"asset_id": "demo-skill"})
    assert payload["success"] is True
    assert payload["data"]["detail_desc"].startswith("# Root README")
    assert "Long official docs" in payload["data"]["detail_desc"]


@pytest.mark.asyncio
async def test_swarmskillshub_detail_artifact_failure_soft_fallback(tmp_path, monkeypatch):
    manager = SwarmSkillsHubDetailHarness(workspace_dir=str(tmp_path))

    async def _fake_get_data(path, **kwargs):  # noqa: ANN001
        if path == "/api/v1/plugins":
            return _plugin_list_payload()
        if "/versions/" in path:
            payload = _version_detail_payload()
            payload["detail_desc"] = ""
            return payload
        raise RuntimeError("artifact unavailable")

    manager.set_mock_get_data(_fake_get_data)
    payload = await manager.handle_skills_swarm_skills_hub_detail({"asset_id": "demo-skill"})
    assert payload["success"] is True
    assert payload["data"]["detail_desc"] == ""
    assert payload["data"]["short_desc"]
