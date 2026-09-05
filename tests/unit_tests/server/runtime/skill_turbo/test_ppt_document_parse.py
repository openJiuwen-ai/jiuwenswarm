from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.rails.read_file_validation import (
    is_non_text_file_path,
    validate_read_file_result,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import document_parse
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.document_parse import (
    DocumentParseNode,
    _normalize_tool_text,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_gen_root import (
    PPTGenRootNode,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import ppt_gen_root
from jiuwenswarm.server.runtime.skill_turbo.validator import PlanCodeValidator


def test_document_parse_passes_builtin_skill_validation() -> None:
    source = Path(document_parse.__file__).read_text(encoding="utf-8")
    validator = PlanCodeValidator.for_builtin_skill_code(
        ["jiuwenswarm.server.runtime.skill_turbo.skill_codes"]
    )

    assert validator.validate(source) == []


def test_ppt_gen_root_passes_builtin_skill_validation() -> None:
    source = Path(ppt_gen_root.__file__).read_text(encoding="utf-8")
    validator = PlanCodeValidator.for_builtin_skill_code(
        ["jiuwenswarm.server.runtime.skill_turbo.skill_codes"]
    )

    assert validator.validate(source) == []


def test_pdf_is_delegated_to_read_file() -> None:
    assert is_non_text_file_path("report.pdf") is False
    assert validate_read_file_result("report.pdf", "extracted PDF text") == (True, None)


def test_normalize_tool_text_preserves_object_failure() -> None:
    result = SimpleNamespace(success=False, data=None, error="read failed")

    assert _normalize_tool_text(result) == "[ERROR]: read failed"


@pytest.mark.asyncio
async def test_document_parse_marks_tool_output_failure_as_read_failure(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"placeholder")
    node = DocumentParseNode()
    result = SimpleNamespace(success=False, data=None, error="read failed")

    monkeypatch.setattr(node, "has_tool", lambda _name: True)

    async def call_tool(_name: str, **_kwargs: Any) -> Any:
        return result

    monkeypatch.setattr(node, "call_tool", call_tool)

    _, content = await node._read_single_document(source)

    assert content == "[读取失败: [ERROR]: read failed]"


@pytest.mark.asyncio
async def test_document_parse_reads_pdf_in_page_batches(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-placeholder")
    node = DocumentParseNode()

    async def read_pdf(path: Path) -> str:
        assert path == source
        return "PDF content"

    async def reject_text_read(_path: Path) -> str:
        raise AssertionError("PDF must use the paged reader")

    monkeypatch.setattr(node, "_read_large_pdf_file", read_pdf)
    monkeypatch.setattr(node, "_read_text_file", reject_text_read)

    _, content = await node._read_single_document(source)

    assert content == "PDF content"


@pytest.mark.asyncio
async def test_pdf_page_batches_stop_on_agent_core_out_of_range_error(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-placeholder")
    node = DocumentParseNode()
    calls: list[str] = []

    monkeypatch.setattr(node, "has_tool", lambda _name: True)

    async def call_tool(_name: str, **kwargs: Any) -> Any:
        pages = kwargs["pages"]
        calls.append(pages)
        if pages == "1-10":
            return SimpleNamespace(
                success=True,
                data={"content": "first batch"},
                error=None,
            )
        return SimpleNamespace(
            success=False,
            data=None,
            error=f"Invalid or empty PDF page range: '{pages}'",
        )

    monkeypatch.setattr(node, "call_tool", call_tool)

    content = await node._read_large_pdf_file(source)

    assert content == "first batch"
    assert calls == ["1-10", "11-20"]


@pytest.mark.asyncio
async def test_root_stops_when_all_documents_fail(monkeypatch) -> None:
    root = PPTGenRootNode()
    calls: list[str] = []

    async def run_subplan(subplan, inputs, results) -> None:
        calls.append(subplan.plan_name)
        if subplan is root._p1:
            inputs["has_documents"] = True
        if subplan is root._p3:
            inputs["doc_parse_ok"] = False
            inputs["doc_parse_error"] = "all reads failed"
        results.append({"node": subplan.plan_name, "status": "ok"})

    monkeypatch.setattr(root, "_run_subplan", run_subplan)

    result = await root._execute({})

    assert result["status"] == "error"
    assert "all reads failed" in result["message"]
    assert calls == ["p0_pipeline_init", "p1_intent_classify", "p3_document_parse"]


@pytest.mark.asyncio
async def test_stream_root_stops_when_all_documents_fail(monkeypatch) -> None:
    root = PPTGenRootNode()
    calls: list[str] = []

    async def should_skip(_subplan, _inputs) -> bool:
        return False

    async def run_subplan_stream(subplan, inputs, results, **_kwargs):
        calls.append(subplan.plan_name)
        if subplan is root._p1:
            inputs["has_documents"] = True
        if subplan is root._p3:
            inputs["doc_parse_ok"] = False
            inputs["doc_parse_error"] = "all reads failed"
        result = {"node": subplan.plan_name, "status": "ok"}
        results.append({"node": subplan.plan_name, "status": "ok", "result": result})
        yield result

    monkeypatch.setattr(root, "should_skip_subplan", should_skip)
    monkeypatch.setattr(root, "_run_subplan_stream", run_subplan_stream)

    chunks = [chunk async for chunk in root._execute_stream({})]

    assert chunks[-1]["status"] == "error"
    assert "all reads failed" in chunks[-1]["message"]
    assert calls == ["p0_pipeline_init", "p1_intent_classify", "p3_document_parse"]
