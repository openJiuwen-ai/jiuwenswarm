# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P8.2 directory-level cli.js fix DOM corruption guard — issue 4162."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen as ppt_page_gen
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    PPTPageGenNode,
    QAFixNode,
    _is_slide_exportable,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils import (
    BashResult,
)

_VALID_HTML = (
    "<!DOCTYPE html><html><body>"
    '<div class="ppt-slide h-[720px]">'
    '<main class="flex-1"><section>content</section></main>'
    "</div></body></html>"
)

# 目录级 fix 重排标签后 <main> 滑出 .ppt-slide 导出边界
_CORRUPTED_HTML = (
    "<!DOCTYPE html><html><body>"
    '<main class="flex-1"><section>content</section></main>'
    '<div class="ppt-slide h-[720px]"><section>content</section></div>'
    "</body></html>"
)


class _FakeQaNode:
    """提供 QAFixNode 所需的 list_dir / read_file / write_file / glob 工具。"""

    def __init__(self, pages_dir: Path) -> None:
        self._pages_dir = pages_dir
        self.writes: list[str] = []

    def has_tool(self, name: str) -> bool:
        return name in {"bash", "list_dir", "read_file", "write_file", "glob"}

    async def call_tool(self, name: str, **kwargs: Any) -> Any:
        if name == "list_dir":
            files = sorted(p.name for p in self._pages_dir.glob("page-*.pptx.html"))
            return {"filenames": files}
        if name == "read_file":
            p = Path(kwargs["file_path"])
            if not p.is_file():
                return {"content": ""}
            return {"content": p.read_text(encoding="utf-8")}
        if name == "write_file":
            self.writes.append(kwargs["file_path"])
            p = Path(kwargs["file_path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(kwargs["content"], encoding="utf-8")
            return {"ok": True}
        if name == "glob":
            matches = sorted(str(p) for p in self._pages_dir.glob(kwargs["pattern"]))
            return {"matching_files": matches, "filenames": matches}
        raise AssertionError(f"unexpected tool {name}")


@pytest.mark.asyncio
async def test_directory_fix_dom_corruption_restored_from_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证 目录级 cli.js fix 破坏页面导出 DOM 后 自动回退备份且 reconcile 不再判缺失。"""
    pptx_root = tmp_path / "pptx-craft"
    cli = pptx_root / "packages" / "cli" / "dist" / "cli.js"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("// stub", encoding="utf-8")

    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    content_page_count = 3
    total_pages = content_page_count + 2
    for i in range(1, total_pages + 1):
        (pages_dir / f"page-{i}.pptx.html").write_text(_VALID_HTML, encoding="utf-8")

    async def _fake_run_bash(node: Any, command: str, **kwargs: Any) -> BashResult:
        backup_dir = pages_dir / "_backup" / "20260911064514"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, total_pages + 1):
            target = pages_dir / f"page-{i}.pptx.html"
            (backup_dir / target.name).write_text(
                target.read_text(encoding="utf-8"), encoding="utf-8"
            )
        (pages_dir / "page-2.pptx.html").write_text(_CORRUPTED_HTML, encoding="utf-8")
        return BashResult(exit_code=0, stdout="已保存修复后的文件", stderr="", raw="")

    monkeypatch.setattr(ppt_page_gen, "run_bash", _fake_run_bash)

    qa = QAFixNode()
    fake = _FakeQaNode(pages_dir)
    qa.has_tool = fake.has_tool  # type: ignore[method-assign]
    qa.call_tool = fake.call_tool  # type: ignore[method-assign]
    result = await qa._execute(
        {
            "pages_dir": str(pages_dir),
            "page_count": content_page_count,
            "total_pages": total_pages,
            "pptx_root": str(pptx_root),
            "style_file_path": str(tmp_path / "style.md"),
        }
    )

    restored = (pages_dir / "page-2.pptx.html").read_text(encoding="utf-8")
    assert _is_slide_exportable(restored) is True
    assert str(pages_dir / "page-2.pptx.html") in [str(Path(w)) for w in fake.writes]
    assert result["qa_status"] == "ok"
    assert "dom_restored_from_backup=[2]" in result["fix_report"]

    gen = PPTPageGenNode()

    async def _read_file(path: str) -> str:
        p = Path(path)
        if not p.is_file():
            return ""
        return p.read_text(encoding="utf-8")

    gen._read_file = _read_file  # type: ignore[method-assign]
    missing, files = await gen._reconcile_missing_pages(
        pages_dir=str(pages_dir),
        total_pages=total_pages,
        reported_missing=[],
        reported_page_files=[f"page-{i}.pptx.html" for i in range(1, total_pages + 1)],
    )
    assert missing == []
    assert files == [f"page-{i}.pptx.html" for i in range(1, total_pages + 1)]


@pytest.mark.asyncio
async def test_directory_fix_keeps_healthy_pages_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证 目录级 cli.js fix 未破坏页面时不触发任何回退写盘。"""
    pptx_root = tmp_path / "pptx-craft"
    cli = pptx_root / "packages" / "cli" / "dist" / "cli.js"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("// stub", encoding="utf-8")

    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    total_pages = 4
    for i in range(1, total_pages + 1):
        (pages_dir / f"page-{i}.pptx.html").write_text(_VALID_HTML, encoding="utf-8")

    async def _fake_run_bash(node: Any, command: str, **kwargs: Any) -> BashResult:
        backup_dir = pages_dir / "_backup" / "20260911064515"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, total_pages + 1):
            target = pages_dir / f"page-{i}.pptx.html"
            (backup_dir / target.name).write_text(
                target.read_text(encoding="utf-8"), encoding="utf-8"
            )
        return BashResult(exit_code=0, stdout="已保存修复后的文件", stderr="", raw="")

    monkeypatch.setattr(ppt_page_gen, "run_bash", _fake_run_bash)

    qa = QAFixNode()
    fake = _FakeQaNode(pages_dir)
    qa.has_tool = fake.has_tool  # type: ignore[method-assign]
    qa.call_tool = fake.call_tool  # type: ignore[method-assign]
    result = await qa._execute(
        {
            "pages_dir": str(pages_dir),
            "page_count": total_pages - 2,
            "total_pages": total_pages,
            "pptx_root": str(pptx_root),
            "style_file_path": str(tmp_path / "style.md"),
        }
    )

    assert result["qa_status"] == "ok"
    assert fake.writes == []
    assert "dom_restored_from_backup" not in result["fix_report"]
    for i in range(1, total_pages + 1):
        assert (
            _is_slide_exportable(
                (pages_dir / f"page-{i}.pptx.html").read_text(encoding="utf-8")
            )
            is True
        )


@pytest.mark.asyncio
async def test_directory_fix_read_failure_not_treated_as_dom_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证 fix 后读失败（空串）页不误回退 backup，仅真 DOM 破坏页才回退。"""
    pptx_root = tmp_path / "pptx-craft"
    cli = pptx_root / "packages" / "cli" / "dist" / "cli.js"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("// stub", encoding="utf-8")

    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    total_pages = 3
    for i in range(1, total_pages + 1):
        (pages_dir / f"page-{i}.pptx.html").write_text(_VALID_HTML, encoding="utf-8")

    async def _fake_run_bash(node: Any, command: str, **kwargs: Any) -> BashResult:
        backup_dir = pages_dir / "_backup" / "20260911064516"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, total_pages + 1):
            target = pages_dir / f"page-{i}.pptx.html"
            (backup_dir / target.name).write_text(
                target.read_text(encoding="utf-8"), encoding="utf-8"
            )
        (pages_dir / "page-2.pptx.html").write_text(_CORRUPTED_HTML, encoding="utf-8")
        return BashResult(exit_code=0, stdout="已保存修复后的文件", stderr="", raw="")

    monkeypatch.setattr(ppt_page_gen, "run_bash", _fake_run_bash)

    qa = QAFixNode()
    fake = _FakeQaNode(pages_dir)
    qa.has_tool = fake.has_tool  # type: ignore[method-assign]
    real_call_tool = fake.call_tool

    async def _call_tool(name: str, **kwargs: Any) -> Any:
        if name == "read_file" and str(kwargs.get("file_path", "")).endswith(
            "page-3.pptx.html"
        ):
            return {"content": ""}
        return await real_call_tool(name, **kwargs)

    qa.call_tool = _call_tool  # type: ignore[method-assign]
    result = await qa._execute(
        {
            "pages_dir": str(pages_dir),
            "page_count": total_pages - 2,
            "total_pages": total_pages,
            "pptx_root": str(pptx_root),
            "style_file_path": str(tmp_path / "style.md"),
        }
    )

    page3_path = str(pages_dir / "page-3.pptx.html")
    assert page3_path not in [str(Path(w)) for w in fake.writes]
    assert (
        _is_slide_exportable(
            (pages_dir / "page-3.pptx.html").read_text(encoding="utf-8")
        )
        is True
    )
    assert str(pages_dir / "page-2.pptx.html") in [str(Path(w)) for w in fake.writes]
    assert "dom_restored_from_backup=[2]" in result["fix_report"]
