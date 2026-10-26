import json

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


class _FakeSearchResponse:
    def __init__(self, data: dict):
        self._data = data

    def json(self):
        return self._data

    @staticmethod
    def raise_for_status():
        return None


class _FakeSearchClient:
    def __init__(self, results: list[dict]):
        self._results = results

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, *, params, headers):
        assert url == "https://clawhub.ai/api/v1/search"
        assert "q" in params
        return _FakeSearchResponse({"results": self._results})


class _FakeDownloadResponse:
    def __init__(self, status_code: int, text: str = "", content: bytes = b""):
        self.status_code = status_code
        self.text = text
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                message=f"Client error '{self.status_code}'",
                request=httpx.Request("GET", "https://clawhub.ai/api/v1/download"),
                response=self,
            )


class _FakeDownloadClient:
    def __init__(self, status_code: int = 200, text: str = "", content: bytes = b""):
        self._status_code = status_code
        self._text = text
        self._content = content
        self._captured_params: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, *, params, headers):
        assert url == "https://clawhub.ai/api/v1/download"
        self._captured_params = params
        return _FakeDownloadResponse(
            status_code=self._status_code,
            text=self._text,
            content=self._content,
        )

    def get_captured_params(self) -> dict | None:
        return self._captured_params


def _zip_bytes(entries: dict[str, str | bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_clawhub_search_returns_owner_handle(tmp_path, monkeypatch):
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    await manager.handle_skills_clawhub_set_token({"token": "test-token"})

    fake_results = [
        {
            "slug": "ppt-generator",
            "displayName": "PPT Generator",
            "summary": "Generate PPT",
            "version": "1.0.0",
            "updatedAt": 1000000,
            "ownerHandle": "kirkraman",
        },
        {
            "slug": "ppt-generator",
            "displayName": "PPT Gen 2",
            "summary": "Another PPT",
            "version": "2.0.0",
            "updatedAt": 2000000,
            "ownerHandle": "wwlyzzyorg",
        },
    ]

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.httpx.AsyncClient",
        lambda timeout: _FakeSearchClient(fake_results),
    )

    result = await manager.handle_skills_clawhub_search({"q": "ppt-generator", "limit": 50})

    assert result["success"] is True
    assert len(result["skills"]) == 2
    assert result["skills"][0]["owner_handle"] == "kirkraman"
    assert result["skills"][1]["owner_handle"] == "wwlyzzyorg"


@pytest.mark.asyncio
async def test_clawhub_search_owner_handle_missing_is_empty_string(tmp_path, monkeypatch):
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    await manager.handle_skills_clawhub_set_token({"token": "test-token"})

    fake_results = [
        {
            "slug": "unique-skill",
            "displayName": "Unique Skill",
            "summary": "Only one publisher",
            "version": "1.0.0",
            "updatedAt": 1000000,
        },
    ]

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.httpx.AsyncClient",
        lambda timeout: _FakeSearchClient(fake_results),
    )

    result = await manager.handle_skills_clawhub_search({"q": "unique-skill"})

    assert result["success"] is True
    assert result["skills"][0]["owner_handle"] == ""


@pytest.mark.asyncio
async def test_clawhub_download_passes_owner_handle_to_api(tmp_path, monkeypatch):
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    await manager.handle_skills_clawhub_set_token({"token": "test-token"})

    fake_client = _FakeDownloadClient(status_code=400, text="error")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    await manager.handle_skills_clawhub_download(
        {"slug": "ppt-generator", "owner_handle": "kirkraman"}
    )

    captured = fake_client.get_captured_params()
    assert captured is not None
    assert captured["slug"] == "ppt-generator"
    assert captured["ownerHandle"] == "kirkraman"


