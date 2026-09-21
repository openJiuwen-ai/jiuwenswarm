import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rsi.failure_reason import with_failure_reason


def test_nested_judge_limit_is_explained_without_credentials(tmp_path):
    error = tmp_path / "cases" / "one" / "judge" / "error.json"
    error.parent.mkdir(parents=True)
    error.write_text(json.dumps({"message": (
        "RateLimitError: 429 Rate limit exceeded for api_key: secret-value. "
        "Limit type: max_parallel_requests. Current limit: 3, Remaining: 0."
    )}), encoding="utf-8")
    task = SimpleNamespace(status="FAILED", run_dir=str(tmp_path))
    payload = with_failure_reason(task, {"failure_reason": f"LLM evaluator failed; inspect {error}"})
    assert "429" in payload["failure_reason"]
    assert "3" in payload["failure_reason"]
    assert "LLM Judge" in payload["failure_reason"]
    assert "secret-value" not in payload["failure_reason"]


@pytest.mark.parametrize("kind", ["outside", "missing", "invalid", "large", "wrong_name"])
def test_unreadable_or_untrusted_error_does_not_break_task_get(tmp_path, kind):
    root = tmp_path / "run"
    root.mkdir()
    error = (tmp_path if kind == "outside" else root) / (
        "other.json" if kind == "wrong_name" else "error.json"
    )
    if kind != "missing":
        content = json.dumps({"message": "SHOULD_NOT_READ"})
        if kind == "invalid":
            content = "not json"
        if kind == "large":
            content = " " * 65537 + content
        error.write_text(content, encoding="utf-8")
    reason = f"LLM evaluator failed; inspect {error}"
    result = with_failure_reason(SimpleNamespace(status="FAILED", run_dir=str(root)), {"failure_reason": reason})
    assert result["failure_reason"] == reason


def test_mask_generic_error_and_ignore_nonfailed_task(tmp_path):
    task = SimpleNamespace(status="FAILED", run_dir=str(tmp_path))
    result = with_failure_reason(task, {"failure_reason": "api_key=secret-value"})
    assert "secret-value" not in result["failure_reason"]
    task.status = "RUNNING"
    assert with_failure_reason(task, {"status": "RUNNING"}) == {"status": "RUNNING"}
