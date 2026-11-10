# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise public issue submission through Web request and interrupt handling."""

import json
from functools import partial

import httpx
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
import pytest

from jiuwenswarm.agents.harness.common.tools import report_issue
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import (
    _request,
    answer_host,  # noqa: F401
)
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold  # noqa: F401

_TOKEN = "test-gitcode-credential"
_TITLE = "Web chat freezes after model selection"
_BODY = "## Reproduction\n1. Select a model.\n\nActual: chat freezes."
_TRACKER = "https://gitcode.com/openJiuwen/jiuwenswarm/issues"


@pytest.fixture
def issue_api(monkeypatch, tmp_path):
    requests = []
    monkeypatch.setattr(report_issue, "get_runtime_state_path", lambda: tmp_path / "runtime.yaml")
    monkeypatch.setattr(
        report_issue.CredentialStore, "get_all", lambda self, name: {"GITCODE_TOKEN": _TOKEN} if name == "gitcode" else {},
    )

    def post(request):
        requests.append(request)
        if "timeout" in request.content.decode():
            raise httpx.ReadTimeout("Simulated disconnect", request=request)
        return httpx.Response(200, json={"number": 42, "html_url": f"{_TRACKER}/42"})

    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(post)))
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["allow_once", "reject"])
async def test_web_issue_report_waits_for_confirmation(answer_host, issue_api, action):
    h = answer_host
    h.raw["permissions"]["defaults"]["*"] = "allow"
    await h.create()
    session_id = h.adapter._parent_session_id
    h.adapter._update_report_issue_tool(session_id, "web")
    await h.adapter.start_interaction(session_id)
    h.script.responses = [
        AssistantMessage(content="", tool_calls=[ToolCall(
            id="issue-call", type="function", name="report_issue",
            arguments=json.dumps({"title": _TITLE, "body": f"{_BODY}\n\nToken: {_TOKEN}"}),
        )]),
        AssistantMessage(content="Done"),
    ]

    paused = await _request(h, query="Report this bug")

    assert paused.ok, paused.payload
    assert not issue_api
    state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert state and state.interrupted_tools, paused.payload
    entry = state.interrupted_tools["issue-call"]
    request = next(iter(entry.interrupt_requests.values()))
    assert request.interrupt_kind == "confirm"
    assert _TITLE in request.message and _TRACKER in request.message
    assert _TOKEN not in request.message and "[redacted]" in request.message
    assert [option["value"] for option in request.ui_options] == ["allow_once", "reject"]

    result = await _request(
        h, source="confirm_interrupt", request_id="issue-call", interrupt_id=request.interrupt_id,
        answers=[{"selected_options": [action]}],
    )

    assert result.ok, result.payload
    if action == "reject":
        assert not issue_api
        assert "cancelled" in repr(h.script.calls[-1])
    else:
        post, = issue_api
        assert post.method == "POST"
        assert str(post.url) == "https://api.gitcode.com/api/v5/repos/openJiuwen/issues"
        assert post.headers["Authorization"] == f"Bearer {_TOKEN}"
        assert json.loads(post.content) == {
            "repo": "jiuwenswarm", "title": _TITLE, "body": f"{_BODY}\n\nToken: [redacted]",
        }
        assert f"{_TRACKER}/42" in repr(h.script.calls[-1])


@pytest.mark.parametrize("body, status", [(_BODY, "created"), ("timeout", "outcome_unknown")])
def test_approval_receipt_prevents_retry_after_success_or_timeout(issue_api, body, status):
    draft = report_issue.IssueDraft(title=_TITLE, body=body)
    first = report_issue._submit_report("intr_repeat", draft, _TOKEN)
    repeated = report_issue._submit_report("intr_repeat", draft, _TOKEN)

    assert first == repeated
    assert first["status"] == status
    assert len(issue_api) == 1
