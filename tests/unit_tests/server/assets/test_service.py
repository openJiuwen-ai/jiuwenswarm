# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""kind 注册表、assets.export / assets.import 编排层与 WS 入口 handler 单测。

真实适配器（agent_template / agent_group / skill）由各自模块的测试覆盖；
本文件用 fake adapter 验证注册、分发、嗅探、暂存路径校验与错误收敛。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.safe_archive import SafeArchiveError
from jiuwenswarm.server.runtime.assets import registry, service
from jiuwenswarm.server.runtime.assets.contract import AssetKind
from jiuwenswarm.server.runtime.assets.errors import AssetError, AssetErrorCode
from jiuwenswarm.server.runtime.assets.registry import ExportSource, ImportOutcome
from jiuwenswarm.server.runtime.assets.staging import (
    STAGING_TTL_SECONDS,
    assets_export_root,
    assets_upload_root,
)


class _FakeAdapter:
    """按构造参数模拟各类适配器行为。"""

    def __init__(
        self,
        kind: AssetKind = AssetKind.AGENT_TEMPLATE,
        *,
        marker: str = "expert.json",
        source_dir: Path | None = None,
        version: str | None = None,
        missing: bool = False,
        forbidden: bool = False,
        conflict_code: str | None = None,
    ) -> None:
        self.kind = kind
        self.marker = marker
        self.source_dir = source_dir
        self.version = version
        self.missing = missing
        self.forbidden = forbidden
        self.conflict_code = conflict_code
        self.imported_from: Path | None = None
        self.imported_files: dict[str, bytes] = {}

    def locate_export(self, local_id: str, version: str | None) -> ExportSource:
        if self.missing:
            raise AssetError(AssetErrorCode.NOT_FOUND, f"资产不存在: {local_id}")
        if self.forbidden:
            raise AssetError(AssetErrorCode.EXPORT_FORBIDDEN, "受限资产不可导出")
        assert self.source_dir is not None
        return ExportSource(
            source_dir=self.source_dir,
            asset_id=local_id,
            version=self.version or version,
        )

    def sniff_package(self, package_dir: Path) -> bool:
        return (Path(package_dir) / self.marker).is_file()

    def import_package(self, package_dir: Path) -> ImportOutcome:
        if self.conflict_code:
            raise AssetError(self.conflict_code, "目标 <id> 冲突")
        package_dir = Path(package_dir)
        self.imported_from = package_dir
        # 契约要求：package_dir 返回后即销毁，适配器须在导入期间复制内容
        self.imported_files = {
            path.relative_to(package_dir).as_posix(): path.read_bytes()
            for path in sorted(package_dir.rglob("*"))
            if path.is_file()
        }
        return ImportOutcome(asset_id=package_dir.name, pending_connectors=("crm",))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr("jiuwenswarm.common.utils.get_workspace_dir", lambda: root)
    return root


@pytest.fixture
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_ADAPTERS", {})
    return registry


@pytest.fixture
def agent_source(tmp_path):
    root = tmp_path / "agent-content"
    root.mkdir()
    (root / "expert.json").write_text('{"name": "demo"}')
    (root / "README.md").write_text("# demo\n")
    return root


def _stage_upload(zip_name: str, entries: dict[str, str], *, root_name: str | None = "my-expert") -> Path:
    """把测试 zip 放进受控上传根，模拟 upload-temp 的产物布局。"""
    upload_dir = assets_upload_root() / "fake-upload"
    upload_dir.mkdir(parents=True, exist_ok=True)
    zip_path = upload_dir / zip_name
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name, content in entries.items():
            arcname = f"{root_name}/{name}" if root_name else name
            archive.writestr(arcname, content)
    return zip_path


def _new_import_workdirs() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("jiuwenswarm_asset_import_*"))


# ------------------------------------------------------------------ registry


def test_register_rejects_non_asset_kind():
    with pytest.raises(ValueError):
        registry.register_asset_adapter(SimpleNamespace(kind="agent_template"))


