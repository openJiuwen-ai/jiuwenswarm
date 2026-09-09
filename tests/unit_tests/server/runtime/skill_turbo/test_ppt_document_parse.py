from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.rails.read_file_validation import (
    is_non_text_file_path,
    validate_read_file_result,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import document_parse
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.document_parse import (
    DocumentParseNode,
    _filter_parseable_paths,
    _normalize_tool_text,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_gen_root import (
    PPTGenRootNode,
)
from jiuwenswarm.server.runtime.skill_turbo.validator import PlanCodeValidator


def test_document_parse_passes_builtin_skill_validation() -> None:
    source = Path(document_parse.__file__).read_text(encoding="utf-8")
    validator = PlanCodeValidator.for_builtin_skill_code(
        ["jiuwenswarm.server.runtime.skill_turbo.skill_codes"]
    )

    assert validator.validate(source) == []


def test_pdf_is_delegated_to_read_file() -> None:
    assert is_non_text_file_path("report.pdf") is False
    assert validate_read_file_result("report.pdf", "extracted PDF text") == (True, None)


def test_normalize_tool_text_preserves_object_failure() -> None:
    result = {"success": False, "data": None, "error": "read failed"}

    assert _normalize_tool_text(result) == "[ERROR]: read failed"


@pytest.mark.asyncio
async def test_parse_with_retry_fails_without_read_file_degrade(
    tmp_path: Path, monkeypatch
) -> None:
    """content-material.md #7：CLI 两轮失败不得 read_file 降级。"""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-placeholder")
    node = DocumentParseNode()
    paths = node._artifact_paths(tmp_path)
    inputs: dict[str, Any] = {
        "pptx_root": str(tmp_path),
        "output_dir": str(tmp_path),
    }
    read_file_calls: list[str] = []

    async def _no_vision(_inputs: dict[str, Any]) -> bool:
        return False

    async def _fail_parse_docs(*_args: Any, **_kwargs: Any) -> None:
        from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils import (
            BashExecError,
        )

        raise BashExecError("parse-docs unavailable")

    async def call_tool(name: str, **kwargs: Any) -> Any:
        if name == "read_file":
            read_file_calls.append(str(kwargs.get("file_path") or ""))
        raise AssertionError(f"unexpected tool during failed parse: {name}")

    monkeypatch.setattr(node, "_probe_vision", _no_vision)
    monkeypatch.setattr(node, "_run_parse_docs", _fail_parse_docs)
    monkeypatch.setattr(node, "call_tool", call_tool)

    ok, error = await node._parse_with_retry(inputs, [str(source)], paths)

    assert ok is False
    assert error == "parse-docs unavailable"
    assert inputs.get("parse_degraded") is False
    assert read_file_calls == []
    assert not paths["raw"].is_file()
    assert not paths["summary"].is_file()
    assert not hasattr(node, "_degraded_parse")


def test_filter_parseable_paths_excludes_presentations() -> None:
    paths = [
        "report.pdf",
        "notes.docx",
        "slides.pptx",
        "template.PPT",
    ]

    assert _filter_parseable_paths(paths) == ["report.pdf", "notes.docx"]


@pytest.mark.asyncio
async def test_execute_marks_presentation_only_inputs_as_unparseable(tmp_path: Path) -> None:
    node = DocumentParseNode()
    inputs = {
        "has_documents": True,
        "output_dir": str(tmp_path),
        "doc_paths": [str(tmp_path / "slides.pptx")],
    }

    result = await node._execute(inputs)

    assert result["doc_parse_ok"] is False
    assert result["doc_parse_error"] == "无可解析文档（演示文稿不进入 parse-docs）"
    assert result["has_documents"] is False


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
