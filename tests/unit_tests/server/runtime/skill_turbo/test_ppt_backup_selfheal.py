# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests: 畸形开标签写盘拦截 + fix 后缺页备份自愈（三道防线）。

背景（"46.PPT任务html生成成但最后PPT生成失败"案例）：页面写入时产生畸形开
标签（class 属性值后缺失 ``">``），pptx-craft fix 的 htmlparser2 解析器把后续
标签吞入属性值、并在错误解析树上补/删闭合标签，导致 div 配对失衡 → P8 判定
不可导出 → P9 拒绝导出 → 交付失败。

红线（本文件逐条锁定）：
- 写盘校验 _validate_slide_dom 必须拦截根因形态（畸形但 div 平衡、
  _is_slide_exportable 仍为 True 的页面）；
- 恢复资格与写盘校验同口径：含畸形开标签的备份不得被恢复（宁可保持缺页、
  由 P9 拒绝导出，也不静默写回视觉残缺/含毒页面）；
- AbortError（HITL 中断）必须透传；正常路径（missing 为空）零工具调用；
- P9 缺页门：无备份可恢复 → 拒绝导出且不触发 convert；可恢复 → 越过缺页门。
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_export import (
    PPTExportNode,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    QAFixNode,
    _find_latest_backup_page_path,
    _has_malformed_open_tag,
    _is_slide_exportable,
    _validate_slide_dom,
    restore_unexportable_pages_from_backup,
)

# ─────────────────────────── 测试样本 ───────────────────────────
# 干净页面：写盘校验与导出校验均通过（备份的合格形态）。
CLEAN_PAGE = (
    '<!DOCTYPE html><html><head><title>p</title></head><body>'
    '<div class="ppt-slide w-[1280px] h-[720px]">'
    '<main class="flex-1"><p class="text-[12px] leading-[1.35]">时刻为时间轴上一点</p></main>'
    '</div></body></html>'
)

# 事故根因形态（fix 前）：class 属性值后缺失 '">，引号吞入后续 </p>。
# div 配对未破坏、main 仍在 slide 内 → 旧导出口径 _is_slide_exportable=True，
# 但写盘口径 _validate_slide_dom=False。
MALFORMED_BUT_EXPORTABLE = (
    '<!DOCTYPE html><html><head><title>p</title></head><body>'
    '<div class="ppt-slide w-[1280px] h-[720px]">'
    '<main class="flex-1">标题</main>'
    '<p class="text-[12px] leading-[1.35]时刻为时间轴上一点，间隔为一段线段</p>'
    '</div></body></html>'
)

# fix 误修形态（fix 后）：ppt-slide 容器闭合丢失 → 不可导出（reconcile 判缺页）。
CORRUPTED_PAGE = (
    '<!DOCTYPE html><html><head><title>p</title></head><body>'
    '<div class="ppt-slide w-[1280px] h-[720px]">'
    '<main class="flex-1">标题</main>'
    '</body></html>'
)

BACKUP_TS = "20260914122050"
BACKUP_TS_OLD = "20260914110000"


class _ToolOutput:
    """模拟生产 ToolOutput（pydantic BaseModel）：str() 的 repr 含字段值。

    生产证据（jiuwen.log GlobTool_skill_turbo）：glob 结果的
    str(result) 含 data={'filenames': [...], 'matching_files': [...]}，
    _find_latest_backup_page_path 靠对 str(result) 做正则提取时间戳。
    """

    def __init__(self, success: bool = True, data: Any = None) -> None:
        self.success = success
        self.data = data

    def __repr__(self) -> str:
        return f"success={self.success} data={self.data!r} error=None"


