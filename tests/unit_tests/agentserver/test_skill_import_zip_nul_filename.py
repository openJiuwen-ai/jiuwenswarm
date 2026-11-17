# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""skills.zip 导入对含 NUL 文件名成员的拒绝（回归 #6689）.

zipfile.writestr 本身拒绝 NUL，须按 issue 方法用字节补丁构造恶意样本：CPython
ZipInfo 在首个 NUL 处截断 filename，旧检查（对截断后名字）永不可达。
"""

import io
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager, SkillRpcError


def _build_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SKILL.md", "---\nname: nul-demo\ndescription: demo\n---\n# Body\n")
        zf.writestr("evil.md", "# evil")
    return buf.getvalue()


def _patched(raw: bytes) -> bytes:
    """等长字节补丁：evil.md -> ev\\x00l.md（本地头 + 中央目录一并替换）。"""
    return raw.replace(b"evil.md", b"ev\x00l.md")


@pytest.mark.asyncio
async def test_import_upload_rejects_nul_member_name(manager: SkillManager, tmp_path: Path) -> None:
    """含 NUL 文件名成员的 zip 导入应返回 SKILL_UNSAFE_PATH + "ZIP 包含非法文件名"。"""
    zip_path = tmp_path / "nul.zip"
    zip_path.write_bytes(_patched(_build_zip_bytes()))

    with pytest.raises(SkillRpcError) as exc_info:
        await manager.handle_skills_import_upload({"path": str(zip_path), "overwrite": False})

    assert exc_info.value.code == "SKILL_UNSAFE_PATH"
    assert "ZIP 包含非法文件名" in str(exc_info.value)
    assert not (manager._skills_dir / "nul-demo").exists()


@pytest.mark.asyncio
async def test_import_upload_accepts_unpatched_zip(manager: SkillManager, tmp_path: Path) -> None:
    """正向对照：同一 zip 未打补丁时正常导入，证明补丁本身不破坏包结构。"""
    zip_path = tmp_path / "ok.zip"
    zip_path.write_bytes(_build_zip_bytes())

    result = await manager.handle_skills_import_upload({"path": str(zip_path), "overwrite": False})

    assert result["success"] is True
    assert (manager._skills_dir / "nul-demo" / "SKILL.md").is_file()


def test_safe_extract_zip_members_into_rejects_nul(tmp_path: Path) -> None:
    """单元级：_safe_extract_zip_members_into 对 NUL 原名直接抛 RuntimeError。"""
    raw = _patched(_build_zip_bytes())
    dest = tmp_path / "extract"
    dest.mkdir()
    with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
        # 展示名已被 CPython 截断为 'ev'；防御须看 orig_filename 才能命中
        names = [info.filename for info in zf.infolist()]
        assert "ev\x00l.md" not in names
        with pytest.raises(RuntimeError, match="非法文件名"):
            SkillManager._safe_extract_zip_members_into(zf, dest.resolve())
