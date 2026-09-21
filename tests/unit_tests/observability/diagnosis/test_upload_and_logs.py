# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""G0 日志 request_id 过滤 + G5 上传日志收集单测。"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.observability.diagnosis import DiagnosisContext
from jiuwenswarm.observability.diagnosis.evidence import (
    SpanRecord,
    collect_log_excerpts,
)
from jiuwenswarm.observability.diagnosis.upload import (
    UploadValidationError,
    save_uploaded_logs,
)


# ---------------------------------------------------------------------------
# G0: 日志按 request_id / session_id 锚点过滤
# ---------------------------------------------------------------------------


def _ctx_with_records(records, **kw) -> DiagnosisContext:
    defaults = dict(
        session_id="sess-1",
        trace_id="trace-1",
        user_note=None,
        requested_mode=None,
        records=records,
    )
    defaults.update(kw)
    return DiagnosisContext(**defaults)


def test_collect_log_excerpts_filters_by_session_id(tmp_path: Path) -> None:
    log_file = tmp_path / "agent_server.log"
    log_file.write_text(
        "line1 unrelated\n"
        "line2 session=sess-1 doing something\n"
        "line3 session=other-sess ignore\n"
        "line4 ERROR boom (session=sess-1)\n",
        encoding="utf-8",
    )
    records = [SpanRecord("trace-1", "req-1", 1_000_000_000, 2_000_000_000, False, "ok", [])]
    ctx = _ctx_with_records(records, log_dir=tmp_path)
    from jiuwenswarm.observability.diagnosis import FailureWindow

    excerpts = collect_log_excerpts(ctx, FailureWindow(0, 0))
    matched_lines = [e["line"] for e in excerpts]
    # 命中 session_id 锚点 + ERROR 级
    assert any("sess-1" in l and "doing" in l for l in matched_lines)
    assert any("ERROR boom" in l for l in matched_lines)
    # 不含 other-sess 的无关行（除非它含 ERROR，line3 没有）
    assert not any("other-sess ignore" in l for l in matched_lines)


def test_collect_log_excerpts_catches_traceback(tmp_path: Path) -> None:
    log_file = tmp_path / "gateway.log"
    log_file.write_text(
        "normal line\n"
        "Traceback (most recent call last):\n"
        "  File 'x.py', line 10, in foo\n"
        "ValueError: bad\n",
        encoding="utf-8",
    )
    records = [SpanRecord("trace-1", "req-1", 1, 2, False, "ok", [])]
    ctx = _ctx_with_records(records, log_dir=tmp_path)
    from jiuwenswarm.observability.diagnosis import FailureWindow

    excerpts = collect_log_excerpts(ctx, FailureWindow(0, 0))
    matched = [e["line"] for e in excerpts]
    # Traceback header 命中（后续栈帧行不带 ERROR token，按行收集不抓整块）
    assert any("Traceback" in l for l in matched)


# ---------------------------------------------------------------------------
# G5a: 上传日志锚点零命中降级
# ---------------------------------------------------------------------------


def test_collect_uploaded_logs_degrades_on_anchor_miss(tmp_path: Path) -> None:
    # 上传日志不含 session_id/request_id 锚点，只有 ERROR/Traceback
    upload = tmp_path / "remote.log"
    upload.write_text(
        "some line\n"
        "ERROR: something broke\n"
        "Traceback (most recent call last):\n"
        "  boom\n",
        encoding="utf-8",
    )
    records = [SpanRecord("trace-1", "req-1", 1, 2, False, "ok", [])]
    ctx = _ctx_with_records(records, uploaded_logs=[upload])
    from jiuwenswarm.observability.diagnosis import FailureWindow

    excerpts = collect_log_excerpts(ctx, FailureWindow(0, 0))
    # 首条应为降级提示
    assert excerpts[0]["matched_key"] == "degraded:no_anchor"
    assert "锚点匹配失败" in excerpts[0]["line"]
    # ERROR/Traceback 行被无锚点提取（每条只收一次）
    matched_lines = [e["line"] for e in excerpts[1:]]
    assert matched_lines.count("ERROR: something broke") == 1
    assert matched_lines.count("Traceback (most recent call last):") == 1
    # 均标注为降级
    for e in excerpts[1:]:
        assert e["matched_key"] == "degraded:no_anchor"


def test_collect_uploaded_logs_replaces_local(tmp_path: Path) -> None:
    """上传日志非空时替换本地日志收集（不合并）。"""
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "agent_server.log").write_text("ERROR local\n", encoding="utf-8")

    upload = tmp_path / "remote.log"
    upload.write_text("ERROR remote session=sess-1\n", encoding="utf-8")

    records = [SpanRecord("trace-1", "req-1", 1, 2, False, "ok", [])]
    ctx = _ctx_with_records(
        records,
        log_dir=local_dir,
        uploaded_logs=[upload],
    )
    from jiuwenswarm.observability.diagnosis import FailureWindow

    excerpts = collect_log_excerpts(ctx, FailureWindow(0, 0))
    sources = {e["source"] for e in excerpts}
    # 只含 uploaded 来源，不含 local
    assert all(s.startswith("uploaded") for s in sources)
    assert not any(s == "local" for s in sources)


