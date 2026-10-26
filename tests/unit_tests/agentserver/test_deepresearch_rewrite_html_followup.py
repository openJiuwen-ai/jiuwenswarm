import json

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch import (
    deepresearch_rewrite_html_followup as html_followup_module,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.deepresearch_rewrite_html_followup import (
    PENDING_HTML_EXPORT_STATE_KEY,
    RewriteHtmlTarget,
    decode_html_tool_result,
    is_html_followup_request,
    target_from_commit_result,
    target_from_state,
)


@pytest.mark.parametrize(
    "query",
    [
        "生成 HTML",
        "生成html",
        "请生成 HTML。",
        "生成最终美化版HTML！",
        "请生成最终美化版 html",
    ],
)
def test_html_followup_accepts_only_documented_explicit_phrases(query):
    assert is_html_followup_request(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "暂不生成 HTML",
        "继续改写",
        "生成 HTML 和 PDF",
        "把下面这句话改成：生成 HTML",
        (
            "<deepresearch_rewrite_request>"
            '{"report_path":"/workspace/report.md","action":"polish",'
            '"selection":{"selected_text":"生成 HTML"},"instruction":""}'
            "</deepresearch_rewrite_request>"
        ),
        None,
    ],
)
def test_html_followup_rejects_ambiguous_or_unrelated_requests(query):
    assert is_html_followup_request(query) is False


def test_html_followup_bounds_input_before_split(monkeypatch):
    monkeypatch.setattr(html_followup_module, "_HTML_FOLLOWUP_MAX_CHARS", 16)

    assert is_html_followup_request("请" * 17) is False


def test_target_from_commit_accepts_only_completed_trusted_fields():
    target = target_from_commit_result({
        "status": "completed",
        "report_path": "/workspace/report-v2.md",
        "revision_id": "rev_child-2",
        "user_supplied_extra": "ignored",
    })

    assert target == RewriteHtmlTarget(
        report_path="/workspace/report-v2.md",
        revision_id="rev_child-2",
    )
    assert target.to_state() == {
        "schema_version": 1,
        "report_path": "/workspace/report-v2.md",
        "revision_id": "rev_child-2",
    }
    assert PENDING_HTML_EXPORT_STATE_KEY == "deepresearch_pending_html_export"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"status": "error", "report_path": "/workspace/r.md", "revision_id": "rev_1"},
        {"status": "completed", "revision_id": "rev_1"},
        {"status": "completed", "report_path": "", "revision_id": "rev_1"},
        {"status": "completed", "report_path": "   ", "revision_id": "rev_1"},
        {"status": "completed", "report_path": "/workspace/r.md", "revision_id": "bad"},
        {"status": "completed", "report_path": "/workspace/r.md", "revision_id": 1},
    ],
)
def test_target_from_commit_rejects_untrusted_or_incomplete_results(payload):
    assert target_from_commit_result(payload) is None


def test_target_from_state_requires_exact_versioned_shape():
    assert target_from_state({
        "schema_version": 1,
        "report_path": "/workspace/report-v2.md",
        "revision_id": "rev_child",
    }) == RewriteHtmlTarget(
        report_path="/workspace/report-v2.md",
        revision_id="rev_child",
    )
    assert target_from_state({
        "schema_version": 2,
        "report_path": "/workspace/report-v2.md",
        "revision_id": "rev_child",
    }) is None
    assert target_from_state({
        "schema_version": 1,
        "report_path": "/workspace/report-v2.md",
        "revision_id": "rev_child",
        "extra": True,
    }) is None


def test_decode_html_tool_result_returns_fixed_safe_messages():
    success = decode_html_tool_result(json.dumps({
        "status": "completed",
        "html_delivered": True,
        "delivery_status": "delivered",
    }))
    failure = decode_html_tool_result(json.dumps({
        "status": "error",
        "error_code": "HTML_GENERATION_FAILED",
        "error": "/secret/path leaked by backend",
    }))
    malformed = decode_html_tool_result("not-json")

    assert success.status == "completed"
    assert success.error_code is None
    assert success.message == "已生成美化后的 HTML。"
    assert failure.status == "error"
    assert failure.error_code == "HTML_GENERATION_FAILED"
    assert failure.message == "HTML 生成失败，但 Markdown 改写版本仍然成功保留。"
    assert "/secret/path" not in failure.message
    assert malformed.status == "error"
    assert malformed.error_code == "INTERNAL_ERROR"


def test_decode_html_tool_result_maps_deep_and_oversized_json_to_fixed_error():
    nesting = 1_100
    deep = "[" * nesting + "0" + "]" * nesting
    oversized = json.dumps({"status": "error", "padding": "x" * (64 * 1024)})

    for payload in (deep, oversized, '{"status":"completed","value":NaN}'):
        result = decode_html_tool_result(payload)
        assert result.status == "error"
        assert result.error_code == "INTERNAL_ERROR"
        assert result.message == "HTML 生成失败，但 Markdown 改写版本仍然成功保留。"