class _FsBackedNode:
    """生产形态工具桩：glob/read_file/write_file/bash，真实落盘。

    - glob      → ToolOutput(data={'filenames': [相对路径], 'matching_files': [绝对路径]})
    - read_file → ToolOutput(data={'content': 'cat -n 编号内容'})（校验去行号链路）
    - write_file→ ToolOutput(data={'content': ''})，真实写盘
    - bash      → 记录命令；statSync 探测返回 20480 字节，其余 exit 0
    """

    def __init__(
        self,
        *,
        tools: set[str] | None = None,
        abort_read_paths: set[str] | None = None,
        fail_read_paths: set[str] | None = None,
    ) -> None:
        self.tools = tools if tools is not None else {
            "glob", "read_file", "write_file", "bash",
        }
        self.abort_read_paths = set(abort_read_paths or ())
        self.fail_read_paths = set(fail_read_paths or ())
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def has_tool(self, name: str) -> bool:
        return name in self.tools

    async def call_tool(self, name: str, **kwargs: Any) -> Any:
        self.calls.append((name, dict(kwargs)))
        if name == "glob":
            root = Path(kwargs["path"])
            rel_hits = [
                rel.replace("\\", "/")
                for rel in (
                    str(p.relative_to(root)).replace("\\", "/")
                    for p in root.rglob("*")
                )
                if fnmatch.fnmatch(rel, kwargs["pattern"])
            ]
            return _ToolOutput(data={
                "filenames": rel_hits,
                "matching_files": [str(root / h) for h in rel_hits],
                "count": len(rel_hits),
            })
        if name == "read_file":
            path = kwargs["file_path"]
            if path in self.abort_read_paths:
                raise AbortError("user interrupt")
            if path in self.fail_read_paths:
                raise RuntimeError("Cannot operate on a closed database")
            text = Path(path).read_text(encoding="utf-8")
            numbered = "\n".join(
                f"{i + 1:>6}\t{line}" for i, line in enumerate(text.splitlines())
            )
            return _ToolOutput(data={"content": numbered})
        if name == "write_file":
            Path(kwargs["file_path"]).write_text(
                kwargs["content"], encoding="utf-8"
            )
            return _ToolOutput(data={"content": ""})
        if name == "bash":
            command = str(kwargs.get("command") or "")
            stdout = "20480" if "statSync" in command else ""
            return _ToolOutput(data={"exit_code": 0, "stdout": stdout})
        raise ValueError(name)

    def bash_commands(self) -> list[str]:
        return [kwargs.get("command", "") for n, kwargs in self.calls if n == "bash"]


def _make_pages(tmp_path: Path, *, backup_html: str | None = CLEAN_PAGE) -> Path:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page-3.pptx.html").write_text(CORRUPTED_PAGE, encoding="utf-8")
    if backup_html is not None:
        backup_dir = pages / "_backup" / BACKUP_TS
        backup_dir.mkdir(parents=True)
        (backup_dir / "page-3.pptx.html").write_text(backup_html, encoding="utf-8")
    return pages


# ─────────────────── 防线A：畸形开标签写盘拦截 ───────────────────


def test_has_malformed_open_tag_detects_incident_pattern():
    # 事故根因形态：属性引号吞入后续 </p>；以及缺闭合 > 的开标签
    assert _has_malformed_open_tag(MALFORMED_BUT_EXPORTABLE) is True
    assert _has_malformed_open_tag('<p class="x leading-[1.35]一段中文</p>') is True
    assert _has_malformed_open_tag('<div class="a') is True
    # 引号吞入下一个标签开头（跨标签吞没）
    assert _has_malformed_open_tag('<div class="outer<span class="x">y</span>') is True


def test_has_malformed_open_tag_allows_legitimate_constructs():
    samples = {
        "clean page": CLEAN_PAGE,
        "multiline attr": '<div\n  class="a\n  b">x</div>',
        "single quote": "<div class='a'>x</div>",
        "fullwidth lt in attr": '<p title="a＜b">x</p>',
        "text lt not in tag": "<div>a < b && c > d</div>",
        "echarts formatter": (
            "<script>var o = {formatter: function(p){"
            "return '<div style=\"text-align:center\">' + p.value + '</div>'"
            "}};</script>"
        ),
        "comment": "<!-- <div class=\"x\"> commented -->",
        "self-closing": '<img src="a.png" />',
        "svg": '<svg viewBox="0 0 10 10"><path d="M0 0"/></svg>',
        "unquoted attr": "<div class=a>x</div>",
    }
    for name, html in samples.items():
        assert _has_malformed_open_tag(html) is False, name


def test_validate_slide_dom_rejects_incident_malformed_page():
    # 锁住两口径差异：导出口径看不出畸形（这正是事故漏放行的原因），
    # 写盘口径必须拦截。
    assert _is_slide_exportable(MALFORMED_BUT_EXPORTABLE) is True
    assert _validate_slide_dom(MALFORMED_BUT_EXPORTABLE) is False
    assert _validate_slide_dom(CLEAN_PAGE) is True
    assert _is_slide_exportable(CORRUPTED_PAGE) is False