def test_register_duplicate_requires_replace(isolated_registry):
    first = _FakeAdapter()
    second = _FakeAdapter()
    registry.register_asset_adapter(first)
    with pytest.raises(ValueError):
        registry.register_asset_adapter(second)
    registry.register_asset_adapter(second, replace=True)
    assert registry.get_asset_adapter(AssetKind.AGENT_TEMPLATE) is second
    # 同一对象重复注册幂等
    registry.register_asset_adapter(second)


def test_registered_kinds_keep_order(isolated_registry):
    registry.register_asset_adapter(_FakeAdapter(AssetKind.SKILL, marker="SKILL.md"))
    registry.register_asset_adapter(_FakeAdapter(AssetKind.AGENT_GROUP, marker="group.json"))
    assert registry.registered_asset_kinds() == (AssetKind.SKILL, AssetKind.AGENT_GROUP)
    assert registry.get_asset_adapter(AssetKind.PLUGIN) is None


# -------------------------------------------------------------------- export


def test_export_happy_path(workspace, isolated_registry, agent_source, monkeypatch):
    captured: dict = {}

    def fake_download_info(file_path, file_name, session_id="", expires_in=None, user_id="", purpose=""):
        captured.update(
            file_path=file_path, file_name=file_name,
            session_id=session_id, expires_in=expires_in, purpose=purpose,
        )
        return {"download_url": f"/file-api/download?token=fake&path={file_path}"}

    monkeypatch.setattr(service, "build_file_download_info", fake_download_info)
    isolated_registry.register_asset_adapter(_FakeAdapter(source_dir=agent_source))

    payload = service.export_asset(
        {"kind": "agent_template", "local_id": "my-expert", "version": "1.2.0"},
        session_id="sess-1",
    )

    assert payload["filename"] == "my-expert-1.2.0.zip"
    assert captured["file_name"] == "my-expert-1.2.0.zip"
    assert captured["session_id"] == "sess-1"
    assert captured["expires_in"] == service.EXPORT_DOWNLOAD_EXPIRES_S
    # 短时效用途标记：下载端点据此强制校验过期（交付产物 token 无此字段）
    assert captured["purpose"] == "asset_export"
    zip_path = Path(captured["file_path"])
    # 落盘 basename 即契约文件名（下载端 Content-Disposition 用它），父目录随机防并发覆盖
    assert zip_path.name == "my-expert-1.2.0.zip"
    assert zip_path.parent != assets_export_root()
    assert payload["download_url"].startswith("/file-api/download?token=")
    assert payload["size_bytes"] == zip_path.stat().st_size
    assert payload["sha256"] == hashlib.sha256(zip_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == ["my-expert/README.md", "my-expert/expert.json"]


def test_export_prunes_stale_entries(workspace, isolated_registry, agent_source, monkeypatch):
    monkeypatch.setattr(
        service, "build_file_download_info",
        lambda *a, **kw: {"download_url": "/file-api/download?token=fake"},
    )
    stale = assets_export_root() / "stale-export"
    stale.mkdir()
    (stale / "old.zip").write_bytes(b"x")
    stale_ts = time.time() - STAGING_TTL_SECONDS - 60
    os.utime(stale, (stale_ts, stale_ts))

    isolated_registry.register_asset_adapter(_FakeAdapter(source_dir=agent_source))
    service.export_asset({"kind": "agent_template", "local_id": "my-expert"})

    assert not stale.exists()


def test_export_without_adapter_is_forbidden(workspace, isolated_registry):
    with pytest.raises(AssetError) as excinfo:
        service.export_asset({"kind": "plugin", "local_id": "whatever"})
    assert excinfo.value.code == AssetErrorCode.EXPORT_FORBIDDEN


def test_export_adapter_errors_propagate(workspace, isolated_registry):
    isolated_registry.register_asset_adapter(_FakeAdapter(missing=True))
    with pytest.raises(AssetError) as excinfo:
        service.export_asset({"kind": "agent_template", "local_id": "ghost"})
    assert excinfo.value.code == AssetErrorCode.NOT_FOUND


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "agent_template"},  # 缺 local_id
        {"kind": "expert", "local_id": "x"},  # 未知 kind
        {"kind": "agent_template", "local_id": "x", "items": [{"kind": "skill"}]},
        "not-a-dict",
    ],
)
def test_export_bad_params_raise_value_error(workspace, isolated_registry, params):
    with pytest.raises(ValueError):
        service.export_asset(params)


