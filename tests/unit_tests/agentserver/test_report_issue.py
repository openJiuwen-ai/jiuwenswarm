# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise public issue submission through Web request and interrupt handling."""

import base64
from functools import partial
import hashlib
import json
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import httpx
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import extract_question_from_interaction
from jiuwenswarm.agents.harness.common.tools import report_issue
from jiuwenswarm.agents.harness.common.tools.issue_report_template import BugReportInput, load_bug_report_template
from jiuwenswarm.common.tool_ownership import qualify_tool_id
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import (
    _request,
    answer_host,  # noqa: F401
)
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold  # noqa: F401

_TOKEN = "test-gitcode-credential"
_TITLE = "Web chat freezes after model selection"
_BODY = "## Reproduction\n1. Select a model.\n\nActual: chat freezes."
_TRACKER = "https://gitcode.com/openJiuwen/jiuwenswarm/issues"
_EN_PATH = ".gitcode/ISSUE_TEMPLATE.en/001-bug-report.yml"
_ZH_PATH = ".gitcode/ISSUE_TEMPLATE.zh/001-bug-report.yml"
_CHECKED = "I have searched for related issues but did not get the expected help."


@pytest.fixture
def issue_api(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[3]
    templates = {path: (root / path).read_text(encoding="utf-8") for path in (_EN_PATH, _ZH_PATH)}
    reads, posts = [], []
    api = SimpleNamespace(templates=templates, reads=reads, posts=posts, gitcode_status=200)
    monkeypatch.setattr(report_issue, "get_runtime_state_path", lambda: tmp_path / "runtime.yaml")
    monkeypatch.setattr(
        report_issue.CredentialStore, "get_all", lambda self, name: {"GITCODE_TOKEN": _TOKEN} if name == "gitcode" else {},
    )

    def respond(request):
        if request.method == "GET":
            reads.append(request)
            if request.url.host == "raw.githubusercontent.com":
                path = request.url.path.removeprefix("/openJiuwen-ai/jiuwenswarm/develop/")
                return httpx.Response(200, content=templates[path].encode())
            if api.gitcode_status != 200:
                return httpx.Response(api.gitcode_status)
            path = request.url.path.split("/contents/", 1)[-1]
            return httpx.Response(200, json={"content": base64.b64encode(templates[path].encode()).decode()})
        posts.append(request)
        if "timeout" in request.content.decode():
            raise httpx.ReadTimeout("Simulated disconnect", request=request)
        return httpx.Response(200, json={"number": 42, "html_url": f"{_TRACKER}/42"})

    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=httpx.MockTransport(respond)))
    return api


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["allow_once", "reject", "template_changed"])
async def test_web_issue_report_waits_for_confirmation(answer_host, issue_api, action):
    h = answer_host
    h.raw["permissions"]["defaults"]["*"] = "allow"
    await h.create()
    session_id = h.adapter._parent_session_id
    h.adapter._update_report_issue_tool(session_id, "web")
    await h.adapter.start_interaction(session_id)
    template_id = hashlib.sha256(f"{_EN_PATH}\n{issue_api.templates[_EN_PATH]}".encode()).hexdigest()
    h.script.responses = [
        AssistantMessage(content="", tool_calls=[ToolCall(
            id="template-call", type="function", name="get_issue_template", arguments="{}",
        )]),
        AssistantMessage(content="", tool_calls=[ToolCall(
            id="issue-call", type="function", name="report_issue",
            arguments=json.dumps({
                "template_id": template_id,
                "title": _TITLE,
                "fields": {
                    "description": "The Web composer stops responding.",
                    "reproduction_steps": "Select a model.",
                    "expected_behavior": "Chat stays responsive.",
                    "actual_behavior": "Chat freezes.",
                    "environment": "Windows, Python 3.13",
                    "version": "0.2.9",
                    "checklist": [_CHECKED],
                    "additional_information": f"Token: {_TOKEN}",
                },
            }),
        )]),
        AssistantMessage(content="Done"),
    ]

    paused = await _request(h, query="Report this bug")

    assert paused.ok, paused.payload
    assert not issue_api.posts
    assert len(issue_api.reads) == 2
    assert all(request.url.path.endswith(_EN_PATH) and "Authorization" not in request.headers for request in issue_api.reads)
    assert _EN_PATH in repr(h.script.calls[1]) and template_id in repr(h.script.calls[1])
    state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert state and state.interrupted_tools, paused.payload
    entry = state.interrupted_tools["issue-call"]
    request = next(iter(entry.interrupt_requests.values()))
    assert request.interrupt_kind == "confirm"
    assert f"[Bug]: {_TITLE}" in request.message and _TRACKER in request.message
    assert "Labels: bug" in request.message
    assert "### Version Information" in request.message
    assert _TOKEN not in request.message and "[redacted]" in request.message
    assert [option["value"] for option in request.ui_options] == ["allow_once", "reject"]
    question = extract_question_from_interaction({"value": request})
    assert question["question"] == request.message
    assert [option["value"] for option in question["options"]] == ["allow_once", "reject"]
    preview = dedent(question["question"].split("\n\n", 2)[2])

    if action == "template_changed":
        issue_api.templates[_EN_PATH] += "\n# Template changed after preview\n"
    result = await _request(
        h, source="confirm_interrupt", request_id="issue-call", interrupt_id=request.interrupt_id,
        answers=[{"selected_options": ["allow_once" if action == "template_changed" else action]}],
    )

    assert result.ok, result.payload
    if action != "allow_once":
        assert not issue_api.posts
        expected_status = "cancelled" if action == "reject" else "template_changed"
        assert expected_status in repr(h.script.calls[-1])
    else:
        post, = issue_api.posts
        assert post.method == "POST"
        assert str(post.url) == "https://api.gitcode.com/api/v5/repos/openJiuwen/issues"
        assert post.headers["Authorization"] == f"Bearer {_TOKEN}"
        payload = json.loads(post.content)
        assert payload["repo"] == "jiuwenswarm"
        assert payload["title"] == f"[Bug]: {_TITLE}"
        assert payload["labels"] == "bug"
        assert preview == f"{payload['title']}\nLabels: {payload['labels']}\n\n{payload['body']}"
        assert payload["body"] == (
            "### Checklist\n\n"
            f"- [x] {_CHECKED}\n"
            "- [ ] This bug has not been fixed in the latest version.\n"
            "- [ ] Please note that if your bug description lacks corresponding environment information "
            "and a minimal reproducible demo, it will be difficult for us to reproduce and resolve the issue, "
            "which will reduce the possibility of receiving feedback, or even lead to the issue being closed.\n\n"
            "### 🐞 Detailed Description of the Problem\n\n"
            "The Web composer stops responding.\n\n"
            "#### Reproduction steps\n\nSelect a model.\n\n"
            "#### Expected behavior\n\nChat stays responsive.\n\n"
            "#### Actual behavior\n\nChat freezes.\n\n"
            "### Detailed Environment Information Description\n\nWindows, Python 3.13\n\n"
            "### Additional Information\n\nToken: [redacted]\n\n"
            "### Version Information\n\n0.2.9"
        )
        assert f"{_TRACKER}/42" in repr(h.script.calls[-1])