# ─────────────── 防线B/C：P8 缺页备份自愈（恢复资格=写盘口径） ───────────────


@pytest.mark.asyncio
async def test_restore_recovers_corrupted_page_from_clean_backup(tmp_path):
    pages = _make_pages(tmp_path, backup_html=CLEAN_PAGE)
    node = _FsBackedNode()
    recovered = await restore_unexportable_pages_from_backup(
        node, str(pages), [3], log_prefix="[T]"
    )
    assert recovered == [3]
    # 恢复后磁盘内容与备份字节级一致，且可导出
    assert (pages / "page-3.pptx.html").read_text(encoding="utf-8") == CLEAN_PAGE
    assert _is_slide_exportable(
        (pages / "page-3.pptx.html").read_text(encoding="utf-8")
    )


@pytest.mark.asyncio
async def test_restore_refuses_backup_with_malformed_tag(tmp_path):
    # 评审负向用例：备份本身含根因畸形标签（畸形但 _is_slide_exportable=True）。
    # 恢复资格与写盘校验同口径 → 拒收：宁可保持缺页由 P9 拒绝导出，
    # 也不静默写回视觉残缺/含毒页面。
    pages = _make_pages(tmp_path, backup_html=MALFORMED_BUT_EXPORTABLE)
    node = _FsBackedNode()
    recovered = await restore_unexportable_pages_from_backup(
        node, str(pages), [3], log_prefix="[T]"
    )
    assert recovered == []
    # 磁盘未被覆盖，仍是 fix 后的损坏内容（未被畸形备份污染）
    assert (pages / "page-3.pptx.html").read_text(encoding="utf-8") == CORRUPTED_PAGE


@pytest.mark.asyncio
async def test_restore_skips_when_no_backup(tmp_path):
    pages = _make_pages(tmp_path, backup_html=None)
    node = _FsBackedNode()
    recovered = await restore_unexportable_pages_from_backup(node, str(pages), [3])
    assert recovered == []
    assert (pages / "page-3.pptx.html").read_text(encoding="utf-8") == CORRUPTED_PAGE


@pytest.mark.asyncio
async def test_restore_propagates_abort_error(tmp_path):
    pages = _make_pages(tmp_path)
    # 恢复函数经 _find_latest_backup_page_path 重建的路径为正斜杠形态
    backup_path = f"{pages}/_backup/{BACKUP_TS}/page-3.pptx.html"
    node = _FsBackedNode(abort_read_paths={backup_path})
    with pytest.raises(AbortError):
        await restore_unexportable_pages_from_backup(node, str(pages), [3])


@pytest.mark.asyncio
async def test_restore_skips_page_on_persistent_read_failure(tmp_path):
    pages = _make_pages(tmp_path)
    backup_path = f"{pages}/_backup/{BACKUP_TS}/page-3.pptx.html"
    node = _FsBackedNode(fail_read_paths={backup_path})
    recovered = await restore_unexportable_pages_from_backup(node, str(pages), [3])
    assert recovered == []
    assert (pages / "page-3.pptx.html").read_text(encoding="utf-8") == CORRUPTED_PAGE


@pytest.mark.asyncio
async def test_restore_empty_missing_makes_no_tool_calls(tmp_path):
    node = _FsBackedNode()
    recovered = await restore_unexportable_pages_from_backup(
        node, str(tmp_path), [], log_prefix="[T]"
    )
    assert recovered == []
    assert node.calls == []  # 正常路径零开销


@pytest.mark.asyncio
async def test_restore_prefers_latest_backup(tmp_path):
    pages = _make_pages(tmp_path, backup_html=CLEAN_PAGE)
    old_dir = pages / "_backup" / BACKUP_TS_OLD
    old_dir.mkdir(parents=True)
    (old_dir / "page-3.pptx.html").write_text(MALFORMED_BUT_EXPORTABLE, encoding="utf-8")

    node = _FsBackedNode()
    found = await _find_latest_backup_page_path(node, str(pages), 3)
    assert BACKUP_TS in found and BACKUP_TS_OLD not in found
    # 最新备份干净 → 恢复成功
    recovered = await restore_unexportable_pages_from_backup(node, str(pages), [3])
    assert recovered == [3]