def test_export_empty_source_cleans_export_dir(workspace, isolated_registry, tmp_path):
    empty = tmp_path / "filtered-out"
    empty.mkdir()
    (empty / ".env").write_text("TOKEN=synthetic")
    isolated_registry.register_asset_adapter(_FakeAdapter(source_dir=empty))
    with pytest.raises(SafeArchiveError) as excinfo:
        service.export_asset({"kind": "agent_template", "local_id": "my-expert"})
    assert excinfo.value.code == "EMPTY_SOURCE"
    assert list(assets_export_root().iterdir()) == []


def test_export_token_failure_cleans_export_dir(workspace, isolated_registry, agent_source, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("token backend down")

    monkeypatch.setattr(service, "build_file_download_info", boom)
    isolated_registry.register_asset_adapter(_FakeAdapter(source_dir=agent_source))
    with pytest.raises(RuntimeError):
        service.export_asset({"kind": "agent_template", "local_id": "my-expert"})
    assert list(assets_export_root().iterdir()) == []


# -------------------------------------------------------------------- import


def test_import_happy_path_sniffed_kind(workspace, isolated_registry):
    zip_path = _stage_upload(
        "my-expert.zip",
        {"expert.json": '{"name": "demo"}', "README.md": "# demo\n"},
    )
    adapter = _FakeAdapter()
    isolated_registry.register_asset_adapter(adapter)
    before = _new_import_workdirs()

    payload = service.import_asset({"path": str(zip_path)})

    assert payload == {
        "kind": "agent_template",
        "id": "my-expert",
        "pending_connectors": ["crm"],
    }
    assert adapter.imported_from is not None
    assert adapter.imported_from.name == "my-expert"
    # 临时工作目录用后即毁（成功路径）
    assert _new_import_workdirs() == before


def test_import_explicit_kind_skips_sniff(workspace, isolated_registry):
    # 无标记文件、根名与 zip 名一致：显式 kind 时不依赖嗅探
    zip_path = _stage_upload("pkg.zip", {"data.txt": "x"}, root_name="pkg")
    adapter = _FakeAdapter(marker="expert.json")
    isolated_registry.register_asset_adapter(adapter)
    payload = service.import_asset({"path": str(zip_path), "kind": "agent_templates"})
    assert payload["kind"] == "agent_template"
    assert payload["id"] == "pkg"


def test_import_rejects_path_outside_upload_root(workspace, isolated_registry, tmp_path):
    outside = tmp_path / "outside.zip"
    with zipfile.ZipFile(outside, "w") as archive:
        archive.writestr("my-expert/expert.json", "{}")
    isolated_registry.register_asset_adapter(_FakeAdapter())
    with pytest.raises(AssetError) as excinfo:
        service.import_asset({"path": str(outside)})
    assert excinfo.value.code == AssetErrorCode.UNSAFE_PATH


def test_import_rejects_symlinked_path(workspace, isolated_registry):
    real = _stage_upload("real.zip", {"expert.json": "{}"})
    link = assets_upload_root() / "link.zip"
    link.symlink_to(real)
    isolated_registry.register_asset_adapter(_FakeAdapter())
    with pytest.raises(AssetError) as excinfo:
        service.import_asset({"path": str(link)})
    assert excinfo.value.code == AssetErrorCode.UNSAFE_PATH


def test_import_rejects_missing_or_non_zip(workspace, isolated_registry):
    upload_dir = assets_upload_root() / "fake-upload"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "notes.txt").write_text("x")
    isolated_registry.register_asset_adapter(_FakeAdapter())
    for bad in (upload_dir / "gone.zip", upload_dir / "notes.txt"):
        with pytest.raises(AssetError) as excinfo:
            service.import_asset({"path": str(bad)})
        assert excinfo.value.code == AssetErrorCode.INVALID_PACKAGE


