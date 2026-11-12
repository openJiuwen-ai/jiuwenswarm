# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""File an approved bug report in JiuwenSwarm's public GitCode tracker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import closing
import hashlib
import json
import sqlite3
from textwrap import indent
from typing import Any, Never

import httpx
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolExposure
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.ownership import InterruptOwnerMixin
from openjiuwen.core.single_agent.interrupt.response import InterruptKind, InterruptRequest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload
from pydantic import BaseModel, ConfigDict, Field, ValidationError
import yaml

from jiuwenswarm.agents.harness.common.a4p.execution_context import AUTHORIZATION_EXECUTION_CONTEXTS
from jiuwenswarm.agents.harness.common.auto_harness.issue_fix.gitcode_issue_client import GitCodeIssueClient
from jiuwenswarm.agents.harness.common.rails.interrupt.permission_options import ALLOW_ONCE, REJECT
from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_redaction import redact_secret_values_preserve_format
from jiuwenswarm.agents.harness.common.tools.issue_report_template import BugReportInput, load_bug_report_template
from jiuwenswarm.common.utils import get_runtime_state_path
from jiuwenswarm.server.runtime.mcp.credential import CredentialStore

REPORT_ISSUE_TOOL_NAME = "report_issue"
GET_ISSUE_TEMPLATE_TOOL_NAME = "get_issue_template"
ISSUE_REPORT_TOOL_NAMES = (GET_ISSUE_TEMPLATE_TOOL_NAME, REPORT_ISSUE_TOOL_NAME)
_OWNER = "openJiuwen"
_REPO = "jiuwenswarm"
_TRACKER_URL = f"https://gitcode.com/{_OWNER}/{_REPO}/issues"

_AUTH_REQUIRED = {
    "status": "auth_required",
    "message": "Connect GitCode in Integrations using a token with permission to create issues.",
    "tracker_url": _TRACKER_URL,
}
_UNKNOWN_OUTCOME = {
    "status": "outcome_unknown",
    "message": "GitCode may have created the issue. Check the tracker before submitting again.",
    "tracker_url": _TRACKER_URL,
}
_TEMPLATE_UNAVAILABLE = {
    "status": "template_unavailable",
    "message": "Could not load the current Bug Report form from GitCode. Retry or open the tracker.",
    "tracker_url": _TRACKER_URL,
}


