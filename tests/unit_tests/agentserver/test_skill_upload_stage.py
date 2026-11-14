# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版 Skill 按包上传：暂存、签名与 finalize 安装契约。"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill import skill_manager as skill_manager_module
from jiuwenswarm.server.runtime.skill.skill_manager import (
    ERROR_SKILL_UPLOAD_CHECK_UNAVAILABLE,
    ERROR_SKILL_UPLOAD_DIGEST_MISMATCH,
    ERROR_SKILL_UPLOAD_NAME_CONFLICT,
    ERROR_SKILL_UPLOAD_PACKAGE_INVALID,
    ERROR_SKILL_UPLOAD_PACKAGE_TOO_LARGE,
    ERROR_SKILL_UPLOAD_STAGE_EXPIRED,
    ERROR_SKILL_UPLOAD_STAGE_NOT_FOUND,
    ERROR_SKILL_UNSAFE_PATH,
    SkillManager,
    SkillRpcError,
)
from jiuwenswarm.server.runtime.skill.skill_upload_approval import (
    is_valid_upload_id,
    skill_tree_digest,
    validate_skill_name,
)


class _FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload_file(self, path: Path, key: str, *, session_id: str = "") -> dict:
        _ = session_id
        data = Path(path).read_bytes()
        self.objects[key] = data
        return {
            "size": len(data),
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        }

    def remove_key(self, key: str) -> None:
        self.objects.pop(key, None)


def _skill_zip(name: str = "upload-skill") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\ndescription: 上传审批测试\nversion: 1.0.0\n---\n# Body\n",
        )
        package.writestr(f"{name}/assets/data.bin", b"payload")
    return buffer.getvalue()


@pytest.fixture(name="pending_root")
def _pending_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "_skill_upload_pending"
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_upload_approval.stage_root",
        lambda: root,
    )
    return root


@pytest.fixture(name="store")
def _store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    fake = _FakeStore()
    monkeypatch.setattr(
        "jiuwenswarm.common.obs_files.AttachmentStore.from_env",
        classmethod(lambda cls: fake),
    )
    return fake


@pytest.fixture(name="manager")
def _manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SkillManager:
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
    monkeypatch.setattr(
        skill_manager_module, "is_enterprise", lambda: True
    )
    monkeypatch.setattr(
        "jiuwenswarm.enterprise.tenant.tenant_context.TenantContext.current_tenant_key",
        classmethod(lambda cls: "tenant-1"),
    )
    instance = SkillManager()
    monkeypatch.setattr(
        instance,
        "assert_enterprise_upload_path",
        lambda path_str, session_id: Path(path_str),
    )
    return instance


@pytest.mark.parametrize("name", ["ok-skill", "skill_1", "技能"])
def test_validate_skill_name_accepts_safe_names(name: str) -> None:
    assert validate_skill_name(name) == name


@pytest.mark.parametrize(
    "name", ["", ".", "..", "_hidden", ".hidden", "a/b", "a\\b", "a..b", "x" * 129]
)
def test_validate_skill_name_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError):
        validate_skill_name(name)


def test_skill_tree_digest_tracks_relative_paths_and_sizes(tmp_path: Path) -> None:
    root = tmp_path / "content"
    (root / "nested").mkdir(parents=True)
    (root / "SKILL.md").write_text("# body", encoding="utf-8")
    (root / "nested" / "a.bin").write_bytes(b"abc")

    digest, file_count, total = skill_tree_digest(root)

    assert digest.startswith("sha256:")
    assert file_count == 2
    assert total == len("# body") + 3


@pytest.mark.asyncio
async def test_stage_extracts_descriptor(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
) -> None:
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())

    result = await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1", "user_id": "u1"}
    )

    descriptor = result["package_info"]
    assert result["success"] is True
    assert result["existing_skill"] is None
    assert is_valid_upload_id(descriptor["upload_id"])
    assert descriptor["skill_name"] == "upload-skill"
    assert descriptor["file_count"] == 2
    assert descriptor["package"]["filename"] == "demo.zip"
    assert descriptor["package"]["sha256"].startswith("sha256:")
    object_key = descriptor["package"]["object_key"]
    assert object_key.startswith("skill-approvals/")
    assert object_key.endswith("/demo.zip")
    assert object_key in store.objects
    assert "download_url" not in descriptor["package"]
    assert "signature" not in result

    staging = pending_root / descriptor["upload_id"]
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["descriptor"] == descriptor
    assert "signature" not in manifest
    assert (staging / "content" / "SKILL.md").is_file()
    assert len(store.objects) == 1


