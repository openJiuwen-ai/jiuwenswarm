"""Skill HTTPS 直链归档下载与按 SKILL.md name 落盘."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def _build_skill_zip(dest: Path, *, skill_name: str = "archive-demo-skill") -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "SKILL.md",
            f"---\nname: {skill_name}\ndescription: demo\nversion: 1.0.0\n---\n\nbody\n",
        )
        zf.writestr("workflow.md", "# workflow\n")
    dest.write_bytes(buf.getvalue())
    return dest


def test_skillnet_install_http_zip_renames_by_skill_md_name(tmp_path: Path, monkeypatch) -> None:
    zip_path = tmp_path / "api-design-review-team_1.0.0.zip"
    _build_skill_zip(zip_path, skill_name="api-design-review-team")
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))

    monkeypatch.setattr(manager, "_assert_skill_download_url_allowed", lambda _url: None)
    monkeypatch.setattr(
        manager,
        "_download_http_archive_bytes_sync",
        lambda _url: zip_path.read_bytes(),
    )

    result = manager.install_skill_sync(
        "https://demo-bucket.obs.cn-north-4.myhuaweicloud.com/skills/pkg.zip",
        force=True,
    )

    assert result.get("ok") is True
    assert result.get("skill_name") == "api-design-review-team"
    installed = tmp_path / "workspace" / "skills" / "api-design-review-team" / "SKILL.md"
    assert installed.is_file()