@pytest.mark.asyncio
async def test_qafix_find_latest_backup_path_delegation(tmp_path):
    pages = _make_pages(tmp_path)
    node = _FsBackedNode()
    qa = QAFixNode()
    qa.set_runtime_callbacks(has_tool=node.has_tool, use_tool=node.call_tool)
    found = await qa._find_latest_backup_path(str(pages), 3)
    assert BACKUP_TS in found


# ─────────────── 防线D：P9 缺页门（最后防线） ───────────────


def _make_p9_env(tmp_path: Path) -> tuple[str, str, str, dict[str, Any]]:
    pages = _make_pages(tmp_path)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    # cli_path 要求 {pptx_root}/packages/cli/dist/cli.js 真实存在
    pptx_root = tmp_path / "pptx_root"
    cli_js = pptx_root / "packages" / "cli" / "dist" / "cli.js"
    cli_js.parent.mkdir(parents=True)
    cli_js.write_text("// stub", encoding="utf-8")
    inputs = {
        "output_dir": str(output_dir),
        "pages_dir": str(pages),
        "topic": "备份自愈测试",
        "pptx_root": str(pptx_root),
        "missing_pages": [3],
        "layout_warning_pages": [],
        "ppt_gen_status": "partial",
        "style_mode": "",
    }
    return str(pages), str(output_dir), str(pptx_root), inputs


def _wire(node: _FsBackedNode, plan_node: PPTExportNode) -> None:
    plan_node.set_runtime_callbacks(
        has_tool=node.has_tool, use_tool=node.call_tool
    )


@pytest.mark.asyncio
async def test_p9_missing_pages_gate_refuses_without_backup(tmp_path):
    pages = _make_pages(tmp_path, backup_html=None)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    pptx_root = tmp_path / "pptx_root"
    cli_js = pptx_root / "packages" / "cli" / "dist" / "cli.js"
    cli_js.parent.mkdir(parents=True)
    cli_js.write_text("// stub", encoding="utf-8")

    node = _FsBackedNode()
    p9 = PPTExportNode()
    _wire(node, p9)
    result = await p9._execute({
        "output_dir": str(output_dir),
        "pages_dir": str(pages),
        "topic": "备份自愈测试",
        "pptx_root": str(pptx_root),
        "missing_pages": [3],
        "ppt_gen_status": "partial",
        "style_mode": "",
    })
    # 无备份可恢复 → 拒绝导出，且不触发 convert（零 bash 调用）
    assert result["export_status"] == "failed"
    assert result["validate_pptx_status"] == "skipped"
    assert node.bash_commands() == []


@pytest.mark.asyncio
async def test_p9_attempts_backup_restore_then_proceeds_to_convert(tmp_path):
    _, _, _, inputs = _make_p9_env(tmp_path)
    node = _FsBackedNode()
    p9 = PPTExportNode()
    _wire(node, p9)
    result = await p9._execute(inputs)

    # 备份恢复成功 → 越过缺页门：convert/validate 均被触发且导出成功
    assert node.bash_commands(), "P9 应在恢复后进入 convert，触发 bash 调用"
    assert any("convert" in cmd for cmd in node.bash_commands())
    assert result["export_status"] == "ok"
    # 恢复后的页面确实来自备份
    pages_dir = Path(inputs["pages_dir"])
    assert (pages_dir / "page-3.pptx.html").read_text(encoding="utf-8") == CLEAN_PAGE


@pytest.mark.asyncio
async def test_p9_refuses_when_backup_malformed(tmp_path):
    # 备份含畸形标签 → P9 恢复拒收 → 缺页仍在 → 拒绝导出（不静默覆盖）
    pages = _make_pages(tmp_path, backup_html=MALFORMED_BUT_EXPORTABLE)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    pptx_root = tmp_path / "pptx_root"
    cli_js = pptx_root / "packages" / "cli" / "dist" / "cli.js"
    cli_js.parent.mkdir(parents=True)
    cli_js.write_text("// stub", encoding="utf-8")

    node = _FsBackedNode()
    p9 = PPTExportNode()
    _wire(node, p9)
    result = await p9._execute({
        "output_dir": str(output_dir),
        "pages_dir": str(pages),
        "topic": "备份自愈测试",
        "pptx_root": str(pptx_root),
        "missing_pages": [3],
        "ppt_gen_status": "partial",
        "style_mode": "",
    })
    assert result["export_status"] == "failed"
    assert node.bash_commands() == []
    assert (pages / "page-3.pptx.html").read_text(encoding="utf-8") == CORRUPTED_PAGE
