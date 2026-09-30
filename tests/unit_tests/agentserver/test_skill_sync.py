# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""POST /skill-sync/* 技能同步（Server/Client 分离部署）契约测试.

覆盖设计文档第 9 节：
- 通用安全：未启用 503、错误 token 401、正确 token 放行、apply 超 50MB 预检 413
- diff 五种状态判定与 version 口径（frontmatter 不回退、None → ""）
- checksum 排除 __pycache__（打包→解压→二轮 diff 应 in_sync）
- package fail-fast、指定版本取数、sha256 头
- include_archive=true 的包直接 apply 被拒（SKILL_RESERVED_PATH）
- apply checksum 校验 / 覆盖 / best_effort / builtin 保护 / skillpack 拒绝
- apply strict 预检闭环 / best_effort 分组
- 类别漂移分组、compare_version
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill.archive_store import (
    ARCHIVE_DIRNAME,
    CHECKSUM_ALGO_VERSION,
    compute_content_checksum,
)
from jiuwenswarm.server.runtime.skill.skill_manager import (
    CATEGORY_LOCAL,
    CATEGORY_MARKETPLACE,
    CATEGORY_UNKNOWN,
    ERROR_SKILL_ALREADY_EXISTS,
    ERROR_SKILL_BUILTIN_READ_ONLY,
    ERROR_SKILL_OPERATION_UNSUPPORTED,
    ERROR_SKILL_RESERVED_PATH,
    ERROR_SKILL_SYNC_CHECKSUM_ALGO_MISMATCH,
    ERROR_SKILL_SYNC_CHECKSUM_MISMATCH,
    ERROR_SKILL_SYNC_EMPTY_PACKAGE,
    ERROR_SKILL_SYNC_FILE_TOO_LARGE,
    ERROR_SKILL_SYNC_INVALID_PAYLOAD,
    ERROR_SKILL_SYNC_PACKAGE_TOO_LARGE,
    ERROR_SKILL_SYNC_UNAUTHORIZED,
    ERROR_SKILL_SYNC_DISABLED,
    ERROR_SKILL_NOT_FOUND,
    SkillManager,
    SkillRpcError,
    compare_version,
    normalize_sync_category,
)
from jiuwenswarm.server.runtime.skill import skill_sync_http
from jiuwenswarm.server.runtime.skill.skill_sync_http import (
    handle_sync_apply_http,
    handle_sync_diff_http,
    handle_sync_package_http,
)


def _skill_md(name: str, description: str = "demo skill", version: str = "") -> str:
    version_line = f"version: {version}\n" if version else ""
    return (
        f"---\nname: {name}\ndescription: {description}\n{version_line}---\n# Body\n"
    )


def _write_skill(
    skill_dir: Path,
    *,
    name: str,
    description: str = "demo skill",
    body: str = "# Hi",
    version: str = "",
) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        _skill_md(name, description, version) + f"{body}\n", encoding="utf-8"
    )
    return skill_dir


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in entries.items():
            zf.writestr(arcname, content)
    return buf.getvalue()


def _write_archive_index(
    skill_dir: Path, *, current_version: str, storage_id: str
) -> None:
    """写入 .archive/versions/index.json 并落对应版本副本目录."""
    index = {
        "schema_version": 2,
        "current_version": current_version,
        "installed_asset_id": f"asset-{storage_id}",
        "versions": [
            {
                "version": current_version,
                "storage_id": storage_id,
                "source": "skillhub",
                "created_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-01T00:00:00Z",
            }
        ],
        "remote_asset_id": None,
        "last_published_version": None,
        "updated_at": "2026-09-01T00:00:00Z",
    }
    index_dir = skill_dir / ARCHIVE_DIRNAME / "versions"
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
    _write_skill(index_dir / "content" / storage_id, name=skill_dir.name)


