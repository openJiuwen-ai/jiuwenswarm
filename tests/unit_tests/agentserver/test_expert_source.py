# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ExpertPackageSource 抽象 / HTTP 实现 / 本地 override / 包校验 / metadata expert_id 测试。

mock 仓库契约即未来正式仓库的替换验收依据。
np:// 命名管道形态：经 serve_pipe 假仓库端到端（HTTP/1.1 over 管道，Windows only）。
"""

import io
import json
import os
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

import jiuwenswarm.server.runtime.session.session_metadata as sm
from jiuwenswarm.server.runtime.expert import expert_store as es


def _make_package(
        root: Path,
        name: str,
        *,
        rails: bool = False,
        subagents: bool = False,
        model: bool = False,
        persona: bool = True,
        tools: list[str] | None = None,
        skills: list[str] | None = None,
        card_id: str | None = None,
) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True)
    manifest: dict = {
        "packageType": "agent_template",
        "agentCard": {
            "id": card_id if card_id is not None else name,
            "name": f"{name} 专家",
            "description": "描述",
        },
        "persona": {"dir": "agents"},
        "metadata": {"tags": ["test"], "profession": "头衔"},
    }
    if rails:
        manifest["rails"] = [{"file": "rails/x.py", "class": "X"}]
    if subagents:
        manifest["subagents"] = [{"dir": "subagents/x"}]
    if model:
        manifest["model"] = {"name": "ignored"}
    if persona:
        (pkg / "agents").mkdir()
        (pkg / "agents" / "00-identity.md").write_text("# 人设", encoding="utf-8")
    if tools:
        for tool_file in tools:
            tool_path = pkg / tool_file
            tool_path.parent.mkdir(parents=True, exist_ok=True)
            tool_path.write_text("# tool", encoding="utf-8")
        manifest["tools"] = [{"file": f} for f in tools]
    if skills:
        # skill 条目是叶子目录引用（{"dir": "skills/xxx"}），目录下须含 SKILL.md
        for skill_dir in skills:
            sd = pkg / skill_dir
            sd.mkdir(parents=True, exist_ok=True)
            (sd / "SKILL.md").write_text(
                "---\nname: test-skill\ndescription: 测试技能\n---\n# body\n",
                encoding="utf-8",
            )
        manifest["skills"] = [{"dir": d} for d in skills]
    (pkg / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return pkg


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test-repo"
    )


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "experts_cache"
    monkeypatch.setattr(es, "get_expert_cache_dir", lambda: target)
    return target


@pytest.mark.asyncio
async def test_list_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/packages"
        return httpx.Response(
            200,
            json={
                "experts": [
                    {
                        "id": "security-reviewer",
                        "name": "安全评审专家",
                        "description": "描述",
                        "available": True,
                        "unavailable_reason": "",
                        "tags": ["security"],
                        "type": "agent",
                        "metadata": {"profession": "安全架构师"},
                        "avatar_url": "http://repo/api/v1/packages/security-reviewer/avatar",
                    }
                ]
            },
        )

    source = es.HttpRepoExpertPackageSource(client=_mock_client(handler))
    (summary,) = await source.list()
    assert summary.id == "security-reviewer"
    assert summary.source == "repo"
    assert summary.available is True
    assert summary.metadata["profession"] == "安全架构师"
    assert summary.avatar_url.endswith("/security-reviewer/avatar")


@pytest.mark.asyncio
async def test_list_repo_down_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    source = es.HttpRepoExpertPackageSource(client=_mock_client(handler))
    with pytest.raises(es.ExpertRepoUnavailable):
        await source.list()


@pytest.mark.asyncio
async def test_list_non_200_raises_unavailable() -> None:
    source = es.HttpRepoExpertPackageSource(
        client=_mock_client(lambda request: httpx.Response(500))
    )
    with pytest.raises(es.ExpertRepoUnavailable):
        await source.list()


@pytest.mark.asyncio
async def test_fetch_ok_extracts_to_cache(cache_dir: Path) -> None:
    payload = _zip_bytes(
        {"manifest.json": "{}", "agents/00-identity.md": "# 人设"}
    )
    source = es.HttpRepoExpertPackageSource(
        client=_mock_client(lambda request: httpx.Response(200, content=payload))
    )
    package_dir = await source.fetch("security-reviewer")
    assert package_dir == cache_dir / "security-reviewer"
    assert (package_dir / "manifest.json").is_file()
    assert (package_dir / "agents" / "00-identity.md").read_text(
        encoding="utf-8"
    ) == "# 人设"


@pytest.mark.asyncio
async def test_fetch_strips_single_top_level_dir(cache_dir: Path) -> None:
    """上架工具「连目录一起压缩」的形态：条目全在一个一级目录下，自动剥层。"""
    payload = _zip_bytes(
        {
            "doc-writer/": "",
            "doc-writer/manifest.json": "{}",
            "doc-writer/agents/": "",
            "doc-writer/agents/00-identity.md": "# 人设",
        }
    )
    source = es.HttpRepoExpertPackageSource(
        client=_mock_client(lambda request: httpx.Response(200, content=payload))
    )
    package_dir = await source.fetch("doc-writer")
    assert (package_dir / "manifest.json").is_file()
    assert not (package_dir / "doc-writer").exists()
    assert (package_dir / "agents" / "00-identity.md").read_text(
        encoding="utf-8"
    ) == "# 人设"


@pytest.mark.asyncio
async def test_fetch_no_strip_when_multiple_top_levels(cache_dir: Path) -> None:
    """存在第二个顶层名时不是包裹形态，原样解压（由装载校验报错）。"""
    payload = _zip_bytes(
        {"a/manifest.json": "{}", "b/readme.md": "x"}
    )
    source = es.HttpRepoExpertPackageSource(
        client=_mock_client(lambda request: httpx.Response(200, content=payload))
    )
    package_dir = await source.fetch("security-reviewer")
    assert not (package_dir / "manifest.json").exists()
    assert (package_dir / "a" / "manifest.json").is_file()
    assert (package_dir / "b" / "readme.md").is_file()


@pytest.mark.asyncio
async def test_fetch_flat_zip_with_dir_entries_not_stripped(cache_dir: Path) -> None:
    """平铺但带目录条目的 zip：根级文件存在即判定非包裹形态，原样解压。"""
    payload = _zip_bytes(
        {
            "agents/": "",
            "agents/00-identity.md": "# 人设",
            "manifest.json": "{}",
        }
    )
    source = es.HttpRepoExpertPackageSource(
        client=_mock_client(lambda request: httpx.Response(200, content=payload))
    )
    package_dir = await source.fetch("security-reviewer")
    assert (package_dir / "manifest.json").is_file()
    assert (package_dir / "agents" / "00-identity.md").is_file()


@pytest.mark.asyncio
async def test_fetch_404_raises_not_found() -> None:
    source = es.HttpRepoExpertPackageSource(
        client=_mock_client(lambda request: httpx.Response(404))
    )
    with pytest.raises(es.ExpertNotFound):
        await source.fetch("nope")


@pytest.mark.asyncio
async def test_fetch_down_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    source = es.HttpRepoExpertPackageSource(client=_mock_client(handler))
    with pytest.raises(es.ExpertRepoUnavailable):
        await source.fetch("security-reviewer")


@pytest.mark.asyncio
async def test_fetch_rejects_zip_slip(cache_dir: Path) -> None:
    payload = _zip_bytes({"../evil.txt": "x"})
    source = es.HttpRepoExpertPackageSource(
        client=_mock_client(lambda request: httpx.Response(200, content=payload))
    )
    with pytest.raises(es.ExpertRepoUnavailable):
        await source.fetch("security-reviewer")
    assert not (cache_dir.parent / "evil.txt").exists()


@pytest.mark.asyncio
async def test_fetch_replaces_stale_cache(cache_dir: Path) -> None:
    stale = cache_dir / "security-reviewer"
    (stale / "old").mkdir(parents=True)
    (stale / "old" / "stale.txt").write_text("stale", encoding="utf-8")
    payload = _zip_bytes({"manifest.json": "{}"})
    source = es.HttpRepoExpertPackageSource(
        client=_mock_client(lambda request: httpx.Response(200, content=payload))
    )
    package_dir = await source.fetch("security-reviewer")
    assert not (package_dir / "old").exists(), "旧缓存目录应先清空再解压"
    assert (package_dir / "manifest.json").is_file()


@pytest.mark.asyncio
async def test_local_override_wins_on_fetch(tmp_path: Path) -> None:
    local_dir = tmp_path / "experts"
    _make_package(local_dir, "security-reviewer")
    local = es.LocalDirExpertPackageSource(experts_dir=local_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("local 命中时不应访问仓库")

    repo = es.HttpRepoExpertPackageSource(client=_mock_client(handler))
    chain = es.ChainExpertPackageSource([local, repo])
    assert await chain.fetch("security-reviewer") == local_dir / "security-reviewer"


@pytest.mark.asyncio
async def test_chain_fetch_falls_back_to_repo(
        tmp_path: Path, cache_dir: Path
) -> None:
    local = es.LocalDirExpertPackageSource(experts_dir=tmp_path / "experts")
    payload = _zip_bytes({"manifest.json": "{}"})
    repo = es.HttpRepoExpertPackageSource(
        client=_mock_client(lambda request: httpx.Response(200, content=payload))
    )
    chain = es.ChainExpertPackageSource([local, repo])
    assert await chain.fetch("security-reviewer") == cache_dir / "security-reviewer"


@pytest.mark.asyncio
async def test_chain_list_merges_local_over_repo(tmp_path: Path) -> None:
    local_dir = tmp_path / "experts"
    _make_package(local_dir, "security-reviewer")
    local = es.LocalDirExpertPackageSource(experts_dir=local_dir)
    repo = es.HttpRepoExpertPackageSource(
        client=_mock_client(
            lambda request: httpx.Response(
                200,
                json={
                    "experts": [
                        {"id": "security-reviewer", "name": "仓库版", "available": True},
                        {"id": "other", "name": "另一个", "available": True},
                    ]
                },
            )
        )
    )
    chain = es.ChainExpertPackageSource([local, repo])
    summaries = {s.id: s for s in await chain.list()}
    assert summaries["security-reviewer"].source == "local", "同名时 local 覆盖 repo"
    assert summaries["other"].source == "repo"


@pytest.mark.asyncio
async def test_chain_list_raises_when_all_empty_and_repo_down(tmp_path: Path) -> None:
    local = es.LocalDirExpertPackageSource(experts_dir=tmp_path / "experts")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    repo = es.HttpRepoExpertPackageSource(client=_mock_client(handler))
    chain = es.ChainExpertPackageSource([local, repo])
    with pytest.raises(es.ExpertRepoUnavailable):
        await chain.list()


@pytest.mark.asyncio
async def test_resolve_package_dir_cache_hit_skips_fetch(
        cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缓存命中时直接返回缓存目录，不触 source（重建路径不被网络阻塞）。"""
    cached = cache_dir / "security-reviewer"
    cached.mkdir(parents=True)
    (cached / "manifest.json").write_text("{}", encoding="utf-8")

    class _BoomSource:
        async def fetch(self, expert_id: str) -> Path:
            raise AssertionError("缓存命中时不应 fetch")

    monkeypatch.setattr(es, "get_expert_source", lambda: _BoomSource())
    assert await es.resolve_expert_package_dir("security-reviewer") == cached