@pytest.mark.asyncio
async def test_stage_runs_off_event_loop_thread(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解包/哈希/OBS 上传不能在事件循环线程执行，否则阻塞整个 AgentServer。"""
    _ = store, pending_root
    loop_thread = threading.get_ident()
    executed: list[int] = []
    original = manager.stage_skill_upload_sync

    def recording(params: dict) -> dict:
        executed.append(threading.get_ident())
        return original(params)

    monkeypatch.setattr(manager, "stage_skill_upload_sync", recording)
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())

    await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1"}
    )

    assert len(executed) == 1
    assert executed[0] != loop_thread


@pytest.mark.asyncio
async def test_stage_maps_staging_mkdir_failure_to_check_unavailable(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """暂存目录创建失败应映射为可重试的业务错误，而不是裸 OSError。"""
    _ = store, pending_root
    blocked = tmp_path / "blocked-as-file"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_upload_approval.stage_root",
        lambda: blocked,
    )
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())

    with pytest.raises(SkillRpcError) as excinfo:
        await manager.handle_skills_upload_stage(
            {"path": str(archive), "session_id": "session-1"}
        )

    assert excinfo.value.code == ERROR_SKILL_UPLOAD_CHECK_UNAVAILABLE


@pytest.mark.asyncio
async def test_stage_maps_internal_zip_limit_to_package_too_large(
    tmp_path: Path,
    manager: SkillManager,
    pending_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = pending_root
    monkeypatch.setattr(skill_manager_module, "_SKILL_ZIP_MAX_FILE_COUNT", 1)
    archive = tmp_path / "too-many-files.zip"
    archive.write_bytes(_skill_zip())

    with pytest.raises(SkillRpcError) as excinfo:
        await manager.handle_skills_upload_stage(
            {"path": str(archive), "session_id": "session-1"}
        )

    assert excinfo.value.code == ERROR_SKILL_UPLOAD_PACKAGE_TOO_LARGE


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["demo.skill.zip", "demo.skill"])
async def test_stage_rejects_skill_package_suffixes(
    tmp_path: Path,
    manager: SkillManager,
    filename: str,
) -> None:
    archive = tmp_path / filename
    archive.write_bytes(_skill_zip())

    with pytest.raises(SkillRpcError) as excinfo:
        await manager.handle_skills_upload_stage(
            {"path": str(archive), "session_id": "session-1"}
        )

    assert excinfo.value.code == ERROR_SKILL_UPLOAD_PACKAGE_INVALID


@pytest.mark.asyncio
async def test_finalize_rejects_digest_mismatch(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
) -> None:
    _ = store
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())
    staged = await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1"}
    )
    descriptor = staged["package_info"]

    with pytest.raises(SkillRpcError) as excinfo:
        await manager.handle_skills_upload_finalize(
            {
                "order_num": f"apr_{descriptor['upload_id']}",
                "upload_id": descriptor["upload_id"],
                "skill_name": descriptor["skill_name"],
                "expected_digest": "sha256:" + "0" * 64,
            }
        )

    assert excinfo.value.code == ERROR_SKILL_UPLOAD_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_finalize_installs_and_reports_already_installed_on_retry(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
) -> None:
    _ = store
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())
    staged = await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1"}
    )
    descriptor = staged["package_info"]
    params = {
        "order_num": f"apr_{descriptor['upload_id']}",
        "upload_id": descriptor["upload_id"],
        "skill_name": descriptor["skill_name"],
        "expected_digest": descriptor["digest"],
        "replace": False,
    }

    first = await manager.handle_skills_upload_finalize(dict(params))
    second = await manager.handle_skills_upload_finalize(dict(params))

    assert first["success"] is True
    assert first["already_installed"] is False
    assert first["skill"]["name"] == "upload-skill"
    assert second["success"] is True
    assert second["already_installed"] is True
    assert (pending_root / descriptor["upload_id"]).is_dir()


@pytest.mark.asyncio
async def test_finalize_retry_with_mismatched_digest_is_not_already_installed(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
) -> None:
    """重试的审批摘要对不上时不得按已安装返回成功，仍需做摘要校验。"""
    _ = store
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())
    staged = await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1"}
    )
    descriptor = staged["package_info"]
    params = {
        "order_num": f"apr_{descriptor['upload_id']}",
        "upload_id": descriptor["upload_id"],
        "skill_name": descriptor["skill_name"],
        "expected_digest": descriptor["digest"],
        "replace": False,
    }
    first = await manager.handle_skills_upload_finalize(dict(params))
    assert first["already_installed"] is False

    with pytest.raises(SkillRpcError) as excinfo:
        await manager.handle_skills_upload_finalize(
            {**params, "expected_digest": "sha256:" + "0" * 64}
        )

    assert excinfo.value.code == ERROR_SKILL_UPLOAD_DIGEST_MISMATCH
    assert (pending_root / descriptor["upload_id"]).is_dir()


@pytest.mark.asyncio
async def test_finalize_retry_after_skill_dir_removed_reinstalls(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
) -> None:
    """安装目录被删后重试应真正重装，而不是凭安装记录报成功。"""
    _ = store
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())
    staged = await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1"}
    )
    descriptor = staged["package_info"]
    params = {
        "order_num": f"apr_{descriptor['upload_id']}",
        "upload_id": descriptor["upload_id"],
        "skill_name": descriptor["skill_name"],
        "expected_digest": descriptor["digest"],
        "replace": False,
    }
    first = await manager.handle_skills_upload_finalize(dict(params))
    assert first["already_installed"] is False

    shutil.rmtree(tmp_path / "skills" / "upload-skill")

    retried = await manager.handle_skills_upload_finalize(dict(params))

    assert retried["success"] is True
    assert retried["already_installed"] is False
    skill_md = tmp_path / "skills" / "upload-skill" / "SKILL.md"
    assert "# Body" in skill_md.read_text(encoding="utf-8")


def _overwrite_with_chat_artifact(manager: SkillManager, tmp_path: Path) -> dict:
    """模拟聊天产物以 force=True 覆盖同名用户技能（不带 upload 验证信息）。"""
    override = tmp_path / "chat-artifact" / "upload-skill"
    override.mkdir(parents=True)
    (override / "SKILL.md").write_text(
        "---\nname: upload-skill\ndescription: 聊天产物覆盖\nversion: 2.0.0\n---\n# Override\n",
        encoding="utf-8",
    )
    return manager._install_imported_skill_dir(
        override,
        force=True,
        origin="import_local:chat-artifact",
        source_trusted=True,
    )


@pytest.mark.asyncio
async def test_finalize_retry_after_external_overwrite_is_not_already_installed(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
) -> None:
    _ = store
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())
    staged = await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1"}
    )
    descriptor = staged["package_info"]
    params = {
        "order_num": f"apr_{descriptor['upload_id']}",
        "upload_id": descriptor["upload_id"],
        "skill_name": descriptor["skill_name"],
        "expected_digest": descriptor["digest"],
        "replace": False,
    }
    first = await manager.handle_skills_upload_finalize(dict(params))
    assert first["already_installed"] is False

    installed = _overwrite_with_chat_artifact(manager, tmp_path)
    assert installed.get("success") is True
    record = manager._find_skill_installation(name="upload-skill")
    assert isinstance(record, dict)
    assert "verification" not in record

    with pytest.raises(SkillRpcError) as excinfo:
        await manager.handle_skills_upload_finalize(dict(params))

    assert excinfo.value.code == ERROR_SKILL_UPLOAD_NAME_CONFLICT
    assert (pending_root / descriptor["upload_id"]).is_dir()


@pytest.mark.asyncio
async def test_finalize_retry_with_replace_reinstalls_approved_package(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
) -> None:
    _ = store
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())
    staged = await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1"}
    )
    descriptor = staged["package_info"]
    params = {
        "order_num": f"apr_{descriptor['upload_id']}",
        "upload_id": descriptor["upload_id"],
        "skill_name": descriptor["skill_name"],
        "expected_digest": descriptor["digest"],
        "replace": False,
    }
    first = await manager.handle_skills_upload_finalize(dict(params))
    assert first["already_installed"] is False
    installed = _overwrite_with_chat_artifact(manager, tmp_path)
    assert installed.get("success") is True

    retried = await manager.handle_skills_upload_finalize(
        {**params, "replace": True}
    )

    assert retried["success"] is True
    assert retried["already_installed"] is False
    skill_md = tmp_path / "skills" / "upload-skill" / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8")
    assert "# Body" in content
    assert "# Override" not in content
    record = manager._find_skill_installation(name="upload-skill")
    assert isinstance(record, dict)
    assert record["verification"]["upload_id"] == descriptor["upload_id"]
    assert (pending_root / descriptor["upload_id"]).is_dir()


@pytest.mark.parametrize(
    ("raw_replace", "expected_force"),
    [
        (False, False),
        (True, True),
        ("false", False),
        ("true", False),
        (1, False),
    ],
)
@pytest.mark.asyncio
async def test_finalize_only_trusts_real_bool_replace(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_replace: object,
    expected_force: bool,
) -> None:
    """字符串 "false" 经 bool() 会变 True，必须只接受真正的 bool。"""
    _ = store, pending_root
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())
    staged = await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1"}
    )
    descriptor = staged["package_info"]
    captured: dict[str, bool] = {}

    def _install(src: Path, *, force: bool, **kwargs: object) -> dict:
        _ = src, kwargs
        captured["force"] = force
        return {"success": True, "skill": {"name": descriptor["skill_name"]}}

    monkeypatch.setattr(manager, "_install_imported_skill_dir", _install)

    result = await manager.handle_skills_upload_finalize(
        {
            "order_num": f"apr_{descriptor['upload_id']}",
            "upload_id": descriptor["upload_id"],
            "skill_name": descriptor["skill_name"],
            "expected_digest": descriptor["digest"],
            "replace": raw_replace,
        }
    )

    assert result["success"] is True
    assert captured["force"] is expected_force


@pytest.mark.asyncio
async def test_finalize_raises_with_code_when_install_returns_failure(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安装失败只带 detail 时也必须抛带 code 的异常，避免网关误记 installed。"""
    _ = store, pending_root
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())
    staged = await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1"}
    )
    descriptor = staged["package_info"]

    monkeypatch.setattr(
        manager,
        "_install_imported_skill_dir",
        lambda *args, **kwargs: {"success": False, "detail": "invalid skill name"},
    )

    with pytest.raises(SkillRpcError) as excinfo:
        await manager.handle_skills_upload_finalize(
            {
                "order_num": f"apr_{descriptor['upload_id']}",
                "upload_id": descriptor["upload_id"],
                "skill_name": descriptor["skill_name"],
                "expected_digest": descriptor["digest"],
                "replace": False,
            }
        )

    assert excinfo.value.code == ERROR_SKILL_UPLOAD_PACKAGE_INVALID
    assert "invalid skill name" in str(excinfo.value)