def test_collect_uploaded_logs_anchor_miss_no_duplicate(tmp_path: Path) -> None:
    """锚点零命中时 ERROR/Traceback 不重复收集（回归：曾双收）。"""
    upload = tmp_path / "remote.log"
    upload.write_text(
        "some line\n"
        "ERROR: something broke\n"
        "Traceback (most recent call last):\n"
        "  boom\n",
        encoding="utf-8",
    )
    records = [SpanRecord("trace-1", "req-1", 1, 2, False, "ok", [])]
    ctx = _ctx_with_records(records, uploaded_logs=[upload])
    from jiuwenswarm.observability.diagnosis import FailureWindow

    excerpts = collect_log_excerpts(ctx, FailureWindow(0, 0))
    # 首条降级提示
    assert excerpts[0]["matched_key"] == "degraded:no_anchor"
    # 非提示证据中，每条 ERROR/Traceback 行只出现一次
    evidence_lines = [e["line"] for e in excerpts[1:]]
    assert evidence_lines.count("ERROR: something broke") == 1
    assert evidence_lines.count("Traceback (most recent call last):") == 1
    # 锚点零命中的 ERROR/Traceback 标注为 degraded
    for e in excerpts[1:]:
        assert e["matched_key"] == "degraded:no_anchor"


def test_collect_uploaded_logs_gbk_fallback(tmp_path: Path) -> None:
    """UTF-8 失败则 GBK fallback（Windows 日志大概率 GBK）。"""
    upload = tmp_path / "win.log"
    # 写一个 UTF-8 解不开、GBK 可解的内容
    content = "ERROR 会话错误\n".encode("gbk")
    upload.write_bytes(content)

    records = [SpanRecord("trace-1", "req-1", 1, 2, False, "ok", [])]
    ctx = _ctx_with_records(records, uploaded_logs=[upload])
    from jiuwenswarm.observability.diagnosis import FailureWindow

    excerpts = collect_log_excerpts(ctx, FailureWindow(0, 0))
    # GBK 内容能被读出（无锚点 → 标注为降级，但行内容仍在）
    lines = [e["line"] for e in excerpts]
    assert any("会话错误" in l for l in lines)


# ---------------------------------------------------------------------------
# G5b: 上传文件校验与落盘
# ---------------------------------------------------------------------------


class _FakeUpload:
    """模拟 Starlette UploadFile：read 是 async 协程（与真实 API 对齐）。"""

    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content
        self._pos = 0

    async def read(self, size: int = -1) -> bytes:
        if size <= 0:
            data = self._content[self._pos:]
            self._pos = len(self._content)
            return data
        data = self._content[self._pos:self._pos + size]
        self._pos += len(data)
        return data


async def test_save_uploaded_logs_rejects_wrong_type(tmp_path: Path) -> None:
    upload = _FakeUpload("evil.exe", b"x")
    with pytest.raises(UploadValidationError, match="不支持的文件类型"):
        await save_uploaded_logs([upload], "sess-1", tmp_path, max_mb=1)


async def test_save_uploaded_logs_rejects_oversize(tmp_path: Path) -> None:
    upload = _FakeUpload("big.log", b"x" * (2 * 1024 * 1024))  # 2MB > 1MB 上限
    with pytest.raises(UploadValidationError, match="超过大小上限"):
        await save_uploaded_logs([upload], "sess-1", tmp_path, max_mb=1)


async def test_save_uploaded_logs_unzips_zip(tmp_path: Path) -> None:
    # 构造含两个 .log 的 zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.log", "ERROR in a\n")
        zf.writestr("b.log", "Traceback (most recent call last):\n")
        zf.writestr("skip.exe", "ignored")  # 非白名单跳过
    upload = _FakeUpload("logs.zip", buf.getvalue())

    saved = await save_uploaded_logs([upload], "sess-1", tmp_path, max_mb=10)
    names = {p.name for p in saved}
    assert names == {"a.log", "b.log"}
    # zip 原文件已删除
    assert not (tmp_path / "sess-1" / "uploads" / "logs.zip").exists()


async def test_save_uploaded_logs_rejects_when_not_allowed(tmp_path: Path) -> None:
    upload = _FakeUpload("a.log", b"x")
    with pytest.raises(UploadValidationError, match="allow_log_upload=false"):
        await save_uploaded_logs([upload], "sess-1", tmp_path, max_mb=10, allow=False)