@pytest.mark.asyncio
async def test_clawhub_download_without_owner_handle_omits_param(tmp_path, monkeypatch):
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    await manager.handle_skills_clawhub_set_token({"token": "test-token"})

    fake_client = _FakeDownloadClient(status_code=400, text="error")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    await manager.handle_skills_clawhub_download({"slug": "unique-skill"})

    captured = fake_client.get_captured_params()
    assert captured is not None
    assert captured["slug"] == "unique-skill"
    assert "ownerHandle" not in captured


@pytest.mark.asyncio
async def test_clawhub_download_empty_owner_handle_omits_param(tmp_path, monkeypatch):
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    await manager.handle_skills_clawhub_set_token({"token": "test-token"})

    fake_client = _FakeDownloadClient(status_code=400, text="error")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    await manager.handle_skills_clawhub_download(
        {"slug": "unique-skill", "owner_handle": ""}
    )

    captured = fake_client.get_captured_params()
    assert captured is not None
    assert "ownerHandle" not in captured


@pytest.mark.asyncio
async def test_clawhub_download_records_owner_qualified_origin(tmp_path, monkeypatch):
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    await manager.handle_skills_clawhub_set_token({"token": "test-token"})

    zip_content = _zip_bytes(
        {
            "weather/SKILL.md": "---\nname: weather\nversion: 1.0.0\n---\nbody\n",
        }
    )
    fake_client = _FakeDownloadClient(status_code=200, content=zip_content)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    result = await manager.handle_skills_clawhub_download(
        {"slug": "weather", "owner_handle": "openclaw", "display_name": "Weather"}
    )

    assert result["success"] is True
    # 内部标识名须与磁盘解析出的规范名一致（此处等于 slug），
    # 否则会被自动扫描逻辑当作"未登记的本地技能"重复注册一条幽灵记录。
    assert result["skill"]["name"] == "weather"
    assert result["skill"]["display_name"] == "Weather"
    local = manager.get_local_skills()
    assert any(
        item.get("origin") == "clawhub:openclaw/weather" and item.get("name") == "weather"
        for item in local
    )
    assert any(item.get("display_name") == "Weather" for item in local)


@pytest.mark.asyncio
async def test_clawhub_download_without_owner_keeps_slug_origin(tmp_path, monkeypatch):
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    await manager.handle_skills_clawhub_set_token({"token": "test-token"})

    zip_content = _zip_bytes(
        {
            "unique-skill/SKILL.md": "---\nname: unique-skill\nversion: 1.0.0\n---\nbody\n",
        }
    )
    fake_client = _FakeDownloadClient(status_code=200, content=zip_content)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    result = await manager.handle_skills_clawhub_download({"slug": "unique-skill"})

    assert result["success"] is True
    local = manager.get_local_skills()
    assert any(item.get("origin") == "clawhub:unique-skill" for item in local)


@pytest.mark.asyncio
async def test_install_skill_parses_owner_handle_from_identifier(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.tools.skill_toolkits import SkillToolkit

    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    await manager.handle_skills_clawhub_set_token({"token": "test-token"})

    toolkit = SkillToolkit(manager=manager)

    fake_client = _FakeDownloadClient(status_code=400, text="error")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    result = await toolkit.install_skill(
        identifier="kirkraman/ppt-generator",
        source="clawhub",
    )

    captured = fake_client.get_captured_params()
    assert captured is not None
    assert captured["slug"] == "ppt-generator"
    assert captured["ownerHandle"] == "kirkraman"


@pytest.mark.asyncio
async def test_install_skill_plain_slug_no_owner_handle(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.tools.skill_toolkits import SkillToolkit

    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    await manager.handle_skills_clawhub_set_token({"token": "test-token"})

    toolkit = SkillToolkit(manager=manager)

    fake_client = _FakeDownloadClient(status_code=400, text="error")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.httpx.AsyncClient",
        lambda timeout: fake_client,
    )

    result = await toolkit.install_skill(
        identifier="unique-skill",
        source="clawhub",
    )

    captured = fake_client.get_captured_params()
    assert captured is not None
    assert captured["slug"] == "unique-skill"
    assert "ownerHandle" not in captured