@pytest.mark.asyncio
async def test_finalize_missing_content_reports_stage_not_found(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
) -> None:
    """暂存内容目录缺失时应报暂存失效，而不是误导性的摘要不一致。"""
    _ = store
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())
    staged = await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1"}
    )
    descriptor = staged["package_info"]
    shutil.rmtree(pending_root / descriptor["upload_id"] / "content")

    with pytest.raises(SkillRpcError) as excinfo:
        await manager.handle_skills_upload_finalize(
            {
                "order_num": f"apr_{descriptor['upload_id']}",
                "upload_id": descriptor["upload_id"],
                "skill_name": descriptor["skill_name"],
                "expected_digest": descriptor["digest"],
            }
        )

    assert excinfo.value.code == ERROR_SKILL_UPLOAD_STAGE_NOT_FOUND


@pytest.mark.asyncio
async def test_finalize_expires_missing_or_stale_stage(
    manager: SkillManager,
    pending_root: Path,
    tmp_path: Path,
    store: _FakeStore,
) -> None:
    _ = store
    with pytest.raises(SkillRpcError) as missing:
        await manager.handle_skills_upload_finalize(
            {
                "order_num": "apr_missing",
                "upload_id": "c" * 32,
                "skill_name": "upload-skill",
                "expected_digest": "sha256:" + "1" * 64,
            }
        )
    assert missing.value.code == ERROR_SKILL_UPLOAD_STAGE_NOT_FOUND

    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())
    staged = await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1"}
    )
    descriptor = staged["package_info"]
    staging = pending_root / descriptor["upload_id"]
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    manifest["expires_at"] = "2000-01-01T00:00:00+00:00"
    (staging / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    with pytest.raises(SkillRpcError) as expired:
        await manager.handle_skills_upload_finalize(
            {
                "order_num": f"apr_{descriptor['upload_id']}",
                "upload_id": descriptor["upload_id"],
                "skill_name": descriptor["skill_name"],
                "expected_digest": descriptor["digest"],
            }
        )
    assert expired.value.code == ERROR_SKILL_UPLOAD_STAGE_EXPIRED
    assert not staging.exists()


def test_retention_days_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """保留天数可配：默认 7，最小 1，非法值回落 7，不设上限。"""
    from jiuwenswarm.server.runtime.skill import skill_upload_approval as approval

    monkeypatch.delenv(approval.RETENTION_DAYS_ENV, raising=False)
    assert approval.retention_days() == 7
    assert approval.stage_ttl_seconds() == 7 * 24 * 60 * 60

    monkeypatch.setenv(approval.RETENTION_DAYS_ENV, "3")
    assert approval.stage_ttl_seconds() == 3 * 24 * 60 * 60

    monkeypatch.setenv(approval.RETENTION_DAYS_ENV, "30")
    assert approval.retention_days() == 30

    monkeypatch.setenv(approval.RETENTION_DAYS_ENV, "0")
    assert approval.retention_days() == 7

    monkeypatch.setenv(approval.RETENTION_DAYS_ENV, "not-a-number")
    assert approval.retention_days() == 7


@pytest.mark.asyncio
async def test_stage_descriptor_uses_configured_retention(
    tmp_path: Path,
    manager: SkillManager,
    store: _FakeStore,
    pending_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = store, pending_root
    monkeypatch.setenv("SKILL_UPLOAD_RETENTION_DAYS", "2")
    archive = tmp_path / "demo.zip"
    archive.write_bytes(_skill_zip())
    staged = await manager.handle_skills_upload_stage(
        {"path": str(archive), "session_id": "session-1"}
    )
    descriptor = staged["package_info"]

    staged_at = datetime.fromisoformat(descriptor["staged_at"])
    stage_expires_at = datetime.fromisoformat(descriptor["stage_expires_at"])

    assert stage_expires_at - staged_at == timedelta(days=2)
    assert "download_expires_at" not in descriptor["package"]


def test_assert_enterprise_upload_path_requires_current_session_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """企业版上传只接受当前租户当前会话 uploads 下的普通文件。"""
    import jiuwenswarm.common.utils as common_utils
    from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

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
    manager = SkillManager()

    tenant_root = tmp_path / "workspace_t"
    sessions = tenant_root / "agent" / "sessions"
    uploads = sessions / "s1" / "uploads"
    uploads.mkdir(parents=True)
    monkeypatch.setattr(common_utils, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(
        TenantContext,
        "current_tenant_root",
        classmethod(lambda cls: tenant_root),
    )

    good = uploads / "good.zip"
    good.write_bytes(_skill_zip())
    assert manager.assert_enterprise_upload_path(str(good), "s1") == good.resolve()

    outside = tenant_root / "outside.zip"
    outside.write_bytes(_skill_zip())
    with pytest.raises(SkillRpcError) as exc:
        manager.assert_enterprise_upload_path(str(outside), "s1")
    assert exc.value.code == ERROR_SKILL_UNSAFE_PATH

    other_uploads = sessions / "s2" / "uploads"
    other_uploads.mkdir(parents=True)
    other = other_uploads / "other.zip"
    other.write_bytes(_skill_zip())
    with pytest.raises(SkillRpcError) as exc2:
        manager.assert_enterprise_upload_path(str(other), "s1")
    assert exc2.value.code == ERROR_SKILL_UNSAFE_PATH

    with pytest.raises(SkillRpcError) as exc3:
        manager.assert_enterprise_upload_path(str(good), "")
    assert exc3.value.code == ERROR_SKILL_UNSAFE_PATH

    with pytest.raises(SkillRpcError) as exc4:
        manager.assert_enterprise_upload_path(str(good), ".")
    assert exc4.value.code == ERROR_SKILL_UNSAFE_PATH