def test_import_rejects_multi_root_package(workspace, isolated_registry):
    zip_path = assets_upload_root() / "fake-upload" / "multi.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("a/f.txt", "1")
        archive.writestr("b/f.txt", "2")
    isolated_registry.register_asset_adapter(_FakeAdapter())
    with pytest.raises(AssetError) as excinfo:
        service.import_asset({"path": str(zip_path)})
    assert excinfo.value.code == AssetErrorCode.INVALID_PACKAGE


def test_import_zipslip_raises_internal_code_for_entry_convergence(workspace, isolated_registry):
    zip_path = assets_upload_root() / "fake-upload" / "slip.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("../evil.txt", "payload")
    isolated_registry.register_asset_adapter(_FakeAdapter())
    before = _new_import_workdirs()
    with pytest.raises(SafeArchiveError) as excinfo:
        service.import_asset({"path": str(zip_path)})
    assert excinfo.value.code == "UNSAFE_PATH"
    # 失败路径同样回收临时工作目录
    assert _new_import_workdirs() == before


def test_import_unsupported_kind_hint(workspace, isolated_registry):
    zip_path = _stage_upload("p.zip", {"manifest.json": "{}"})
    with pytest.raises(AssetError) as excinfo:
        service.import_asset({"path": str(zip_path), "kind": "plugin"})
    assert excinfo.value.code == AssetErrorCode.INVALID_PACKAGE


def test_import_sniff_no_match(workspace, isolated_registry):
    zip_path = _stage_upload("p.zip", {"unknown.bin": "x"})
    isolated_registry.register_asset_adapter(_FakeAdapter(marker="expert.json"))
    with pytest.raises(AssetError) as excinfo:
        service.import_asset({"path": str(zip_path)})
    assert excinfo.value.code == AssetErrorCode.INVALID_PACKAGE


def test_import_conflict_propagates(workspace, isolated_registry):
    zip_path = _stage_upload("dup.zip", {"expert.json": "{}"})
    isolated_registry.register_asset_adapter(
        _FakeAdapter(conflict_code=AssetErrorCode.ALREADY_EXISTS)
    )
    with pytest.raises(AssetError) as excinfo:
        service.import_asset({"path": str(zip_path)})
    assert excinfo.value.code == AssetErrorCode.ALREADY_EXISTS


# ------------------------------------------------------------------- handler


class _FakeSink:
    def __init__(self) -> None:
        self.responses: list = []

    async def send_response(self, response) -> None:
        self.responses.append(response)


def _make_ctx(
    params: dict,
    *,
    session_id: str | None = None,
    user_id: str = "",
    metadata: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        request=SimpleNamespace(
            request_id="req-1",
            channel_id="chan-1",
            session_id=session_id,
            user_id=user_id,
            params=params,
            metadata=metadata,
        ),
        sink=_FakeSink(),
    )


def _run(coro):
    return asyncio.run(coro)


def test_handler_export_ok(workspace, isolated_registry, agent_source, monkeypatch):
    from jiuwenswarm.server.handlers.assets import handle_assets_export

    captured: dict = {}

    def fake_download_info(*args, **kwargs):
        captured.update(kwargs)
        return {"download_url": "/file-api/download?token=fake"}

    monkeypatch.setattr(service, "build_file_download_info", fake_download_info)
    isolated_registry.register_asset_adapter(_FakeAdapter(source_dir=agent_source))
    ctx = _make_ctx(
        {"kind": "agent_template", "local_id": "my-expert"},
        session_id="sess-9",
        user_id="user-9",
        metadata={"trace-id": "t-9"},
    )
    _run(handle_assets_export(ctx))
    response = ctx.sink.responses[0]
    assert response.ok is True
    assert response.request_id == "req-1"
    assert response.channel_id == "chan-1"
    # 请求 metadata 原样透传进响应（与 session_assets 等兄弟 handler 一致）
    assert response.metadata == {"trace-id": "t-9"}
    assert response.payload["filename"] == "my-expert.zip"
    # 会话与用户身份进入 token 签发（AgentOS 下载代理依赖 user_id 查询参数）
    assert captured["session_id"] == "sess-9"
    assert captured["user_id"] == "user-9"