class IssueDraft(BaseModel):
    """The complete report the user will see before submitting."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    title: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    body: str = Field(min_length=1, max_length=8000)
    labels: tuple[str, ...] = ()


def _gitcode_token() -> str:
    # The integration credential belongs to the current workspace. An Auto-Harness
    # or process-wide token may belong to a different account.
    try:
        credentials = CredentialStore().get_all("gitcode")
    except (OSError, ValueError):
        return ""
    token = credentials.get("GITCODE_TOKEN") or credentials.get("GC_TOKEN")
    return token.strip() if isinstance(token, str) else ""


def _submit_report(approval_id: str, draft: IssueDraft, token: str) -> dict[str, Any]:
    """Claim an approval durably before attempting the single external write."""
    path = get_runtime_state_path().parent / "issue_reports.sqlite3"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS issue_reports (approval_id TEXT PRIMARY KEY, result TEXT)"
                )
                claim = connection.execute(
                    "INSERT OR IGNORE INTO issue_reports (approval_id) VALUES (?)", (approval_id,)
                )
                if claim.rowcount == 0:
                    result, = connection.execute(
                        "SELECT result FROM issue_reports WHERE approval_id = ?", (approval_id,)
                    ).fetchone()
                    return json.loads(result) if result is not None else _UNKNOWN_OUTCOME

            result = _post_report(draft, token)
            try:
                with connection:
                    connection.execute(
                        "UPDATE issue_reports SET result = ? WHERE approval_id = ?",
                        (json.dumps(result), approval_id),
                    )
            except sqlite3.Error:
                # The committed claim still prevents replay if receipt storage fails.
                pass
            return result
    except (OSError, sqlite3.Error):
        return {"status": "storage_unavailable", "message": "Could not record the approval for submission."}


def _post_report(draft: IssueDraft, token: str) -> dict[str, Any]:
    try:
        issue = GitCodeIssueClient(token=token).create_issue(
            owner=_OWNER, repo=_REPO, title=draft.title, body=draft.body, labels=draft.labels,
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            return _AUTH_REQUIRED
        if 400 <= status < 500 and status != 408:
            return {
                "status": "rejected",
                "message": f"GitCode rejected the report (HTTP {status}).",
                "tracker_url": _TRACKER_URL,
            }
        return _UNKNOWN_OUTCOME
    except (httpx.HTTPError, ValueError):
        return _UNKNOWN_OUTCOME
    return {"status": "created", "number": issue.number, "url": f"{_TRACKER_URL}/{issue.number}"}


class GetIssueTemplateTool(Tool):
    """Expose the current Bug Report form without creating an issue."""

    def __init__(self, session_id: str, language: str = "en") -> None:
        super().__init__(ToolCard(
            id=GET_ISSUE_TEMPLATE_TOOL_NAME,
            name=GET_ISSUE_TEMPLATE_TOOL_NAME,
            description=(
                "Read the current JiuwenSwarm GitCode Bug Report template before drafting a report. "
                "Returns its template_id, path, title prefix, labels, and field choices for report_issue."
            ),
            input_params={"type": "object", "properties": {}, "additionalProperties": False},
            exposure=ToolExposure.DIRECT,
            idempotent=True,
        ))
        self._session_id = session_id
        self._language = language

    async def invoke(self, inputs: Any, *, session: Any = None) -> dict[str, Any]:
        run = AUTHORIZATION_EXECUTION_CONTEXTS.get(self._session_id)
        if run is None or not run.is_interactive_web:
            return {"status": "unavailable", "message": "Issue template lookup requires interactive Web chat."}
        try:
            template = await asyncio.to_thread(load_bug_report_template, _OWNER, _REPO, self._language)
        except (httpx.HTTPError, ValueError, TypeError, yaml.YAMLError):
            return _TEMPLATE_UNAVAILABLE
        return template.describe()

    async def stream(self, inputs: Any, *, session: Any = None) -> AsyncIterator[dict[str, Any]]:
        yield await self.invoke(inputs, session=session)


class ReportIssueTool(Tool, InterruptOwnerMixin):
    """Pause the tool call until the user approves its exact public payload."""

    accepts_tool_callback_context = True
    interrupt_kind = InterruptKind.CONFIRM

    def __init__(self, session_id: str, language: str = "en") -> None:
        super().__init__(ToolCard(
            id=REPORT_ISSUE_TOOL_NAME,
            name=REPORT_ISSUE_TOOL_NAME,
            description=(
                "File a new public bug report in the JiuwenSwarm GitCode tracker. "
                "Use only when the user asks to report a JiuwenSwarm bug. Call get_issue_template first. "
                "Ask for missing facts and only include checklist statements the user can confirm. "
                "Omit private data. The user reviews the draft before it posts. "
                "If the outcome is unknown, check the tracker before attempting another submission."
            ),
            input_params=BugReportInput.model_json_schema(),
            exposure=ToolExposure.DIRECT,
            parallel_safe=False,
        ))
        self._session_id = session_id
        self._language = language

    async def invoke(
        self,
        inputs: Any,
        *,
        session: Any = None,
        _tool_callback_context: AgentCallbackContext | None = None,
    ) -> dict[str, Any]:
        ctx = _tool_callback_context
        tool_call = ctx.inputs.tool_call if ctx is not None else None
        run = AUTHORIZATION_EXECUTION_CONTEXTS.get(self._session_id)
        is_interactive_web = run is not None and run.is_interactive_web
        if tool_call is None or not tool_call.id or not is_interactive_web:
            return {"status": "unavailable", "message": "Reporting requires interactive Web chat."}
        try:
            report = BugReportInput.model_validate(inputs)
        except ValidationError as exc:
            return {
                "status": "invalid_draft",
                "message": "Use get_issue_template and supply the structured report fields.",
                "fields": [".".join(map(str, error["loc"])) for error in exc.errors(include_input=False)],
            }

        token = await asyncio.to_thread(_gitcode_token)
        if not token:
            return _AUTH_REQUIRED
        try:
            template = await asyncio.to_thread(load_bug_report_template, _OWNER, _REPO, self._language)
        except (httpx.HTTPError, ValueError, TypeError, yaml.YAMLError):
            return _TEMPLATE_UNAVAILABLE
        if report.template_id != template.id:
            return {"status": "template_changed", "message": "Call get_issue_template again and review a new draft."}

        try:
            title, body = template.render(report)
        except ValueError as exc:
            return {"status": "invalid_draft", "message": str(exc)}
        try:
            draft = IssueDraft(
                title=redact_secret_values_preserve_format(title.replace(token, "[redacted]"), max_length=None),
                body=redact_secret_values_preserve_format(body.replace(token, "[redacted]"), max_length=None),
                labels=template.labels,
            )
        except ValidationError:
            return {"status": "invalid_draft", "message": "Shorten the report and submit it for review again."}
        digest = hashlib.sha256(f"{template.id}\n{draft.model_dump_json()}".encode("utf-8")).hexdigest()
        answer = self.find_own_answer(ctx.extra, tool_call.id)
        if answer is None:
            self._request_approval(tool_call, draft, digest)
        if (
            answer.unaddressed or not answer.request.interrupt_id
            or answer.request.metadata.get("report_digest") != digest
        ):
            return {"status": "cancelled", "message": "The report needs a new approval for this exact draft."}

        approval = answer.value
        if isinstance(approval, ConfirmPayload):
            approval = approval.model_dump()
        if (
            not isinstance(approval, dict) or approval.get("approved") is not True
            or approval.get("auto_confirm", False) is not False
        ):
            return {"status": "cancelled", "message": "Issue submission cancelled."}
        return await asyncio.to_thread(_submit_report, answer.request.interrupt_id, draft, token)

    async def stream(
        self,
        inputs: Any,
        *,
        session: Any = None,
        _tool_callback_context: AgentCallbackContext | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        yield await self.invoke(inputs, session=session, _tool_callback_context=_tool_callback_context)

    def _request_approval(self, tool_call: ToolCall, draft: IssueDraft, digest: str) -> Never:
        if self._language == "en":
            heading = "Review this draft before posting publicly to JiuwenSwarm on GitCode."
            allow, reject = "Allow once", "Reject"
            labels_caption = "Labels"
        else:
            heading = "请检查草稿。确认后，报告将公开发布到 GitCode 上的 JiuwenSwarm 项目。"
            allow, reject = "本次允许", "拒绝"
            labels_caption = "标签"
        labels = ", ".join(draft.labels) or "—"
        preview = indent(f"{draft.title}\n{labels_caption}: {labels}\n\n{draft.body}", "    ")
        request = InterruptRequest(
            message=f"{heading}\n\n{_TRACKER_URL}\n\n{preview}",
            payload_schema=ConfirmPayload.to_schema(),
            ui_options=[{"value": ALLOW_ONCE, "label": allow}, {"value": REJECT, "label": reject}],
            metadata={"report_digest": digest},
        )
        self.stamp_interrupt(request)
        raise ToolInterruptException(request=request, tool_call=tool_call)


def build_issue_report_tools(session_id: str, language: str) -> list[Tool]:
    return [GetIssueTemplateTool(session_id, language), ReportIssueTool(session_id, language)]