@pytest.mark.parametrize("body, status", [(_BODY, "created"), ("timeout", "outcome_unknown")])
def test_approval_receipt_prevents_retry_after_success_or_timeout(issue_api, body, status):
    draft = report_issue.IssueDraft(title=_TITLE, body=body)
    first = report_issue._submit_report("intr_repeat", draft, _TOKEN)
    repeated = report_issue._submit_report("intr_repeat", draft, _TOKEN)

    assert first == repeated
    assert first["status"] == status
    assert len(issue_api.posts) == 1


def test_report_tool_refresh_removes_only_adapter_owned_cards(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._last_mode = "team.work.normal"
    adapter._tool_owner_id = lambda: "single-agent-adapter"
    local_tool, leader_tool = report_issue.build_issue_report_tools("web-report", "en")
    local_tool.card.id = qualify_tool_id(local_tool.card, "single-agent-adapter")
    leader_tool.card.id = qualify_tool_id(leader_tool.card, "team-leader")
    cards = [local_tool.card, leader_tool.card]

    def remove(name):
        cards[:] = [card for card in cards if card.name != name]

    manager = SimpleNamespace(list=lambda: list(cards), remove=remove)
    adapter._instance = SimpleNamespace(ability_manager=manager)

    adapter._update_report_issue_tool("web-report", "web")

    assert cards == [leader_tool.card]


def test_chinese_template_validates_the_required_branch_module_pair(issue_api):
    template = load_bug_report_template("openJiuwen", "jiuwenswarm", "cn")
    form = template.describe()
    assert form["template_path"] == _ZH_PATH
    assert {"branch", "module", "checklist"}.issubset(form["required_fields"])
    assert "version" in form["optional_fields"]
    draft = BugReportInput.model_validate({
        "template_id": form["template_id"],
        "title": "[Bug]: Composer issue",
        "fields": {
            "description": "Chat freezes.",
            "reproduction_steps": "Select a model.",
            "expected_behavior": "Chat stays responsive.",
            "actual_behavior": "Chat freezes.",
            "environment": "Windows, Python 3.13",
            "branch": "develop",
            "module": "【通用】 前端",
            "checklist": [form["checklist_options"][0]["label"]],
        },
    })
    title, body = template.render(draft)
    assert title == "[Bug]: Composer issue"
    assert body.startswith("### 分支 / 模块\n\ndevelop/【通用】 前端")
    assert "### 版本信息" not in body

    draft.fields.module = "【dev-stable专属】 性能"
    with pytest.raises(ValueError, match="branch and module pair"):
        template.render(draft)


def test_template_lookup_falls_back_to_github_during_gitcode_outage(issue_api):
    primary = load_bug_report_template("openJiuwen", "jiuwenswarm", "en").describe()
    issue_api.gitcode_status = 503

    mirror = load_bug_report_template("openJiuwen", "jiuwenswarm", "en").describe()

    assert primary["template_source"] == "gitcode"
    assert mirror["template_source"] == "github"
    assert mirror["template_id"] == primary["template_id"]
    request = issue_api.reads[-1]
    assert str(request.url) == f"https://raw.githubusercontent.com/openJiuwen-ai/jiuwenswarm/develop/{_EN_PATH}"
    assert "Authorization" not in request.headers


def test_missing_gitcode_template_stays_unavailable(issue_api):
    issue_api.gitcode_status = 404

    with pytest.raises(httpx.HTTPStatusError, match="404"):
        load_bug_report_template("openJiuwen", "jiuwenswarm", "en")

    request, = issue_api.reads
    assert request.url.host == "api.gitcode.com"