def test_handler_export_converges_errors(workspace, isolated_registry, monkeypatch):
    from jiuwenswarm.server.handlers.assets import handle_assets_export

    # 契约异常透传
    isolated_registry.register_asset_adapter(_FakeAdapter(missing=True))
    ctx = _make_ctx({"kind": "agent_template", "local_id": "ghost"})
    _run(handle_assets_export(ctx))
    payload = ctx.sink.responses[0].payload
    assert ctx.sink.responses[0].ok is False
    assert payload["code"] == "ASSET_NOT_FOUND"

    # 参数非法 → INVALID_PARAMS
    ctx = _make_ctx({"kind": "agent_template"})
    _run(handle_assets_export(ctx))
    assert ctx.sink.responses[0].payload["code"] == "INVALID_PARAMS"

    # 带内部码异常 → 查表收敛
    monkeypatch.setattr(
        service, "export_asset",
        lambda *a, **kw: (_ for _ in ()).throw(SafeArchiveError("UNSAFE_PATH")),
    )
    ctx = _make_ctx({"kind": "agent_template", "local_id": "x"})
    _run(handle_assets_export(ctx))
    assert ctx.sink.responses[0].payload["code"] == "ASSET_UNSAFE_PATH"

    # 未知异常 → INTERNAL_ERROR 兜底
    monkeypatch.setattr(
        service, "export_asset",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    ctx = _make_ctx({"kind": "agent_template", "local_id": "x"})
    _run(handle_assets_export(ctx))
    assert ctx.sink.responses[0].payload["code"] == "INTERNAL_ERROR"


def test_handler_import_ok_and_error(workspace, isolated_registry):
    from jiuwenswarm.server.handlers.assets import handle_assets_import

    zip_path = _stage_upload("my-expert.zip", {"expert.json": "{}"})
    isolated_registry.register_asset_adapter(_FakeAdapter())
    ctx = _make_ctx({"path": str(zip_path)})
    _run(handle_assets_import(ctx))
    response = ctx.sink.responses[0]
    assert response.ok is True
    assert response.payload == {
        "kind": "agent_template",
        "id": "my-expert",
        "pending_connectors": ["crm"],
    }

    ctx = _make_ctx({"path": "/tmp/not-staged.zip"})
    _run(handle_assets_import(ctx))
    response = ctx.sink.responses[0]
    assert response.ok is False
    assert response.payload["code"] == "ASSET_UNSAFE_PATH"


def test_service_roundtrip_export_then_import(workspace, isolated_registry, agent_source, monkeypatch):
    """导出产物直接回灌导入：包格式、嗅探与 id 全链路一致。"""
    captured: dict = {}

    def fake_download_info(file_path, file_name, **kwargs):
        captured["file_path"] = file_path
        return {"download_url": "/file-api/download?token=fake"}

    monkeypatch.setattr(service, "build_file_download_info", fake_download_info)
    adapter = _FakeAdapter(source_dir=agent_source)
    isolated_registry.register_asset_adapter(adapter)

    exported = service.export_asset(
        {"kind": "agent_template", "local_id": "my-expert", "version": "2.0.0"}
    )
    # 把导出产物挪进上传暂存根，模拟前端下载后再上传
    staged = assets_upload_root() / "roundtrip"
    staged.mkdir(parents=True, exist_ok=True)
    staged_zip = staged / exported["filename"]
    staged_zip.write_bytes(Path(captured["file_path"]).read_bytes())

    payload = service.import_asset({"path": str(staged_zip)})
    assert payload == {
        "kind": "agent_template",
        "id": "my-expert",
        "pending_connectors": ["crm"],
    }
    assert adapter.imported_from is not None
    # 导入返回后临时目录已销毁，适配器只能在导入期间拿到内容
    assert not adapter.imported_from.exists()
    assert adapter.imported_files["expert.json"] == b'{"name": "demo"}'
    assert adapter.imported_files["README.md"] == b"# demo\n"
    assert hashlib.sha256(staged_zip.read_bytes()).hexdigest() == exported["sha256"]


# ---------------------------------------------------------- WS 适配器入口


def _make_ws_request(method, params: dict, *, session_id: str | None = "sess-ws", user_id: str = ""):
    from jiuwenswarm.common.schema.agent import AgentRequest

    return AgentRequest(
        request_id="req-ws",
        channel_id="chan-ws",
        session_id=session_id,
        req_method=method,
        params=params,
        user_id=user_id,
    )


def test_ws_adapter_export_then_import(workspace, isolated_registry, agent_source, monkeypatch):
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    captured: dict = {}

    def fake_download_info(file_path, file_name, **kwargs):
        captured["file_path"] = file_path
        captured.update(kwargs)
        return {"download_url": "/file-api/download?token=fake"}

    monkeypatch.setattr(service, "build_file_download_info", fake_download_info)
    isolated_registry.register_asset_adapter(_FakeAdapter(source_dir=agent_source))
    swarm = interface_module.JiuWenSwarm()

    resp = _run(swarm._handle_assets_package_request(
        _make_ws_request(
            ReqMethod.ASSETS_EXPORT,
            {"kind": "agent_template", "local_id": "my-expert"},
            user_id="user-ws",
        )
    ))
    assert resp.ok is True
    assert resp.request_id == "req-ws"
    assert resp.channel_id == "chan-ws"
    assert resp.payload["filename"] == "my-expert.zip"
    # 会话与用户身份进入 token 签发（AgentOS 下载代理依赖 user_id 查询参数）
    assert captured["session_id"] == "sess-ws"
    assert captured["user_id"] == "user-ws"

    # 导出产物挪进上传暂存根后回灌导入，WS 入口全链路可用
    staged_dir = assets_upload_root() / "ws-roundtrip"
    staged_dir.mkdir(parents=True, exist_ok=True)
    staged_zip = staged_dir / Path(captured["file_path"]).name
    staged_zip.write_bytes(Path(captured["file_path"]).read_bytes())

    resp = _run(swarm._handle_assets_package_request(
        _make_ws_request(ReqMethod.ASSETS_IMPORT, {"path": str(staged_zip)})
    ))
    assert resp.ok is True
    assert resp.payload == {
        "kind": "agent_template",
        "id": "my-expert",
        "pending_connectors": ["crm"],
    }


def test_ws_adapter_error_convergence_and_passthrough(workspace, isolated_registry):
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    isolated_registry.register_asset_adapter(_FakeAdapter(missing=True))
    swarm = interface_module.JiuWenSwarm()

    # 契约异常：与 HTTP 入口同错同码
    resp = _run(swarm._handle_assets_package_request(
        _make_ws_request(
            ReqMethod.ASSETS_EXPORT, {"kind": "agent_template", "local_id": "ghost"}
        )
    ))
    assert resp.ok is False
    assert resp.payload["code"] == "ASSET_NOT_FOUND"

    # 参数非法 → INVALID_PARAMS
    resp = _run(swarm._handle_assets_package_request(
        _make_ws_request(ReqMethod.ASSETS_IMPORT, {})
    ))
    assert resp.ok is False
    assert resp.payload["code"] == "INVALID_PARAMS"

    # 非 assets.* 方法不归本入口处理
    assert _run(swarm._handle_assets_package_request(
        _make_ws_request(ReqMethod.SESSION_ASSETS_LIST, {})
    )) is None