def _multipart(
    fields: dict[str, object], *, boundary: str = "----SkillSyncBoundary"
) -> tuple[str, bytes]:
    parts: list[bytes] = []
    for name, value in fields.items():
        if isinstance(value, tuple):
            filename, content = value
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: application/zip\r\n\r\n"
                ).encode("utf-8")
                + content
                + b"\r\n"
            )
        else:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    content_type = f"multipart/form-data; boundary={boundary}"
    return content_type, b"".join(parts)


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SkillManager:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    state_file = skills_dir / "skills_state.json"
    state_file.write_text(
        json.dumps(
            {
                "marketplaces": [],
                "installed_plugins": [],
                "local_skills": [],
                "skill_configs": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: tmp_path / "builtin_missing",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager._get_agent_root_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager._get_marketplace_dir",
        lambda: skills_dir / "_marketplace",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager._get_state_file",
        lambda: state_file,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_state_file",
        lambda: state_file,
    )
    return SkillManager()


@pytest.fixture
def sync_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "unit-test-sync-token"
    monkeypatch.setenv("JIUWENSKILL_SYNC_TOKEN", token)
    return token


def _auth(token: str) -> str:
    return f"Bearer {token}"


# ---------------------------------------------------------------------------
# compare_version / normalize_sync_category
# ---------------------------------------------------------------------------


def test_compare_version_numeric_and_padding() -> None:
    assert compare_version("1.0.0", "1.0.0") == 0
    assert compare_version("1.10.0", "1.9.0") == 1  # 数字段按整数值
    assert compare_version("01.0", "1.0") == 0  # 前导零忽略
    assert compare_version("1.0", "1.0.0") == 0  # 缺失段按 0 补齐
    assert compare_version("2.0", "10.0") == -1


def test_compare_version_non_numeric_and_none() -> None:
    assert compare_version("1.a", "1.b") == -1  # 非数字段字典序
    assert compare_version("1.0.0-rc1", "1.0.0") is None  # 预发布后缀不可判定
    assert compare_version("", "1.0") is None
    assert compare_version("1..0", "1.0.0") is None  # 空段不可解析，人工决策


def test_normalize_sync_category() -> None:
    assert normalize_sync_category("baoyu") == CATEGORY_MARKETPLACE
    assert normalize_sync_category("skillnet") == "online"
    assert normalize_sync_category("clawhub") == "online"
    assert normalize_sync_category("teamskillshub") == "online"
    assert normalize_sync_category("local") == CATEGORY_LOCAL
    assert normalize_sync_category("project") == "project"
    assert normalize_sync_category("builtin") == "builtin"
    assert normalize_sync_category("") == CATEGORY_UNKNOWN


# ---------------------------------------------------------------------------
# compute_content_checksum 排除口径（决策 6）
# ---------------------------------------------------------------------------


def test_checksum_excludes_pycache_and_pyc(tmp_path: Path) -> None:
    root = tmp_path / "skill-a"
    _write_skill(root, name="skill-a")
    (root / "scripts").mkdir()
    (root / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
    before = compute_content_checksum(root)

    # 加入派生产物后 checksum 不变
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "run.cpython-311.pyc").write_bytes(b"\x00compiled")
    (root / "scripts" / "__pycache__").mkdir()
    (root / "scripts" / "__pycache__" / "x.pyc").write_bytes(b"\x00compiled")
    (root / "legacy.pyc").write_bytes(b"\x00compiled")
    assert compute_content_checksum(root) == before

    # .archive 依旧排除
    (root / ARCHIVE_DIRNAME / "versions").mkdir(parents=True)
    (root / ARCHIVE_DIRNAME / "versions" / "index.json").write_text("{}", encoding="utf-8")
    assert compute_content_checksum(root) == before


# ---------------------------------------------------------------------------
# 通用安全（鉴权 / 413）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diff_disabled_without_token_config(
    manager: SkillManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JIUWENSKILL_SYNC_TOKEN", raising=False)
    monkeypatch.setattr(
        skill_sync_http, "skill_sync_enabled", lambda: False
    )
    status, payload = handle_sync_diff_http(
        authorization="",
        body=json.dumps({"client_skills": []}).encode("utf-8"),
    )
    assert status == 503
    assert payload["code"] == ERROR_SKILL_SYNC_DISABLED


@pytest.mark.asyncio
async def test_diff_unauthorized_wrong_token(
    manager: SkillManager, sync_token: str
) -> None:
    status, payload = handle_sync_diff_http(
        authorization="Bearer wrong-token",
        body=json.dumps({"client_skills": []}).encode("utf-8"),
    )
    assert status == 401
    assert payload["code"] == ERROR_SKILL_SYNC_UNAUTHORIZED


@pytest.mark.asyncio
async def test_diff_non_ascii_bearer_token_returns_401_not_crash(
    manager: SkillManager, sync_token: str
) -> None:
    """非 ASCII Bearer token：401 拒绝而非 TypeError 打穿异常处理."""
    status, payload = handle_sync_diff_http(
        authorization="Bearer 无效令牌",
        body=json.dumps({"client_skills": []}).encode("utf-8"),
    )
    assert status == 401
    assert payload["code"] == ERROR_SKILL_SYNC_UNAUTHORIZED


@pytest.mark.asyncio
async def test_diff_non_ascii_configured_token_still_works(
    manager: SkillManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置侧 token 含非 ASCII 时接口仍可用（两侧 encode 后比较）."""
    monkeypatch.setenv("JIUWENSKILL_SYNC_TOKEN", "中文令牌-123")
    status, payload = handle_sync_diff_http(
        authorization="Bearer 中文令牌-123",
        body=json.dumps({"client_skills": []}).encode("utf-8"),
    )
    assert status == 200
    assert payload["success"] is True
    # 错误的非 ASCII token 也走正常 401
    status2, payload2 = handle_sync_diff_http(
        authorization="Bearer 错误令牌",
        body=json.dumps({"client_skills": []}).encode("utf-8"),
    )
    assert status2 == 401


def test_upload_limit_leaves_headroom_over_zip_limit() -> None:
    """上传上限须高于打包上限：multipart body = zip + boundary + 字段开销.

    两值相等时接近上限的合法包永远回传不了（2.3 回归）。
    """
    from jiuwenswarm.server.runtime.skill import skill_sync_http
    from jiuwenswarm.server.runtime.skill.skill_manager import (
        _SKILL_SYNC_MAX_ZIP_BYTES,
    )

    assert (
        skill_sync_http._SKILL_SYNC_MAX_UPLOAD_BYTES
        > _SKILL_SYNC_MAX_ZIP_BYTES + 1024 * 1024
    ), "上传上限须比打包上限至少多 1MB 余量"


@pytest.mark.asyncio
async def test_diff_authorized_passes(
    manager: SkillManager, sync_token: str
) -> None:
    status, payload = handle_sync_diff_http(
        authorization=_auth(sync_token),
        body=json.dumps({"client_skills": []}).encode("utf-8"),
    )
    assert status == 200
    assert payload["success"] is True


@pytest.mark.asyncio
async def test_apply_content_length_precheck_413(
    manager: SkillManager, sync_token: str
) -> None:
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token),
        content_type="multipart/form-data",
        body=b"",
        content_length=skill_sync_http._SKILL_SYNC_MAX_UPLOAD_BYTES + 1,
    )
    assert status == 413
    assert payload["code"] == ERROR_SKILL_SYNC_FILE_TOO_LARGE


def test_precheck_rejects_before_body_read() -> None:
    """纯 header 预检：鉴权失败 / JSON body 超限在读 body 前就能拒绝."""
    from jiuwenswarm.server.runtime.skill.skill_sync_http import (
        precheck_skill_sync_request,
    )

    # 未启用：503（不读 body）
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("JIUWENSKILL_SYNC_TOKEN", raising=False)
        mp.setattr(skill_sync_http, "skill_sync_enabled", lambda: False)
        with pytest.raises(SkillRpcError) as excinfo:
            precheck_skill_sync_request(
                authorization="", path="/skill-sync/diff", content_length=10
            )
        assert excinfo.value.code == ERROR_SKILL_SYNC_DISABLED

    # token 无效：401（不读 body）
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("JIUWENSKILL_SYNC_TOKEN", "unit-test-sync-token")
        with pytest.raises(SkillRpcError) as excinfo:
            precheck_skill_sync_request(
                authorization="Bearer wrong",
                path="/skill-sync/diff",
                content_length=10,
            )
        assert excinfo.value.code == ERROR_SKILL_SYNC_UNAUTHORIZED

    # diff / package 的 JSON body 超 Content-Length 上限：413 拒绝
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("JIUWENSKILL_SYNC_TOKEN", "unit-test-sync-token")
        for path in ("/skill-sync/diff", "/skill-sync/package"):
            with pytest.raises(SkillRpcError) as excinfo:
                precheck_skill_sync_request(
                    authorization="Bearer unit-test-sync-token",
                    path=path,
                    content_length=skill_sync_http.SKILL_SYNC_MAX_JSON_BYTES + 1,
                )
            assert excinfo.value.code == ERROR_SKILL_SYNC_FILE_TOO_LARGE

    # 合法 header：放行（apply 上限更大）
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("JIUWENSKILL_SYNC_TOKEN", "unit-test-sync-token")
        precheck_skill_sync_request(
            authorization="Bearer unit-test-sync-token",
            path="/skill-sync/apply",
            content_length=skill_sync_http.SKILL_SYNC_MAX_UPLOAD_BYTES,
        )


@pytest.mark.asyncio
async def test_package_http_missing_zip_path_returns_500(
    manager: SkillManager, sync_token: str
) -> None:
    """manager 结果缺 zip_path 时返回 500，绝不进入 rmtree 清理路径."""
    from jiuwenswarm.server.runtime.skill import skill_sync_http as mod

    def _stub_coro(coro: Any) -> Any:
        return {"success": True, "sha256": "x" * 64, "size": 1}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod, "_run_manager_coro", _stub_coro)
        _write_skill(manager._skills_dir / "demo", name="demo")
        status, payload = handle_sync_package_http(
            authorization=_auth(sync_token),
            body=json.dumps({"skills": [{"name": "demo"}]}).encode("utf-8"),
            send_file=lambda *args, **kwargs: None,
        )
    assert status == 500
    assert payload["code"] == "SKILL_SYNC_PACKAGE_FAILED"


@pytest.mark.asyncio
async def test_apply_http_notifies_agentserver_reload(
    manager: SkillManager, sync_token: str
) -> None:
    """HTTP apply 落盘后经 AgentServer WS 发 skills.sync.reload；通知失败不影响结果."""
    zip_bytes = _zip_bytes(
        {"remote-skill/SKILL.md": _skill_md("remote-skill").encode("utf-8")}
    )
    content_type, body = _multipart(
        {
            "file": ("skills.zip", zip_bytes),
            "sha256": hashlib.sha256(zip_bytes).hexdigest(),
        }
    )
    notified: list[dict] = []

    def _fake_rpc(*, method: object, params: dict, timeout_s: float = 0.0) -> dict:
        notified.append({"method": str(getattr(method, "value", method)), "params": params})
        return {"success": True, "applied": params.get("applied", [])}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "jiuwenswarm.server.runtime.skill.skills_multipart_http"
            "._call_agent_skill_rpc",
            _fake_rpc,
        )
        status, payload = handle_sync_apply_http(
            authorization=_auth(sync_token),
            content_type=content_type,
            body=body,
        )
        assert status == 200
        assert payload["success"] is True
        assert notified == [
            {"method": "skills.sync.reload", "params": {"applied": ["remote-skill"]}}
        ]

        # AgentServer 不可达（通知抛错）不影响 apply 结果
        def _boom(*, method: object, params: dict, timeout_s: float = 0.0) -> dict:
            raise OSError("agentserver unreachable")

        mp.setattr(
            "jiuwenswarm.server.runtime.skill.skills_multipart_http"
            "._call_agent_skill_rpc",
            _boom,
        )
        zip_bytes2 = _zip_bytes(
            {
                "remote-skill-2/SKILL.md": _skill_md("remote-skill-2").encode("utf-8"),
            }
        )
        content_type2, body2 = _multipart(
            {
                "file": ("skills.zip", zip_bytes2),
                "sha256": hashlib.sha256(zip_bytes2).hexdigest(),
            }
        )
        status2, payload2 = handle_sync_apply_http(
            authorization=_auth(sync_token),
            content_type=content_type2,
            body=body2,
        )
        assert status2 == 200
        assert payload2["success"] is True
        assert [a["name"] for a in payload2["applied"]] == ["remote-skill-2"]


@pytest.mark.asyncio
async def test_apply_http_no_landed_skills_skips_notification(
    manager: SkillManager, sync_token: str
) -> None:
    """无落盘（全部 skipped）时不发 reload 通知."""
    zip_bytes = _zip_bytes(
        {"remote-skill/SKILL.md": _skill_md("remote-skill").encode("utf-8")}
    )
    content_type, body = _multipart(
        {
            "file": ("skills.zip", zip_bytes),
            "sha256": hashlib.sha256(zip_bytes).hexdigest(),
        }
    )
    notified: list[dict] = []

    def _fake_rpc(*, method: object, params: dict, timeout_s: float = 0.0) -> dict:
        notified.append({"params": params})
        return {"success": True}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "jiuwenswarm.server.runtime.skill.skills_multipart_http"
            "._call_agent_skill_rpc",
            _fake_rpc,
        )
        # 第一次安装成功（会发通知），之后 strict 重复提交整单 409（无落盘，无通知）
        status, _payload = handle_sync_apply_http(
            authorization=_auth(sync_token), content_type=content_type, body=body
        )
        assert status == 200
        assert len(notified) == 1
        status2, payload2 = handle_sync_apply_http(
            authorization=_auth(sync_token), content_type=content_type, body=body
        )
        assert status2 == 409
        assert len(notified) == 1  # 未新增通知


# ---------------------------------------------------------------------------
# diff：状态判定 / version 口径 / 类别漂移
# ---------------------------------------------------------------------------


def _client_digest(
    name: str,
    *,
    checksum: str,
    version: str = "",
    category: str = "local",
    source: str = "local",
) -> dict:
    return {
        "name": name,
        "category": category,
        "source": source,
        "skill_type": "skill",
        "version": version,
        "content_checksum": checksum,
        "description": "",
        "updated_at": "",
        "builtin": False,
    }


def _items_by_status(result: dict, category: str | None = None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for group in result["categories"]:
        if category is not None and group["category"] != category:
            continue
        for status, items in group["items"].items():
            out.setdefault(status, []).extend(items)
    return out


@pytest.mark.asyncio
async def test_diff_five_statuses_and_version_semantics(
    manager: SkillManager,
) -> None:
    # server_only：server 有 client 无
    _write_skill(manager._skills_dir / "server-only", name="server-only")
    manager._add_local_skill({"name": "server-only", "source": "local", "origin": "local"})

    # in_sync：同名同 checksum（server 端真实 __pycache__ 不参与 checksum）
    in_sync_dir = _write_skill(manager._skills_dir / "in-sync", name="in-sync")
    (in_sync_dir / "__pycache__").mkdir()
    (in_sync_dir / "__pycache__" / "m.pyc").write_bytes(b"\x00")
    manager._add_local_skill({"name": "in-sync", "source": "local", "origin": "local"})
    in_sync_checksum = compute_content_checksum(in_sync_dir)

    # version_mismatch：checksum 不同、双方 version 非空且可判定方向
    vm_dir = _write_skill(
        manager._skills_dir / "ver-mismatch", name="ver-mismatch", body="server v2"
    )
    manager._add_local_skill({"name": "ver-mismatch", "source": "local", "origin": "local"})
    _write_archive_index(vm_dir, current_version="1.1.0", storage_id="sid-vm")

    # content_mismatch：checksum 不同、双方 version 均为空（frontmatter 不回退）
    _write_skill(
        manager._skills_dir / "content-mismatch",
        name="content-mismatch",
        body="server changed",
        version="9.9.9",  # frontmatter version 必须被忽略（决策 5）
    )
    manager._add_local_skill({"name": "content-mismatch", "source": "local", "origin": "local"})

    client_skills = [
        # client_only：client 有 server 无
        _client_digest("client-only", checksum="cafebabe"),
        _client_digest("in-sync", checksum=in_sync_checksum),
        _client_digest("ver-mismatch", checksum="different", version="1.0.0"),
        _client_digest("content-mismatch", checksum="another-diff"),
    ]
    result = await manager.handle_skill_sync_diff(
        {"client_id": "edge-01", "client_skills": client_skills}
    )

    assert result["success"] is True
    assert result["client_id"] == "edge-01"
    summary = result["summary"]
    assert summary["server_only"] == 1
    assert summary["client_only"] == 1
    assert summary["in_sync"] == 1
    assert summary["version_mismatch"] == 1
    assert summary["content_mismatch"] == 1
    assert summary["total"] == 5

    items = _items_by_status(result)
    assert items["server_only"][0]["name"] == "server-only"
    assert items["client_only"][0]["name"] == "client-only"
    assert items["in_sync"][0]["name"] == "in-sync"
    assert items["version_mismatch"][0]["name"] == "ver-mismatch"
    assert items["content_mismatch"][0]["name"] == "content-mismatch"

    # version 口径：无 .archive 索引 → ""，frontmatter 9.9.9 不回退
    cm_entry = items["content_mismatch"][0]
    assert cm_entry["server"]["version"] == ""
    assert cm_entry["client"]["version"] == ""

    # server 侧 digest builtin 标记存在
    assert items["server_only"][0]["server"]["builtin"] is False


@pytest.mark.asyncio
async def test_diff_category_grouping_and_drift(manager: SkillManager) -> None:
    # server 侧：marketplace 来源技能
    _write_skill(manager._skills_dir / "from-market", name="from-market")
    manager._add_installed_plugin(
        {"name": "from-market", "source": "baoyu", "marketplace": "baoyu"}
    )
    # client 侧上报为 local（类别漂移：拉取后 origin=sync_client 落为 local）
    result = await manager.handle_skill_sync_diff(
        {
            "client_skills": [
                _client_digest(
                    "from-market",
                    checksum="whatever",
                    category="local",
                    source="local",
                )
            ]
        }
    )
    # 归入 server 侧 category = marketplace；漂移对用户可见
    group = next(g for g in result["categories"] if g["category"] == "marketplace")
    assert group["summary"]["content_mismatch"] == 1
    entry = group["items"]["content_mismatch"][0]
    assert entry["server"]["category"] == "marketplace"
    assert entry["client"]["category"] == "local"


@pytest.mark.asyncio
async def test_diff_payload_validation_and_mcp_exclusion(
    manager: SkillManager,
) -> None:
    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skill_sync_diff({"client_skills": "not-a-list"})
    assert exc_info.value.code == ERROR_SKILL_SYNC_INVALID_PAYLOAD

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skill_sync_diff({"client_skills": [{"name": "a"}]})
    assert exc_info.value.code == ERROR_SKILL_SYNC_INVALID_PAYLOAD

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skill_sync_diff(
            {
                "client_skills": [
                    _client_digest("dup", checksum="x"),
                    _client_digest("dup", checksum="x"),
                ]
            }
        )
    assert exc_info.value.code == ERROR_SKILL_SYNC_INVALID_PAYLOAD

    # source=mcp 的条目直接忽略，不报错
    result = await manager.handle_skill_sync_diff(
        {"client_skills": [_client_digest("mcp-skill", checksum="x", source="mcp")]}
    )
    assert result["summary"]["client_only"] == 0

    # MCP 条目缺 name/checksum 不再导致整单 400（跳过先于必填校验）
    result = await manager.handle_skill_sync_diff(
        {"client_skills": [{"source": "mcp"}, {"name": "ok", "content_checksum": "c1"}]}
    )
    assert result["summary"]["client_only"] == 1

    # 用户技能与 MCP 技能同名不触发重复校验（MCP 目录与用户目录可同名）
    result = await manager.handle_skill_sync_diff(
        {
            "client_skills": [
                _client_digest("same-name", checksum="c1"),
                _client_digest("same-name", checksum="c2", source="mcp"),
            ]
        }
    )
    assert result["summary"]["client_only"] == 1


@pytest.mark.asyncio
async def test_diff_options_filters(manager: SkillManager) -> None:
    _write_skill(manager._skills_dir / "hidden", name="hidden")
    manager._add_local_skill({"name": "hidden", "source": "local", "origin": "local"})
    result = await manager.handle_skill_sync_diff(
        {
            "client_skills": [],
            "options": {"include_builtin": False, "categories": ["online"]},
        }
    )
    # local 类技能被 categories 过滤，builtin 过滤对非 builtin 无影响
    assert result["summary"]["server_only"] == 0


@pytest.mark.asyncio
async def test_diff_checksum_algo_version_contract(manager: SkillManager) -> None:
    """checksum_algo_version 协议字段：匹配放行 / 缺省兼容 / 不符 400 / 非法 400."""
    payload = {"client_skills": [_client_digest("any", checksum="x")]}

    # 显式声明当前版本：放行
    result = await manager.handle_skill_sync_diff(
        {**payload, "checksum_algo_version": CHECKSUM_ALGO_VERSION}
    )
    assert result["success"] is True

    # 缺省（旧 client 不带字段）：视为当前版本，放行
    result = await manager.handle_skill_sync_diff(dict(payload))
    assert result["success"] is True

    # 版本不符：拒绝比对（防止算法失配导致全量 content_mismatch 误报）
    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skill_sync_diff(
            {**payload, "checksum_algo_version": CHECKSUM_ALGO_VERSION + 1}
        )
    assert exc_info.value.code == ERROR_SKILL_SYNC_CHECKSUM_ALGO_MISMATCH
    assert f"client=v{CHECKSUM_ALGO_VERSION + 1}" in exc_info.value.message
    assert f"server=v{CHECKSUM_ALGO_VERSION}" in exc_info.value.message

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skill_sync_diff(
            {**payload, "checksum_algo_version": 1}
        )
    assert exc_info.value.code == ERROR_SKILL_SYNC_CHECKSUM_ALGO_MISMATCH

    # 非整数值：payload 非法
    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skill_sync_diff(
            {**payload, "checksum_algo_version": "v2"}
        )
    assert exc_info.value.code == ERROR_SKILL_SYNC_INVALID_PAYLOAD


# ---------------------------------------------------------------------------
# package：fail-fast / 指定版本 / sha256 / 打包口径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_package_fail_fast_on_unknown_skill(manager: SkillManager) -> None:
    _write_skill(manager._skills_dir / "exists", name="exists")
    manager._add_local_skill({"name": "exists", "source": "local", "origin": "local"})
    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skill_sync_package(
            {"skills": [{"name": "exists"}, {"name": "missing"}]}
        )
    assert exc_info.value.code == ERROR_SKILL_NOT_FOUND
    assert "missing" in exc_info.value.message


@pytest.mark.asyncio
async def test_package_fail_fast_on_unknown_version(manager: SkillManager) -> None:
    _write_skill(manager._skills_dir / "no-archive", name="no-archive")
    manager._add_local_skill({"name": "no-archive", "source": "local", "origin": "local"})
    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skill_sync_package(
            {"skills": [{"name": "no-archive", "version": "3.2.1"}]}
        )
    assert exc_info.value.code == "SKILL_VERSION_NOT_FOUND"
    assert "no-archive@3.2.1" in exc_info.value.message


@pytest.mark.asyncio
async def test_package_empty_and_duplicate(manager: SkillManager) -> None:
    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skill_sync_package({"skills": []})
    assert exc_info.value.code == ERROR_SKILL_SYNC_EMPTY_PACKAGE

    _write_skill(manager._skills_dir / "dup", name="dup")
    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skill_sync_package(
            {"skills": [{"name": "dup"}, {"name": "dup"}]}
        )
    assert exc_info.value.code == ERROR_SKILL_SYNC_INVALID_PAYLOAD


@pytest.mark.asyncio
async def test_package_zip_structure_and_sha256(manager: SkillManager) -> None:
    weather_dir = _write_skill(
        manager._skills_dir / "weather", name="weather", description="天气查询"
    )
    (weather_dir / "__pycache__").mkdir()
    (weather_dir / "__pycache__" / "leak.pyc").write_bytes(b"\x00should-not-ship")
    manager._add_local_skill({"name": "weather", "source": "skillnet", "origin": "skillnet"})

    result = await manager.handle_skill_sync_package(
        {"skills": [{"name": "weather"}]}
    )
    assert result["success"] is True
    zip_path = Path(result["zip_path"])
    assert zip_path.is_file()
    body = zip_path.read_bytes()
    assert hashlib.sha256(body).hexdigest() == result["sha256"]
    assert result["size"] == len(body)

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = zf.namelist()
        assert "weather/SKILL.md" in names
        assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
        assert not any(n.startswith(f"weather/{ARCHIVE_DIRNAME}") for n in names)
    # 临时目录由 HTTP 层清理；此处手动清理避免测试残留
    import shutil

    shutil.rmtree(zip_path.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_package_excludes_uppercase_pyc(manager: SkillManager) -> None:
    """大写 .PYC 同样被排除：打包与 checksum 的排除口径须大小写一致（决策 6）."""
    skill_dir = _write_skill(manager._skills_dir / "cased", name="cased")
    (skill_dir / "UPPER.PYC").write_bytes(b"\x00derived-upper")
    (skill_dir / "Mixed.PyC").write_bytes(b"\x00derived-mixed")
    manager._add_local_skill({"name": "cased", "source": "local", "origin": "local"})

    import shutil

    try:
        result = await manager.handle_skill_sync_package(
            {"skills": [{"name": "cased"}]}
        )
        assert result["success"] is True
        with zipfile.ZipFile(result["zip_path"]) as zf:
            names = zf.namelist()
            assert "cased/SKILL.md" in names
            assert not any(n.upper().endswith(".PYC") for n in names), names
    finally:
        shutil.rmtree(Path(result["zip_path"]).parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_apply_zip_slip_returns_400_not_500(
    manager: SkillManager, sync_token: str
) -> None:
    """Zip-Slip 恶意包按 400 SKILL_INVALID_PACKAGE 拒绝，而非 500 触发客户端重试."""
    evil = _zip_bytes({"evil/SKILL.md": _skill_md("evil").encode("utf-8"),
                       "../escape.txt": b"zip-slip"})
    content_type, body = _multipart(
        {
            "file": ("skills.zip", evil),
            "sha256": hashlib.sha256(evil).hexdigest(),
        }
    )
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token),
        content_type=content_type,
        body=body,
    )
    assert status == 400
    assert payload["code"] == "SKILL_INVALID_PACKAGE"


@pytest.mark.asyncio
async def test_package_roundtrip_second_diff_in_sync(manager: SkillManager) -> None:
    """server 打包 → 解压模拟 client → 二轮 diff 应 in_sync（决策 6 闭环）."""
    server_dir = _write_skill(
        manager._skills_dir / "roundtrip", name="roundtrip", body="shared content"
    )
    (server_dir / "scripts").mkdir()
    (server_dir / "scripts" / "tool.py").write_text("print('tool')\n", encoding="utf-8")
    (server_dir / "__pycache__").mkdir()
    (server_dir / "__pycache__" / "tool.pyc").write_bytes(b"\x00derived")
    manager._add_local_skill({"name": "roundtrip", "source": "local", "origin": "local"})

    result = await manager.handle_skill_sync_package({"skills": [{"name": "roundtrip"}]})
    body = Path(result["zip_path"]).read_bytes()

    # client 侧解压落盘（无 __pycache__），计算同算法 checksum
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        client_dir = Path(tmp) / "roundtrip"
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            zf.extractall(tmp)
        client_checksum = compute_content_checksum(client_dir)

    diff = await manager.handle_skill_sync_diff(
        {"client_skills": [_client_digest("roundtrip", checksum=client_checksum)]}
    )
    assert diff["summary"]["in_sync"] == 1

    import shutil

    shutil.rmtree(Path(result["zip_path"]).parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_package_include_archive_true_carries_archive(
    manager: SkillManager,
) -> None:
    skill_dir = _write_skill(manager._skills_dir / "archived", name="archived")
    manager._add_local_skill({"name": "archived", "source": "local", "origin": "local"})
    _write_archive_index(skill_dir, current_version="1.0.0", storage_id="sid-1")

    result = await manager.handle_skill_sync_package(
        {"skills": [{"name": "archived"}], "include_archive": True}
    )
    body = Path(result["zip_path"]).read_bytes()
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = zf.namelist()
        assert f"archived/{ARCHIVE_DIRNAME}/versions/index.json" in names
        assert f"archived/{ARCHIVE_DIRNAME}/versions/content/sid-1/SKILL.md" in names

    # include_archive=false（默认）：.archive 不进包
    result_plain = await manager.handle_skill_sync_package(
        {"skills": [{"name": "archived"}]}
    )
    body_plain = Path(result_plain["zip_path"]).read_bytes()
    with zipfile.ZipFile(io.BytesIO(body_plain)) as zf:
        assert not any(ARCHIVE_DIRNAME in n for n in zf.namelist())

    # include_archive=true 的包不可直接 apply 的断言见
    # test_package_include_archive_zip_rejected_by_apply（独立用例，需 auth fixture）
    import shutil

    shutil.rmtree(Path(result["zip_path"]).parent, ignore_errors=True)
    shutil.rmtree(Path(result_plain["zip_path"]).parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_package_include_archive_zip_rejected_by_apply(
    manager: SkillManager, sync_token: str
) -> None:
    """include_archive=true 的包直接 apply 被拒（SKILL_RESERVED_PATH）."""
    _write_skill(manager._skills_dir / "arch-rt", name="arch-rt")
    manager._add_local_skill({"name": "arch-rt", "source": "local", "origin": "local"})
    result = await manager.handle_skill_sync_package(
        {"skills": [{"name": "arch-rt"}], "include_archive": True}
    )
    # 根级 .archive 的包（include_archive 备份包未剥离 .archive 直接回推）
    root_level = _zip_bytes(
        {
            "arch-rt/SKILL.md": _skill_md("arch-rt").encode("utf-8"),
            f"{ARCHIVE_DIRNAME}/versions/index.json": b"{}",
        }
    )
    content_type, form = _multipart(
        {
            "file": ("sync.zip", root_level),
            "sha256": hashlib.sha256(root_level).hexdigest(),
        }
    )
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token),
        content_type=content_type,
        body=form,
    )
    assert status == 400
    assert payload["code"] == ERROR_SKILL_RESERVED_PATH

    import shutil

    shutil.rmtree(Path(result["zip_path"]).parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_package_batch_quota_too_many_skills(
    manager: SkillManager,
) -> None:
    skills = [{"name": f"s-{i}"} for i in range(101)]
    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skill_sync_package({"skills": skills})
    assert exc_info.value.code == ERROR_SKILL_SYNC_PACKAGE_TOO_LARGE


# ---------------------------------------------------------------------------
# apply：checksum / 覆盖 / strict / best_effort / builtin / skillpack
# ---------------------------------------------------------------------------


def _apply_form(
    zip_content: bytes,
    *,
    sha256: str | None = None,
    overwrite: str = "false",
    mode: str = "strict",
    origin: str | None = None,
) -> tuple[str, bytes]:
    fields: dict[str, object] = {
        "file": ("sync.zip", zip_content),
        "sha256": sha256 or hashlib.sha256(zip_content).hexdigest(),
        "overwrite": overwrite,
        "mode": mode,
    }
    if origin is not None:
        fields["origin"] = origin
    return _multipart(fields)


@pytest.mark.asyncio
async def test_apply_checksum_mismatch(manager: SkillManager, sync_token: str) -> None:
    package = _zip_bytes({"weather/SKILL.md": _skill_md("weather").encode("utf-8")})
    content_type, form = _apply_form(package, sha256="0" * 64)
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token),
        content_type=content_type,
        body=form,
    )
    assert status == 400
    assert payload["code"] == ERROR_SKILL_SYNC_CHECKSUM_MISMATCH
    # 不落盘
    assert not (manager._skills_dir / "weather").exists()


@pytest.mark.asyncio
async def test_apply_invalid_sha256_field(manager: SkillManager, sync_token: str) -> None:
    package = _zip_bytes({"weather/SKILL.md": _skill_md("weather").encode("utf-8")})
    content_type, form = _apply_form(package, sha256="not-hex")
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token),
        content_type=content_type,
        body=form,
    )
    assert status == 400
    assert payload["code"] == ERROR_SKILL_SYNC_INVALID_PAYLOAD


@pytest.mark.asyncio
async def test_apply_strict_success_and_idempotent(
    manager: SkillManager, sync_token: str
) -> None:
    package = _zip_bytes(
        {
            "weather/SKILL.md": _skill_md("weather", description="天气查询").encode("utf-8"),
            "ppt/SKILL.md": _skill_md("ppt", description="PPT 生成").encode("utf-8"),
        }
    )
    content_type, form = _apply_form(package)
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 200
    assert payload["success"] is True
    assert len(payload["applied"]) == 2
    installed = {item["name"]: item for item in payload["applied"]}
    assert installed["weather"]["action"] == "installed"
    assert installed["weather"]["version"] is None  # 新装为 null（设计 5.4）
    assert (manager._skills_dir / "weather" / "SKILL.md").is_file()

    # 同包重复 apply（strict + overwrite=false）：整单 409（设计 5.4 预检闭环）
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 409
    assert payload["code"] == ERROR_SKILL_ALREADY_EXISTS

    # best_effort：同名冲突落 skipped，幂等无副作用
    content_type, form = _apply_form(package, mode="best_effort")
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 200
    assert payload["applied"] == []
    assert len(payload["skipped"]) == 2
    assert all(s["reason"] == ERROR_SKILL_ALREADY_EXISTS for s in payload["skipped"])
    # 内容未被覆盖
    weather_md = (manager._skills_dir / "weather" / "SKILL.md").read_text(encoding="utf-8")
    assert "天气查询" in weather_md


@pytest.mark.asyncio
async def test_apply_overwrite_preserves_server_version(
    manager: SkillManager, sync_token: str
) -> None:
    existing = _write_skill(manager._skills_dir / "weather", name="weather")
    manager._add_local_skill({"name": "weather", "source": "local", "origin": "local"})
    # server 已有 current_version
    _write_archive_index(existing, current_version="1.9.0", storage_id="sid-w")

    package = _zip_bytes(
        {"weather/SKILL.md": _skill_md("weather", description="新版描述").encode("utf-8")}
    )
    content_type, form = _apply_form(package, overwrite="true")
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 200
    assert payload["applied"][0]["action"] == "overwritten"
    assert payload["applied"][0]["version"] == "1.9.0"  # 覆盖保留 server 原 current_version


@pytest.mark.asyncio
async def test_apply_strict_precheck_closure(
    manager: SkillManager, sync_token: str
) -> None:
    """strict：后一个技能冲突时，前一个技能不得安装（预检闭环）."""
    _write_skill(manager._skills_dir / "occupied", name="occupied")
    manager._add_local_skill({"name": "occupied", "source": "local", "origin": "local"})

    package = _zip_bytes(
        {
            "fresh/SKILL.md": _skill_md("fresh").encode("utf-8"),
            "occupied/SKILL.md": _skill_md("occupied").encode("utf-8"),
        }
    )
    content_type, form = _apply_form(package)
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 409
    assert payload["code"] == ERROR_SKILL_ALREADY_EXISTS
    # 预检失败整单不安装：fresh 不得落盘
    assert not (manager._skills_dir / "fresh").exists()


@pytest.mark.asyncio
async def test_apply_best_effort_groups(
    manager: SkillManager, sync_token: str
) -> None:
    _write_skill(manager._skills_dir / "occupied", name="occupied")
    manager._add_local_skill({"name": "occupied", "source": "local", "origin": "local"})

    package = _zip_bytes(
        {
            "fresh/SKILL.md": _skill_md("fresh").encode("utf-8"),
            "occupied/SKILL.md": _skill_md("occupied").encode("utf-8"),
            "broken/SKILL.md": "not-frontmatter".encode("utf-8"),  # 无 name/description
        }
    )
    content_type, form = _apply_form(package, mode="best_effort")
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 200
    assert payload["success"] is True
    applied_names = {item["name"] for item in payload["applied"]}
    skipped = {item["name"]: item for item in payload["skipped"]}
    assert applied_names == {"fresh"}
    assert skipped["occupied"]["reason"] == ERROR_SKILL_ALREADY_EXISTS
    assert skipped["broken"]["reason"] == "SKILL_INVALID_METADATA"
    assert (manager._skills_dir / "fresh" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_apply_builtin_protection_403(
    manager: SkillManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sync_token: str
) -> None:
    builtin_dir = tmp_path / "builtin_skills"
    _write_skill(builtin_dir / "core-skill", name="core-skill")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: builtin_dir,
    )
    # 已安装到用户目录的 builtin 副本
    _write_skill(manager._skills_dir / "core-skill", name="core-skill")

    package = _zip_bytes(
        {"core-skill/SKILL.md": _skill_md("core-skill", description="篡改").encode("utf-8")}
    )
    content_type, form = _apply_form(package, overwrite="true")
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 403
    assert payload["code"] == ERROR_SKILL_BUILTIN_READ_ONLY


@pytest.mark.asyncio
async def test_apply_skillpack_rejected(
    manager: SkillManager, sync_token: str
) -> None:
    pack_md = (
        "---\n"
        "name: my-pack\n"
        "description: pack\n"
        "kind: skillpack\n"
        "members:\n"
        "  - name: member-a\n"
        "---\n# Pack\n"
    )
    package = _zip_bytes({"my-pack/SKILL.md": pack_md.encode("utf-8")})
    content_type, form = _apply_form(package)
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 400
    assert payload["code"] == ERROR_SKILL_OPERATION_UNSUPPORTED

    # best_effort：落 skipped
    content_type, form = _apply_form(package, mode="best_effort")
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 200
    assert payload["skipped"][0]["reason"] == ERROR_SKILL_OPERATION_UNSUPPORTED


@pytest.mark.asyncio
async def test_apply_directory_name_must_match_frontmatter(
    manager: SkillManager, sync_token: str
) -> None:
    package = _zip_bytes(
        {"dir-name/SKILL.md": _skill_md("frontmatter-name").encode("utf-8")}
    )
    content_type, form = _apply_form(package)
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 400
    assert payload["code"] == ERROR_SKILL_SYNC_INVALID_PAYLOAD
    assert "dir-name" in payload["message"] and "frontmatter-name" in payload["message"]


@pytest.mark.asyncio
async def test_diff_marks_name_mismatch_and_roundtrip_rejected(
    manager: SkillManager, sync_token: str
) -> None:
    """目录名与 frontmatter name 不一致的技能：diff 标记 name_mismatch，
    package 拉得下来，原样推回 400（client SDK 据标记跳过推送）."""
    import shutil as _shutil

    # 目录名 skill-creator-normal，frontmatter 写 name: skill-creator（真实存在的形态）
    _write_skill(
        manager._skills_dir / "skill-creator-normal", name="skill-creator"
    )
    _write_skill(manager._skills_dir / "normal-skill", name="normal-skill")
    manager._add_local_skill({"name": "skill-creator-normal", "source": "local", "origin": "local"})
    manager._add_local_skill({"name": "normal-skill", "source": "local", "origin": "local"})

    # ① diff：server digest 标记 name_mismatch
    result = await manager.handle_skill_sync_diff({"client_skills": []})
    by_name = {}
    for group in result["categories"]:
        for items in group["items"].values():
            for item in items:
                if item["server"] is not None:
                    by_name[item["name"]] = item["server"]
    assert by_name["skill-creator-normal"]["name_mismatch"] is True
    assert by_name["normal-skill"]["name_mismatch"] is False

    # ② package：该技能可正常拉取（仅下行）
    pkg = await manager.handle_skill_sync_package(
        {"skills": [{"name": "skill-creator-normal"}]}
    )
    try:
        with zipfile.ZipFile(pkg["zip_path"]) as zf:
            names = zf.namelist()
            assert "skill-creator-normal/SKILL.md" in names
            pulled = zf.read("skill-creator-normal/SKILL.md")

        # ③ client 原样打回 zip 推送：apply 恒 400（标记存在的意义）
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("skill-creator-normal/SKILL.md", pulled)
        content_type, form = _apply_form(buf.getvalue())
        status, payload = handle_sync_apply_http(
            authorization=_auth(sync_token), content_type=content_type, body=form
        )
        assert status == 400
        assert payload["code"] == ERROR_SKILL_SYNC_INVALID_PAYLOAD
    finally:
        _shutil.rmtree(Path(pkg["zip_path"]).parent, ignore_errors=True)


def test_concurrent_apply_keeps_all_local_skill_records(
    manager: SkillManager, sync_token: str
) -> None:
    """并发 apply 不丢 local_skills 记录（进程内锁回归，P2-3）."""
    import threading

    def _push(name: str, results: list) -> None:
        package = _zip_bytes({f"{name}/SKILL.md": _skill_md(name).encode("utf-8")})
        content_type, form = _apply_form(package)
        results.append(
            handle_sync_apply_http(
                authorization=_auth(sync_token), content_type=content_type, body=form
            )
        )

    results: list = []
    threads = [
        threading.Thread(target=_push, args=(f"concurrent-{i}", results))
        for i in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(status == 200 for status, _ in results)
    disk_state = json.loads(
        (manager._skills_dir / "skills_state.json").read_text(encoding="utf-8")
    )
    recorded = {ls["name"] for ls in disk_state.get("local_skills", [])}
    for i in range(6):
        assert f"concurrent-{i}" in recorded, f"concurrent-{i} 记录丢失"
    # 原子写不残留临时文件
    leftovers = [
        p.name
        for p in manager._skills_dir.iterdir()
        if p.name.startswith(".") and p.name.endswith(".tmp")
    ]
    assert leftovers == [], f"残留临时文件: {leftovers}"


@pytest.mark.asyncio
async def test_apply_rejects_root_level_files(
    manager: SkillManager, sync_token: str
) -> None:
    package = _zip_bytes(
        {
            "weather/SKILL.md": _skill_md("weather").encode("utf-8"),
            "stray.txt": b"not allowed",
        }
    )
    content_type, form = _apply_form(package)
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 400
    assert payload["code"] == ERROR_SKILL_SYNC_INVALID_PAYLOAD


@pytest.mark.asyncio
async def test_apply_empty_package(manager: SkillManager, sync_token: str) -> None:
    package = _zip_bytes({})
    content_type, form = _apply_form(package)
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 400
    assert payload["code"] == ERROR_SKILL_SYNC_EMPTY_PACKAGE


@pytest.mark.asyncio
async def test_apply_origin_recorded(manager: SkillManager, sync_token: str) -> None:
    package = _zip_bytes({"weather/SKILL.md": _skill_md("weather").encode("utf-8")})
    # 客户端试图伪造 origin：服务端必须强制覆盖为 sync_client（3.5 回归）
    content_type, form = _apply_form(package, origin="evil-origin")
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 200
    assert payload["success"] is True
    # origin 落盘供溯源展示：从磁盘 state 断言（HTTP 层每次请求用独立
    # SkillManager 实例，fixture manager 的内存 state 不自动刷新）
    disk_state = json.loads(
        (manager._skills_dir / "skills_state.json").read_text(encoding="utf-8")
    )
    local = {ls["name"]: ls for ls in disk_state.get("local_skills", [])}
    assert local["weather"]["origin"] == "sync_client"
    assert local["weather"]["source"] == "local"


# ---------------------------------------------------------------------------
# WS 路由：apply 落盘后 agent reload 判定（interface._handle_skills_request）
# ---------------------------------------------------------------------------


class _StubManager:
    """替身 SkillManager：记录 handler 调用并返回预置 payload."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def reload_state(self) -> None:
        self.calls.append("reload_state")

    async def handle_skill_sync_apply(self, params: dict) -> dict:
        self.calls.append("handle_skill_sync_apply")
        return self.payload

    async def handle_skill_sync_reload(self, params: dict) -> dict:
        self.calls.append("handle_skill_sync_reload")
        return self.payload


def _build_facade_with_stub(stub: _StubManager) -> Any:
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    facade = JiuWenSwarm.__new__(JiuWenSwarm)
    facade._skill_manager = stub
    return facade


def _sync_reload_request(applied: list[str]) -> Any:
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod

    return AgentRequest(
        request_id="test-sync-reload",
        channel_id="web",
        session_id="sess-1",
        req_method=ReqMethod.SKILL_SYNC_RELOAD,
        params={"applied": applied},
    )


@pytest.mark.asyncio
async def test_sync_reload_notification_triggers_agent_reload() -> None:
    """skills.sync.reload 通知（applied 非空）→ 重建 agent + 刷新 rails."""
    stub = _StubManager({"success": True, "applied": ["a", "b"]})
    facade = _build_facade_with_stub(stub)
    reloaded: list[str] = []

    async def fake_create_instance() -> None:
        reloaded.append("create_instance")

    async def fake_reload_rails(session_id=None) -> None:
        reloaded.append(f"rails:{session_id}")

    monkey_local = pytest.MonkeyPatch()
    monkey_local.setattr(facade, "create_instance", fake_create_instance)
    monkey_local.setattr(facade, "_reload_team_skill_rails", fake_reload_rails)
    try:
        response = await facade._handle_skills_request(
            _sync_reload_request(["a", "b"])
        )
    finally:
        monkey_local.undo()

    assert response is not None and response.ok
    assert stub.calls == ["reload_state", "handle_skill_sync_reload"]
    assert "create_instance" in reloaded


@pytest.mark.asyncio
async def test_sync_reload_empty_applied_skips_agent_reload() -> None:
    """reload 通知 applied 为空时不重建 agent（无技能落盘）."""
    stub = _StubManager({"success": True, "applied": []})
    facade = _build_facade_with_stub(stub)
    reloaded: list[str] = []

    async def fake_create_instance() -> None:
        reloaded.append("create_instance")

    async def fake_reload_rails(session_id=None) -> None:
        reloaded.append(f"rails:{session_id}")

    monkey_local = pytest.MonkeyPatch()
    monkey_local.setattr(facade, "create_instance", fake_create_instance)
    monkey_local.setattr(facade, "_reload_team_skill_rails", fake_reload_rails)
    try:
        response = await facade._handle_skills_request(_sync_reload_request([]))
    finally:
        monkey_local.undo()

    assert response is not None and response.ok
    assert "create_instance" not in reloaded


@pytest.mark.asyncio
async def test_sync_reload_partial_landing_triggers_agent_reload() -> None:
    """reload 通知 applied 非空（web 进程部分成功落盘）必须 reload.

    回归缺陷：判定若用 `not payload.get("success")` 而非 applied 为空，
    部分成功场景会错误跳过 reload，已落盘技能对 agent 不可见。
    """
    stub = _StubManager(
        {
            "success": False,
            "applied": ["a"],
        }
    )
    facade = _build_facade_with_stub(stub)
    reloaded: list[str] = []

    async def fake_create_instance() -> None:
        reloaded.append("create_instance")

    async def fake_reload_rails(session_id=None) -> None:
        reloaded.append(f"rails:{session_id}")

    monkey_local = pytest.MonkeyPatch()
    monkey_local.setattr(facade, "create_instance", fake_create_instance)
    monkey_local.setattr(facade, "_reload_team_skill_rails", fake_reload_rails)
    try:
        response = await facade._handle_skills_request(_sync_reload_request(["a"]))
    finally:
        monkey_local.undo()

    assert response is not None and response.ok
    assert stub.calls == ["reload_state", "handle_skill_sync_reload"]
    assert "create_instance" in reloaded, "applied 非空场景必须重建 agent 实例"


@pytest.mark.asyncio
async def test_sync_apply_no_landed_skill_skips_agent_reload() -> None:
    """WS apply 路由已收缩（仅剩 skills.sync.reload），无落盘 reload 不重建."""
    stub = _StubManager({"success": True, "applied": []})
    facade = _build_facade_with_stub(stub)
    reloaded: list[str] = []

    async def fake_create_instance() -> None:
        reloaded.append("create_instance")

    async def fake_reload_rails(session_id=None) -> None:
        reloaded.append(f"rails:{session_id}")

    monkey_local = pytest.MonkeyPatch()
    monkey_local.setattr(facade, "create_instance", fake_create_instance)
    monkey_local.setattr(facade, "_reload_team_skill_rails", fake_reload_rails)
    try:
        response = await facade._handle_skills_request(_sync_reload_request([]))
    finally:
        monkey_local.undo()

    assert response is not None and response.ok
    assert "create_instance" not in reloaded, "无落盘场景不应重建 agent 实例"


def test_ws_routes_only_reload_registered() -> None:
    """skills.sync.diff/package/apply 不得进 WS 路由（绕过 token 鉴权）."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import _SKILL_ROUTES
    from jiuwenswarm.common.schema.message import ReqMethod

    assert ReqMethod.SKILL_SYNC_RELOAD in _SKILL_ROUTES
    for method in (
        ReqMethod.SKILL_SYNC_DIFF,
        ReqMethod.SKILL_SYNC_PACKAGE,
        ReqMethod.SKILL_SYNC_APPLY,
    ):
        assert method not in _SKILL_ROUTES, f"{method} 不应注册 WS 路由"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["skills.sync.diff", "skills.sync.package", "skills.sync.apply"],
)
async def test_ws_rejected_sync_methods_return_error_not_chat(method: str) -> None:
    """被收缩出路由的 sync 方法经 WS 调用：显式错误响应，不落入聊天路径.

    回归：未显式拒绝时，合法枚举方法沿 facade 链路落到聊天处理，
    空 query 触发一次 LLM 调用。
    """
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod

    stub = _StubManager({"success": True, "applied": []})
    facade = _build_facade_with_stub(stub)
    request = AgentRequest(
        request_id="test-ws-rejected",
        channel_id="web",
        session_id="sess-1",
        req_method=ReqMethod(method),
        params={},
    )
    response = await facade._handle_skills_request(request)
    assert response is not None, "必须显式拒绝而非放行到聊天路径"
    assert response.ok is False
    assert response.payload["code"] == "SKILL_SYNC_WS_UNSUPPORTED"
    assert "HTTP" in response.payload["message"]
    assert stub.calls == [], "handler 不应被调用"


# ---------------------------------------------------------------------------
# package HTTP 层：X-Package-Sha256 头 + 流式发送
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_package_http_sends_zip_with_sha256_header(
    manager: SkillManager, sync_token: str
) -> None:
    _write_skill(manager._skills_dir / "weather", name="weather")
    manager._add_local_skill({"name": "weather", "source": "local", "origin": "local"})

    sent: dict[str, object] = {}

    def send_file(zip_path: Path, filename: str, sha256: str, size: int) -> None:
        sent["path"] = zip_path
        sent["filename"] = filename
        sent["sha256"] = sha256
        sent["size"] = size
        sent["body"] = zip_path.read_bytes()

    status, payload = handle_sync_package_http(
        authorization=_auth(sync_token),
        body=json.dumps({"skills": [{"name": "weather"}]}).encode("utf-8"),
        send_file=send_file,
    )
    assert status == 200
    assert payload is None
    assert str(sent["filename"]).startswith("skills_sync_")
    assert sent["filename"] == Path(str(sent["path"])).name
    assert hashlib.sha256(sent["body"]).hexdigest() == sent["sha256"]  # type: ignore[arg-type]
    assert len(sent["body"]) == sent["size"]
    # 临时文件在 finally 清理后删除
    assert not Path(str(sent["path"])).exists()


@pytest.mark.asyncio
async def test_package_http_error_keeps_json_response(
    manager: SkillManager, sync_token: str
) -> None:
    status, payload = handle_sync_package_http(
        authorization=_auth(sync_token),
        body=json.dumps({"skills": [{"name": "missing"}]}).encode("utf-8"),
        send_file=lambda *args: None,
    )
    assert status == 404
    assert payload is not None
    assert payload["code"] == ERROR_SKILL_NOT_FOUND


@pytest.mark.asyncio
async def test_package_http_send_failure_after_headers_not_double_written(
    manager: SkillManager, sync_token: str
) -> None:
    """send_file 头已发出后 body 阶段 OSError：不得再返回 500 JSON（双响应损坏）.

    回归 bot 审查缺陷：该异常曾落到外层 except 返回 (500, error_body)，
    调用方再 _write_json 会写出第二个状态行。修复后仅记录日志、返回
    (200, None)，由 client 侧 sha256 校验失败触发重试。
    """
    _write_skill(manager._skills_dir / "weather", name="weather")
    manager._add_local_skill({"name": "weather", "source": "local", "origin": "local"})

    def send_file_raises_mid_body(
        zip_path: Path, filename: str, sha256: str, size: int
    ) -> None:
        # 模拟：响应头（200）已写出，body 读取/写出阶段磁盘 IO 故障
        raise OSError("disk failure while streaming body")

    status, payload = handle_sync_package_http(
        authorization=_auth(sync_token),
        body=json.dumps({"skills": [{"name": "weather"}]}).encode("utf-8"),
        send_file=send_file_raises_mid_body,
    )
    assert status == 200
    assert payload is None  # 不再尝试写 JSON 错误响应


# ---------------------------------------------------------------------------
# 端到端：diff → package → apply 双向闭环
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_sync_roundtrip_push_then_diff(
    manager: SkillManager, sync_token: str
) -> None:
    """client_only → apply 推送 → 二轮 diff 应 in_sync."""
    # client 侧技能内容与上传 zip 严格一致（含脚本文件）
    skill_md_content = _skill_md("weather", description="天气查询") + "# Hi\n"
    client_dir = manager._skills_dir.parent / "client-side" / "weather"
    client_dir.mkdir(parents=True)
    (client_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
    (client_dir / "scripts").mkdir()
    (client_dir / "scripts" / "tool.py").write_text("print('x')\n", encoding="utf-8")
    client_checksum = compute_content_checksum(client_dir)

    diff = await manager.handle_skill_sync_diff(
        {"client_skills": [_client_digest("weather", checksum=client_checksum)]}
    )
    assert diff["summary"]["client_only"] == 1

    # client 打 zip 上传（结构 = <name>/SKILL.md …，内容与本地一致）
    package = _zip_bytes(
        {
            "weather/SKILL.md": skill_md_content.encode("utf-8"),
            "weather/scripts/tool.py": b"print('x')\n",
        }
    )
    content_type, form = _apply_form(package)
    status, payload = handle_sync_apply_http(
        authorization=_auth(sync_token), content_type=content_type, body=form
    )
    assert status == 200 and payload["success"] is True

    # 二轮 diff：server 内容与 client 一致 → in_sync
    diff2 = await manager.handle_skill_sync_diff(
        {"client_skills": [_client_digest("weather", checksum=client_checksum)]}
    )
    assert diff2["summary"]["in_sync"] == 1
