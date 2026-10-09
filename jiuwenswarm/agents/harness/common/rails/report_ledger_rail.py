"""D19 tier 2, the chat half: a completion claim must reconcile with the ledger.

The measured failure shape (2026-08-13): the model answered "I have drafted the
plan in the shared document (Q3 Launch Note)" over a turn with **zero write
calls** -- the report was assembled from text the document already held. Nothing
mechanical stood between that sentence and the person reading it.

This rail audits the reconciliation without understanding anything: it counts the
turn's successful cloud-document writes at ``after_tool_call``, and at
``after_invoke`` -- when the final answer exists -- checks one implication: a
turn that *touched* cloud documents, *wrote nothing*, and *claims a document was
modified* is misreporting, whatever its words. The annotation is appended to the
answer rather than replacing it: the person should read the claim **and** the
audit, the same pairing the unattended path posts into the thread.

Honest limitation (VISIBILITY-VERIFY): on the streamed path the answer text may
have reached the client chunk by chunk before ``after_invoke`` runs, in which
case the appended annotation lands in the stored result but possibly not in the
live view. The log line fires either way; whether the annotation surfaces in the
web UI needs one live measurement, and until then this rail is a floor on the
record, not a guarantee on the screen.
"""

from __future__ import annotations

import logging
import re

from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

_WRITE_TOOLS = frozenset({
    "clouddoc_batch_edit",
    "clouddoc_write_region",
    "clouddoc_add_page",
    "clouddoc_apply_for_comment",
})

# What counts as a claim that the document changed. Narrow on purpose: the
# annotation is stapled onto the reply, so a pattern that also catches a truthful
# answer ("文档已读取", "配置已更新，文档未修改", "I have written a summary below",
# "the sheet was updated by another editor") corrupts the very reports it exists to
# keep honest. In both languages the document -- or a part of it: a worksheet, a
# cell address, a paragraph -- has to be the object or the destination of the
# agent's own completed change verb, adjacent to it; a change to something else in
# the same sentence, and a change someone else made, are not claims.
_DOC_NOUN = r"(?:the |your |this |that |our )?(?:shared )?(?:document|spreadsheet|sheet|deck|slides?|doc)\b"
_ZH_OBJECT = r"(?:文档|表格|幻灯片|正文|工作表|单元格|页面|段落|标题|[A-Z]{1,3}\d{1,5})"
_ZH_VERB = r"(?:修改|更新|写入|应用|改好|改写|替换|新增|删除|完成修改)"
_CLAIMS_DOC_MODIFIED = re.compile(
    "|".join((
        # 文档已更新 / 工作表已新增 / B4 已修改 -- the object right before the verb
        _ZH_OBJECT + r"\s*(?:已|中已|里已|内已|都已|均已)(?:直接)?" + _ZH_VERB,
        # 已更新文档 / 已修改了正文第二段 -- the object right after it
        r"已(?:直接)?" + _ZH_VERB + r"(?:了|好)?[^，。；！？\n]{0,8}?" + _ZH_OBJECT,
        # 已把新段落写入文档 -- the document as the destination
        r"已(?:直接)?(?:把|将)[^，。；！？\n]{0,40}?(?:写入|写进|加进|加到|应用到|更新到|替换到)" + _ZH_OBJECT,
        # The document as the object of a completed change verb ...
        r"I have (?:updated|modified|edited|revised|rewritten|changed) " + _DOC_NOUN,
        # ... or as the destination of something written. "based on the document"
        # is not a destination, so ``on`` is deliberately absent.
        r"I have (?:drafted|written|inserted|applied|added|put|made)\b[^.\n]{0,60}?"
        r"\b(?:in|into|to) " + _DOC_NOUN,
        # A passive with a named agent other than the speaker is a report about
        # someone else's edit, not a claim of one's own.
        r"(?:document|spreadsheet|deck|sheet) (?:has been|was) (?:updated|modified|edited)"
        r"(?!\s+by\s+(?!me\b))",
    )),
    re.IGNORECASE,
)

_ANNOTATION = (
    "\n\n⚠️ 对账提示：本回合没有产生任何文档写入——如上文声称已修改文档，"
    "该修改并未发生，请勿据此认为文档已更新。"
)


class ReportLedgerRail(DeepAgentRail):
    """Count writes as they happen; audit the closing claim against the count."""

    def __init__(self) -> None:
        super().__init__()
        self._touched = False
        self._writes = 0

    async def before_invoke(self, ctx) -> None:  # noqa: D102 - per-turn reset
        self._touched = False
        self._writes = 0

    async def after_tool_call(self, ctx) -> None:  # noqa: D102
        name = str(getattr(ctx.inputs, "tool_name", "") or "")
        if not name.startswith("clouddoc_"):
            return
        self._touched = True
        if name not in _WRITE_TOOLS:
            return
        result = getattr(ctx.inputs, "tool_result", None)
        ok = bool(result.get("ok")) if isinstance(result, dict) else "\"ok\": true" in str(result or "").lower()
        if ok:
            self._writes += 1

    async def after_invoke(self, ctx) -> None:  # noqa: D102
        try:
            if not self._touched or self._writes:
                return
            result = getattr(ctx.inputs, "result", None)
            if not isinstance(result, dict):
                return
            output = result.get("output")
            if not isinstance(output, str) or not _CLAIMS_DOC_MODIFIED.search(output):
                return
            result["output"] = output + _ANNOTATION
            ctx.extra["_report_ledger_flag"] = True
            logger.warning(
                "[clouddoc] 对账不符（聊天路径）：声称已修改文档但本回合零写入"
            )
        except Exception:  # noqa: BLE001 - annotation duty must not fail the turn
            logger.exception("[clouddoc] 报告对账 rail 未完成")
