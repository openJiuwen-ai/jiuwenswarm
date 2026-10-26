# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import warnings

warnings.filterwarnings(
    "ignore",
    message=r"Protobuf gencode version.*",
)

import os
import tempfile
import time

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    _ARTIFACT_SCAN_MAX_TEXT_BYTES,
    READONLY_INNER_TOOLS,
    _extract_artifact_paths_from_result,
    _extract_file_paths_from_write_tool,
    _extract_image_paths_from_tool_result,
    detect_artifact_paths,
)


def test_write_tool_prefers_path_from_tool_args() -> None:
    paths = _extract_file_paths_from_write_tool(
        "write_file",
        {"path": "/tmp/demo.py"},
        "Wrote /tmp/other.py",
    )
    assert paths == ["/tmp/demo.py"]


def test_write_tool_fallback_extracts_py_from_result_text() -> None:
    paths = _extract_file_paths_from_write_tool(
        "write_file",
        {},
        "Successfully wrote /workspace/out/demo.py",
    )
    assert paths == ["/workspace/out/demo.py"]


def test_write_tool_fallback_ignores_non_python_paths() -> None:
    paths = _extract_file_paths_from_write_tool(
        "write_file",
        {},
        "Saved /workspace/out/chart.png and /workspace/out/notes.txt",
    )
    assert paths == []


def test_image_path_extraction_keeps_image_whitelist() -> None:
    # generate_image 真实输出格式：每行一个路径，可能含空格（effective_project_dir）
    paths = _extract_image_paths_from_tool_result(
        "Generated 1 image(s) successfully!\n"
        "Local file paths (use for attachments or send_file):\n"
        "- E:\\01 code\\proj\\generated_images\\chart.png\n"
        "- /workspace/out/demo.py\n"
        "Prompt: a chart"
    )
    assert paths == ["E:\\01 code\\proj\\generated_images\\chart.png"]


def test_weak_key_candidate_falls_back_to_body_scan(tmp_path) -> None:
    # P1-1 回归：结构化弱键（result: "ok"）校验失败后，不应屏蔽
    # stdout 正文中的真实产物路径
    base = tmp_path.resolve()
    report = base / "report.xlsx"
    report.write_bytes(b"x")
    paths = _extract_artifact_paths_from_result(
        {"stdout": f"Saved to {report}\nDone.", "result": "ok"},
        workspace_base=base,
    )
    assert paths == [str(report)]


def test_structured_hit_short_circuits_body_scan(tmp_path) -> None:
    # 结构化候选校验通过即返回，不再扫描正文（对齐 clowder-ai 语义）
    base = tmp_path.resolve()
    report = base / "report.xlsx"
    report.write_bytes(b"x")
    junk = base / "junk.txt"
    junk.write_bytes(b"x")
    paths = _extract_artifact_paths_from_result(
        {"output_path": str(report), "stdout": f"see {junk}"},
        workspace_base=base,
    )
    assert paths == [str(report)]


# evaluate_script 大文本短路：避免 800K HTML 触发 findall + 逐条 stat 阻塞（633s 根因）。


def _big_html(size: int) -> str:
    r"""构造含海量路径样子串的 HTML（href/src/D:\..\.ext 满地都是）。"""
    chunk = (
        '<a href="/api/v1/users/list.html">'
        '<img src="/static/img/foo.png" /> '
        '<script src="https://x.com/a/b/c.js"></script> '
        r'<link rel="stylesheet" href="D:\proj\dist\main.css"> '
        r'C:\Users\me\app\node_modules\pkg\index.js '
    )
    out = []
    while len("".join(out)) < size:
        out.append(chunk)
    return "".join(out)[:size]


def test_invoke_tool_evaluate_script_short_circuits_no_stat_storm() -> None:
    """evaluate_script 经 invoke_tool 间接调用且结果为大文本 HTML：
    READONLY_INNER_TOOLS 命中 → 直接返回空检测，不进正则扫描/逐条 stat。

    回归此路径会阻塞事件循环 633s（见 officeclaw 历史 633075ms AFTER_TOOL_CALL）。
    """
    assert "evaluate_script" in READONLY_INNER_TOOLS

    big_html = _big_html(800_000)
    tool_args = {"tool_name": "evaluate_script", "arguments": {"script": "x"}}
    tool_result = {
        "success": True,
        "tool_name": "evaluate_script",
        "result": big_html,
    }

    t0 = time.monotonic()
    det = detect_artifact_paths("invoke_tool", tool_args, tool_result, workspace_base=None)
    elapsed_ms = (time.monotonic() - t0) * 1000

    # 解包后内部工具名为 evaluate_script，且不产生任何产物路径
    assert det.tool_name == "evaluate_script"
    assert det.paths == []
    # 短路必须极快：旧路径 633s，修复后应在毫秒级
    assert elapsed_ms < 100, f"short-circuit too slow: {elapsed_ms:.1f}ms"


def test_oversize_result_text_skips_regex_scan() -> None:
    """即便绕过只读工具白名单（如某未知只读 MCP 工具返回超大 stdout），
    _ARTIFACT_SCAN_MAX_TEXT_BYTES 兜底也应跳过 findall，避免 stat 风暴。

    正则对 80 万字符 findall 会匹配出海量候选路径，逐条 stat() 串行阻塞。
    """
    big_text = _big_html(_ARTIFACT_SCAN_MAX_TEXT_BYTES * 8)
    assert len(big_text) > _ARTIFACT_SCAN_MAX_TEXT_BYTES

    t0 = time.monotonic()
    paths = _extract_artifact_paths_from_result(big_text, workspace_base=None)
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert paths == []
    assert elapsed_ms < 100, f"oversize guard too slow: {elapsed_ms:.1f}ms"


def test_normal_stdout_still_detects_real_artifact() -> None:
    """兜底防御不能误伤正常 code/bash stdout 产物检测：
    小文本中含真实存在的文件路径时，仍应检出。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        real_file = os.path.join(tmpdir, "report.csv")
        with open(real_file, "w", encoding="utf-8") as f:
            f.write("a,b\n1,2\n")

        stdout = f"done, output written to {real_file}\nmore lines\n"
        paths = _extract_artifact_paths_from_result(
            {"stdout": stdout}, workspace_base=None
        )

        assert any(
            os.path.normcase(real_file) == os.path.normcase(p) for p in paths
        ), f"regression: real artifact not detected: {paths}"
