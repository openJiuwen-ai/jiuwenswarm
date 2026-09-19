"""D19 tier 2, chat half: the closing claim reconciled against the write count."""

from __future__ import annotations

import types

import pytest

from jiuwenswarm.agents.harness.common.rails.report_ledger_rail import ReportLedgerRail


def _ctx(**inputs):
    ctx = types.SimpleNamespace()
    ctx.inputs = types.SimpleNamespace(**inputs)
    ctx.extra = {}
    return ctx


@pytest.mark.asyncio
async def test_a_claim_over_zero_writes_gets_the_annotation():
    """The founding incident: "I have drafted the plan in the shared document" over
    a turn that read two documents and wrote nothing."""
    rail = ReportLedgerRail()
    await rail.before_invoke(_ctx())
    await rail.after_tool_call(_ctx(tool_name="clouddoc_read", tool_result={"ok": True}))
    ctx = _ctx(result={"output": "I have drafted the plan in the shared document."})
    await rail.after_invoke(ctx)
    assert "对账提示" in ctx.inputs.result["output"]
    assert ctx.extra.get("_report_ledger_flag") is True


@pytest.mark.asyncio
async def test_a_claim_backed_by_a_write_passes_untouched():
    rail = ReportLedgerRail()
    await rail.before_invoke(_ctx())
    await rail.after_tool_call(_ctx(tool_name="clouddoc_write_region", tool_result={"ok": True}))
    ctx = _ctx(result={"output": "已直接修改，B4 已更新。"})
    await rail.after_invoke(ctx)
    assert "对账提示" not in ctx.inputs.result["output"]


@pytest.mark.asyncio
async def test_a_refused_write_does_not_count_as_a_write():
    """A write the rails refused left the document untouched; a claim on top of it
    is exactly as false as one over no call at all."""
    rail = ReportLedgerRail()
    await rail.before_invoke(_ctx())
    await rail.after_tool_call(_ctx(tool_name="clouddoc_batch_edit", tool_result={"ok": False, "detail": "拒绝"}))
    ctx = _ctx(result={"output": "文档已更新完毕。"})
    await rail.after_invoke(ctx)
    assert "对账提示" in ctx.inputs.result["output"]


@pytest.mark.asyncio
async def test_a_turn_that_never_touched_clouddoc_is_out_of_scope():
    """"已修改" about anything else -- code, config, a local file -- is none of this
    rail's business; the gate is having touched clouddoc tools at all."""
    rail = ReportLedgerRail()
    await rail.before_invoke(_ctx())
    ctx = _ctx(result={"output": "配置已修改。"})
    await rail.after_invoke(ctx)
    assert "对账提示" not in ctx.inputs.result["output"]


@pytest.mark.asyncio
async def test_counters_reset_between_turns():
    """A write in turn one must not vouch for a claim in turn two."""
    rail = ReportLedgerRail()
    await rail.before_invoke(_ctx())
    await rail.after_tool_call(_ctx(tool_name="clouddoc_batch_edit", tool_result={"ok": True}))
    await rail.before_invoke(_ctx())
    await rail.after_tool_call(_ctx(tool_name="clouddoc_read", tool_result={"ok": True}))
    ctx = _ctx(result={"output": "文档已修改完成。"})
    await rail.after_invoke(ctx)
    assert "对账提示" in ctx.inputs.result["output"]


@pytest.mark.asyncio
async def test_adding_a_page_counts_as_a_write():
    """A new worksheet or slide changes the shared document; a claim over it is backed."""
    rail = ReportLedgerRail()
    await rail.before_invoke(_ctx())
    await rail.after_tool_call(_ctx(tool_name="clouddoc_add_page", tool_result={"ok": True}))
    ctx = _ctx(result={"output": "已新增工作表并更新文档。"})
    await rail.after_invoke(ctx)
    assert "对账提示" not in ctx.inputs.result["output"]


# ------------------------- review: a truthful read-only answer is not a claim


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "文档已读取，内容如下：第一段……",
    "I have written a summary below based on the document.",
    "I have read the spreadsheet; here is what it says.",
    "已读取表格 B2:D9，未做修改。",
    # A change to something that is not the document, in the same sentence.
    "配置已更新，文档未修改。",
    "已读取表格，配置文件已更新。",
    "已修改完成。",                                  # no object at all: not a claim about the document
    # Someone else's edit, reported truthfully.
    "The sheet was updated by another editor.",
    "The document has been modified by your colleague since the last read.",
])
async def test_read_only_prose_after_a_read_passes_untouched(text):
    rail = ReportLedgerRail()
    await rail.before_invoke(_ctx())
    await rail.after_tool_call(_ctx(tool_name="clouddoc_read", tool_result={"ok": True}))
    ctx = _ctx(result={"output": text})
    await rail.after_invoke(ctx)
    assert "对账提示" not in ctx.inputs.result["output"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "I have updated the document as requested.",
    "I have written the new paragraph into the doc.",
    "The spreadsheet has been updated.",
    "文档已修改完成，请查看。",
    "已更新文档，请查看。",
    "已把新段落写入文档。",
    "工作表「台账」已新增，B4 已更新。",
])
async def test_a_modification_claim_over_a_read_only_turn_is_annotated(text):
    rail = ReportLedgerRail()
    await rail.before_invoke(_ctx())
    await rail.after_tool_call(_ctx(tool_name="clouddoc_read", tool_result={"ok": True}))
    ctx = _ctx(result={"output": text})
    await rail.after_invoke(ctx)
    assert "对账提示" in ctx.inputs.result["output"]
