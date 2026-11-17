# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An Excel turn does not end on cells written and not read back, or on a failed call, without one reminder."""

from __future__ import annotations

import json

import pytest
from openjiuwen.core.foundation.llm import ToolCall

from jiuwenswarm.office.excel_reminders import ExcelReminderRail
from tests.unit_tests.office.conftest import EXCEL_PACKAGE

WRITE = ToolCall(id="write-1", type="function", name="set_cell_range",
                 arguments=json.dumps({"sheetName": "Sheet1", "cells": {"A1": {"value": 1}}}))
READ = ToolCall(id="read-1", type="function", name="get_cell_ranges",
                arguments=json.dumps({"sheetName": "Sheet1", "ranges": ["A1"]}))
CODE_READ = ToolCall(id="code-1", type="function", name="execute_office_js",
                     arguments=json.dumps({"code": "const r = context.workbook.getSelectedRange(); r.load('address');"
                                                   " await context.sync(); return r.address;", "explanation": "where"}))
CODE = ToolCall(id="code-1", type="function", name="execute_office_js",
                arguments=json.dumps({"code": "return {error: 'no sheet'};", "explanation": "check"}))
WRITTEN = {"content": '{"formula_results": {}, "messages": []}'}
FAILED = {"content": "Sheet1 is protected", "is_error": True}


def reminders(model) -> list[str]:
    """The reminders in the model's last call, oldest first."""
    return [str(m.content) for m in model.seen[-1][0]
            if getattr(m, "role", "") == "user" and "<system-reminder>" in str(m.content)]


@pytest.mark.parametrize(("steps", "results", "expected"), [
    # Written, the model wants to stop: reminded once; it reads back and the turn ends.
    ([[WRITE], "Done.", [READ]], [WRITTEN, {"content": "{}"}], ["read back the cells"]),
    # Written and read back before stopping: nothing to remind.
    ([[WRITE], [READ]], [WRITTEN, {"content": "{}"}], []),
    # Failed and not retried: reminded once; the model may then end by naming it.
    ([[WRITE]], [FAILED], ["failed this turn"]),
    # Code that only read, as the pane's mutation record says: nothing to read back.
    ([[CODE_READ]], [{"content": '{"success": true, "result": "A1", "mutation": {"state": "not_applied"}}'}], []),
    # Code that ran and reported its own error counts as failed; data with an error field among others does not.
    ([[CODE]], [{"content": '{"success": true, "result": {"error": "no sheet"}}'}], ["failed this turn"]),
    ([[CODE]], [{"content": '{"success": true, "result": {"cell": "B2", "error": "#N/A"}, '
                            '"mutation": {"state": "not_applied"}}'}], []),
])
async def test_a_turn_ends_on_open_work_only_after_one_reminder(
        conversation, attach_pane, steps, results, expected) -> None:
    attach_pane("book-a", ["set_cell_range", "get_cell_ranges", "execute_office_js"], client_app="office-excel")

    async with conversation(steps, session_id="book-a", package=EXCEL_PACKAGE) as pane:
        await pane.agent.register_rail(ExcelReminderRail())
        requests = await pane.turn("put 1 in A1")
        for result in results:
            requests = await pane.answer((requests[0], result))
        assert requests == []

    seen = reminders(pane.model)
    assert len(seen) == len(expected)
    assert all(text in reminder for text, reminder in zip(expected, seen))