@pytest.mark.asyncio
async def test_resolve_package_dir_miss_falls_back_to_fetch(
        tmp_path: Path, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缓存 miss 回退 source.fetch——LocalDir 调试源不落缓存场景的兜底。"""
    local_pkg = _make_package(tmp_path / "experts", "sample-group")
    fetch_calls: list[str] = []

    class _LocalLikeSource:
        async def fetch(self, expert_id: str) -> Path:
            fetch_calls.append(expert_id)
            return local_pkg

    monkeypatch.setattr(es, "get_expert_source", lambda: _LocalLikeSource())
    assert await es.resolve_expert_package_dir("sample-group") == local_pkg
    assert fetch_calls == ["sample-group"]


def test_validate_ok_with_tools_warning_free(tmp_path: Path) -> None:
    pkg = _make_package(tmp_path, "security-reviewer", tools=["tools/scan.py"])
    assert es.validate_expert_package(pkg) == []


def test_validate_model_field_warns(tmp_path: Path) -> None:
    pkg = _make_package(tmp_path, "security-reviewer", model=True)
    warnings = es.validate_expert_package(pkg)
    assert any("model" in w for w in warnings)


@pytest.mark.parametrize(
    "kwargs, reason_part",
    [
        ({"rails": True}, "rails"),
        ({"subagents": True}, "subagents"),
        ({"persona": False}, "persona"),
        ({"card_id": "other-id"}, "不一致"),
    ],
)
def test_validate_rejects_invalid(tmp_path: Path, kwargs: dict, reason_part: str) -> None:
    pkg = _make_package(tmp_path, "security-reviewer", **kwargs)
    with pytest.raises(es.InvalidExpertPackage, match=reason_part):
        es.validate_expert_package(pkg)


def test_validate_rejects_missing_tool_file(tmp_path: Path) -> None:
    pkg = _make_package(tmp_path, "security-reviewer")
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    manifest["tools"] = [{"file": "tools/missing.py"}]
    (pkg / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(es.InvalidExpertPackage, match="tools"):
        es.validate_expert_package(pkg)


def test_validate_ok_with_skills(tmp_path: Path) -> None:
    """合法 skills（叶子形态：dir 含 SKILL.md）应无警告通过。"""
    pkg = _make_package(tmp_path, "security-reviewer", skills=["skills/threat-modeling"])
    assert es.validate_expert_package(pkg) == []


def test_validate_rejects_skill_missing_dir(tmp_path: Path) -> None:
    """skills dir 指向不存在的目录 → 拒载。"""
    pkg = _make_package(tmp_path, "security-reviewer")
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    manifest["skills"] = [{"dir": "skills/missing"}]
    (pkg / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(es.InvalidExpertPackage, match="skills dir"):
        es.validate_expert_package(pkg)


def test_validate_rejects_skill_missing_skill_md(tmp_path: Path) -> None:
    """skills dir 存在但无 SKILL.md（非叶子形态）→ 拒载。"""
    pkg = _make_package(tmp_path, "security-reviewer")
    (pkg / "skills" / "empty").mkdir(parents=True)
    (pkg / "skills" / "empty" / ".keep").write_text("", encoding="utf-8")
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    manifest["skills"] = [{"dir": "skills/empty"}]
    (pkg / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(es.InvalidExpertPackage, match="SKILL.md"):
        es.validate_expert_package(pkg)


def test_validate_rejects_skill_dir_escape(tmp_path: Path) -> None:
    """skills dir 路径逃逸包目录 → 拒载（与 zip slip 同规）。"""
    pkg = _make_package(tmp_path, "security-reviewer")
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    manifest["skills"] = [{"dir": "../outside"}]
    (pkg / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(es.InvalidExpertPackage, match="逃逸"):
        es.validate_expert_package(pkg)


def test_validate_rejects_skill_entry_without_dir(tmp_path: Path) -> None:
    """skills 条目缺 dir 键 → 拒载。"""
    pkg = _make_package(tmp_path, "security-reviewer")
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    manifest["skills"] = [{"mode": "all"}]
    (pkg / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(es.InvalidExpertPackage, match="dir"):
        es.validate_expert_package(pkg)


def test_validate_rejects_missing_manifest(tmp_path: Path) -> None:
    (tmp_path / "empty-pkg").mkdir()
    with pytest.raises(es.InvalidExpertPackage, match="manifest"):
        es.validate_expert_package(tmp_path / "empty-pkg")


def test_validate_avatar_declared_but_missing(tmp_path: Path) -> None:
    pkg = _make_package(tmp_path, "security-reviewer")
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    manifest["metadata"]["avatar"] = "avatars/missing.png"
    (pkg / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(es.InvalidExpertPackage, match="头像"):
        es.validate_expert_package(pkg)


def test_validate_avatar_ok(tmp_path: Path) -> None:
    pkg = _make_package(tmp_path, "security-reviewer")
    (pkg / "avatars").mkdir()
    (pkg / "avatars" / "expert.png").write_bytes(b"\x89PNG")
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    manifest["metadata"]["avatar"] = "avatars/expert.png"
    (pkg / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    assert es.validate_expert_package(pkg) == []


@pytest.fixture
def sessions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "sessions"
    target.mkdir()
    monkeypatch.setattr(sm, "get_agent_sessions_dir", lambda: target)
    sm._METADATA_CACHE.clear()
    yield target
    sm._METADATA_CACHE.clear()


def test_metadata_init_defaults_expert_id_empty(sessions_dir: Path) -> None:
    sm.init_session_metadata(session_id="s1", channel_id="desktop")
    assert sm.get_session_metadata("s1", cache_bust=True)["expert_id"] == ""


def test_metadata_init_with_expert_id(sessions_dir: Path) -> None:
    sm.init_session_metadata(
        session_id="s2", channel_id="desktop", expert_id="security-reviewer"
    )
    assert (
            sm.get_session_metadata("s2", cache_bust=True)["expert_id"]
            == "security-reviewer"
    )


def test_metadata_update_and_clear_expert_id(sessions_dir: Path) -> None:
    sm.init_session_metadata(session_id="s3", channel_id="desktop")
    sm.update_session_metadata(
        session_id="s3", expert_id="security-reviewer", sync_write=True
    )
    assert (
            sm.get_session_metadata("s3", cache_bust=True)["expert_id"]
            == "security-reviewer"
    )
    sm.update_session_metadata(session_id="s3", expert_id="", sync_write=True)
    assert sm.get_session_metadata("s3", cache_bust=True)["expert_id"] == ""


def test_metadata_legacy_session_without_key(sessions_dir: Path) -> None:
    """旧会话 metadata.json 无 expert_id 键时读出默认 ""（无需迁移）。"""
    session_dir = sessions_dir / "legacy"
    session_dir.mkdir()
    (session_dir / "metadata.json").write_text(
        json.dumps({"session_id": "legacy", "title": "旧会话"}), encoding="utf-8"
    )
    assert sm.get_session_metadata("legacy", cache_bust=True)["expert_id"] == ""


class TestRepoClientShape:
    """base_url scheme 分流：np:// → 管道 transport；http(s):// → 保持现状（纯构造，跨平台）。"""

    @pytest.mark.asyncio
    async def test_np_url_uses_named_pipe_transport(self) -> None:
        from jiuwenswarm.common import np_transport

        source = es.HttpRepoExpertPackageSource(base_url="np://claw-expert-repo")
        client = source._http()
        try:
            assert isinstance(client._transport, np_transport.NamedPipeTransport)
            assert client._transport._pipe_path == r"\\.\pipe\claw-expert-repo"
            assert client.trust_env is False, "np:// 形态须屏蔽代理 env"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_http_url_keeps_default_transport(self) -> None:
        from jiuwenswarm.common import np_transport

        source = es.HttpRepoExpertPackageSource(base_url="http://127.0.0.1:18901")
        client = source._http()
        try:
            assert not isinstance(client._transport, np_transport.NamedPipeTransport)
            assert client.trust_env is True, "http 形态行为不变（默认 trust_env）"
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_injected_client_still_wins(self) -> None:
        injected = _mock_client(lambda request: httpx.Response(200, json={"experts": []}))
        source = es.HttpRepoExpertPackageSource(
            base_url="np://claw-expert-repo", client=injected
        )
        assert source._http() is injected
        await injected.aclose()


@pytest.mark.skipif(sys.platform != "win32", reason="命名管道仅 Windows")
class TestHttpRepoOverNamedPipe:
    """np:// base_url 经 serve_pipe 假仓库的端到端（HTTP/1.1 over 命名管道）。"""

    async def _start_repo_pipe(self, tag: str, responder):
        """responder(request_line: str) -> (status, body_bytes)；返回 (base_url, server)。"""
        from jiuwenswarm.common import np_transport

        base_url = f"np://claw-test-np-expert-{os.getpid()}-{tag}"

        async def handle(stream) -> None:
            buf = bytearray()
            while b"\r\n\r\n" not in buf:
                chunk = await stream.read()
                if not chunk:
                    return
                buf.extend(chunk)
            head = bytes(buf).split(b"\r\n\r\n", 1)[0]
            request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            status, body = responder(request_line)
            payload = (
                f"HTTP/1.1 {status} OK\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Content-Type: application/octet-stream\r\n"
                "\r\n"
            ).encode("latin-1") + body
            await stream.write(payload)

        server = await np_transport.serve_pipe(
            np_transport.pipe_path_from_url(base_url), handle
        )
        return base_url, server

    @pytest.mark.asyncio
    async def test_np_list_ok(self) -> None:
        def responder(request_line: str):
            assert request_line == "GET /api/v1/packages HTTP/1.1"
            return 200, json.dumps(
                {
                    "experts": [
                        {
                            "id": "security-reviewer",
                            "name": "安全评审专家",
                            "description": "描述",
                            "available": True,
                            "tags": ["security"],
                        }
                    ]
                }
            ).encode()

        base_url, server = await self._start_repo_pipe("list", responder)
        try:
            source = es.HttpRepoExpertPackageSource(base_url=base_url)
            (summary,) = await source.list()
            assert summary.id == "security-reviewer"
            assert summary.source == "repo"
            assert summary.available is True
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_np_fetch_ok_via_env(
            self, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """桌面注入形态：JIUWEN_EXPERT_REPO_URL=np://<管道名>（构造参数缺省走 env）。"""
        payload = _zip_bytes({"manifest.json": "{}", "agents/00-identity.md": "# 人设"})

        def responder(request_line: str):
            assert request_line == "GET /api/v1/packages/security-reviewer HTTP/1.1"
            return 200, payload

        base_url, server = await self._start_repo_pipe("fetch", responder)
        try:
            monkeypatch.setenv(es.REPO_URL_ENV, base_url)
            source = es.HttpRepoExpertPackageSource()
            package_dir = await source.fetch("security-reviewer")
            assert package_dir == cache_dir / "security-reviewer"
            assert (package_dir / "manifest.json").is_file()
            assert (package_dir / "agents" / "00-identity.md").read_text(
                encoding="utf-8"
            ) == "# 人设"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_np_fetch_404_raises_not_found(self) -> None:
        base_url, server = await self._start_repo_pipe(
            "404", lambda request_line: (404, b"not found")
        )
        try:
            source = es.HttpRepoExpertPackageSource(base_url=base_url)
            with pytest.raises(es.ExpertNotFound):
                await source.fetch("nope")
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_np_pipe_down_raises_unavailable(
            self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """管道不可达（桌面代理未起）→ ExpertRepoUnavailable（与 http 连接失败同语义）。

        直接桩掉 open_pipe 立即抛错，避免真实连接 10s 超时拖慢用例。
        """
        from jiuwenswarm.common import np_transport

        async def _boom(path: str, *, timeout: float = 10.0):
            raise np_transport.PipeTimeoutError(f"管道未监听: {path}")

        monkeypatch.setattr(np_transport, "open_pipe", _boom)
        source = es.HttpRepoExpertPackageSource(base_url="np://claw-test-np-expert-down")
        with pytest.raises(es.ExpertRepoUnavailable):
            await source.list()
def test_metadata_init_defaults_expert_type_agent(sessions_dir: Path) -> None:
    sm.init_session_metadata(session_id="t1", channel_id="desktop")
    assert sm.get_session_metadata("t1", cache_bust=True)["expert_type"] == "agent"


def test_metadata_init_and_update_expert_type(sessions_dir: Path) -> None:
    sm.init_session_metadata(
        session_id="t2",
        channel_id="desktop",
        expert_id="sample-expert-group",
        expert_type="team",
        mode="team",
        team_name="expert-group-sample-expert-group-t2",
        team_template_id="expert_group",
    )
    metadata = sm.get_session_metadata("t2", cache_bust=True)
    assert metadata["expert_type"] == "team"
    assert metadata["mode"] == "team"
    assert metadata["team_name"] == "expert-group-sample-expert-group-t2"
    sm.update_session_metadata(
        session_id="t2",
        expert_id="",
        expert_type="agent",
        team_name="",
        team_template_id="",
        mode="agent",
        sync_write=True,
    )
    metadata = sm.get_session_metadata("t2", cache_bust=True)
    assert metadata["expert_type"] == "agent"
    assert metadata["mode"] == "agent"
    assert metadata["team_name"] == ""


def test_metadata_legacy_session_defaults_expert_type_agent(sessions_dir: Path) -> None:
    """旧会话 metadata.json 无 expert_type 键时读出默认 "agent"（无需迁移）。"""
    session_dir = sessions_dir / "legacy2"
    session_dir.mkdir()
    (session_dir / "metadata.json").write_text(
        json.dumps({"session_id": "legacy2", "title": "旧会话"}), encoding="utf-8"
    )
    assert sm.get_session_metadata("legacy2", cache_bust=True)["expert_type"] == "agent"
