# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Graceful finalization when the ReAct budget is exhausted.

``react_agent`` returns a bare ``Max iterations reached without completion``
error after the final tool result has already been written.  This rail:

* injects a finalization-budget reminder for the last few iterations so the
  model saves its work and answers before the budget runs out.  The reminder
  is a per-call prompt attachment (tail user message), not a system prompt
  section, so the cached prompt prefix stays stable;
* tracks document state reported by tools: the live revision from
  ``<session-meta>`` and the last confirmed save receipt;
* on ``AFTER_REACT_ITERATION`` of the final allowed iteration requests a
  force-finish with an actionable message that states truthfully whether the
  edited document was saved.  The ReAct for-else consumes that request, so no
  change to the openjiuwen dependency is required.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentKind
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import logger

_BUDGET_SECTION = "task_finalization_budget"
_BUDGET_SOURCE = "last_iteration_finalize_rail"
_MAX_RESERVE = 15
_MIN_RESERVE = 2
_SESSION_META_RE = re.compile(r"<session-meta>(.*?)</session-meta>", re.DOTALL)
_MAX_UNWRAP_DEPTH = 8
_MAX_TRACKED_SESSIONS = 256

# Tools whose successful result is a trusted file-save receipt.
DEFAULT_SAVE_TOOL_NAMES: tuple[str, ...] = ("save_pptx", "execute_fill_plan")
# Tools that bind a document; their payload revision is the document's state
# at bind time (a reused live document may already be at revision N).
_BIND_TOOL_NAMES = frozenset({"open_pptx", "switch_file"})
# Local tools that echo arbitrary text (files, shell output); their output is
# never trusted as document state even if it contains receipt-like JSON.
_UNTRUSTED_TEXT_TOOLS = frozenset({
    "bash", "read_file", "write_file", "edit_file", "grep", "glob", "ls",
    "list_files", "tools_search", "skill_tool", "web_fetch", "web_search",
})


@dataclass
class _DocState:
    document_id: str
    path: str | None = None
    live_revision: int | None = None
    saved_revision: int | None = None
    saved_path: str | None = None
    baseline_revision: int | None = None

    def observe(self, revision: int) -> None:
        if self.baseline_revision is None:
            self.baseline_revision = revision

    @property
    def has_unsaved_edits(self) -> bool:
        if self.live_revision is None or self.live_revision <= 0:
            return False
        known = [r for r in (self.saved_revision, self.baseline_revision) if r is not None]
        return not known or self.live_revision > max(known)


@dataclass
class _SessionState:
    docs: dict[str, _DocState] = field(default_factory=dict)
    last_document_id: str | None = None

    def begin_run(self) -> None:
        self.last_document_id = None

    def touch(self, document_id: str) -> _DocState:
        doc = self.docs.get(document_id)
        if doc is None:
            doc = self.docs[document_id] = _DocState(document_id=document_id)
        self.last_document_id = document_id
        return doc

    @property
    def last_doc(self) -> _DocState | None:
        return self.docs.get(self.last_document_id) if self.last_document_id else None


class LastIterationFinalizeRail(DeepAgentRail):
    """Return an actionable partial-result message instead of a bare error."""

    priority: int = 96

    def __init__(
        self,
        *,
        language: str = "cn",
        save_tool_names: Iterable[str] = DEFAULT_SAVE_TOOL_NAMES,
    ) -> None:
        super().__init__()
        self._language = "en" if str(language).lower().startswith("en") else "cn"
        self._save_tool_names = frozenset(save_tool_names)
        self._max_iterations: int | None = None
        # One adapter-level rail instance serves every session of the agent,
        # so document state is keyed by session id.  Invoke-level hooks run on
        # the outer agent and may not carry the session, so a run boundary is
        # detected from react_iteration == 1 (a resume starts later).  State is
        # never dropped in after_invoke: a streaming invoke that pauses on a
        # permission interrupt does not surface result_type=interrupt there, and
        # dropping would lose the save receipt before the resume.
        self._sessions: dict[str, _SessionState] = {}

    def set_language(self, language: str | None) -> None:
        self._language = "en" if str(language or "").lower().startswith("en") else "cn"

    # ------------------------------------------------------------------ hooks

    async def before_invoke(self, ctx: Any) -> None:
        # A permission-interrupt resume continues the same run (same iteration
        # budget, InteractiveInput query).  A fresh text query starts a new run.
        session_id = self._session_id(ctx)
        if session_id and self._is_fresh_query(ctx):
            self._begin_run(session_id)
        self._max_iterations = self._read_max_iterations(ctx)
        if self._max_iterations is None:
            logger.warning(
                "[LastIterationFinalizeRail] max_iterations unavailable; "
                "graceful finalize disabled"
            )

    async def after_invoke(self, ctx: Any) -> None:
        session_id = self._session_id(ctx)
        if not session_id:
            return
        # Document state is kept: whether this invoke paused on an interrupt is
        # not reliably visible here (streaming invokes), and the next run resets
        # it in _begin_run anyway.
        await self._clear_budget_attachment(ctx, session_id)

    async def before_model_call(self, ctx: Any) -> None:
        session_id = self._session_id(ctx)
        maximum = self._resolve_max_iterations(ctx)
        try:
            iteration = int(getattr(getattr(ctx, "inputs", None), "react_iteration", 0) or 0)
        except (TypeError, ValueError):
            iteration = 0
        if iteration == 1:
            # First iteration of a fresh run (a resume starts later).
            self._begin_run(session_id)
        state = self._sessions.get(session_id)
        if not isinstance(maximum, int) or maximum < 5 or iteration <= 0:
            await self._clear_budget_attachment(ctx, session_id)
            return
        remaining = maximum - iteration + 1  # including the current iteration
        if remaining > self._reserve(maximum):
            await self._clear_budget_attachment(ctx, session_id)
            return
        manager = self._attachment_manager(ctx)
        if manager is None or not session_id:
            return
        try:
            await manager.add_section(
                session_id=session_id,
                section=_BUDGET_SECTION,
                kind=PromptAttachmentKind.RUNTIME,
                content=self._budget_content(iteration, maximum, remaining, state),
                priority=self.priority,
                source=_BUDGET_SOURCE,
                metadata={"iteration": iteration, "max_iterations": maximum},
                content_kind="text/markdown",
            )
        except Exception as exc:  # noqa: BLE001 - reminder is best-effort
            logger.warning("[LastIterationFinalizeRail] budget attachment failed: %s", exc)

    async def after_tool_call(self, ctx: Any) -> None:
        inputs = getattr(ctx, "inputs", None)
        if inputs is None:
            return
        name = self._effective_tool_name(inputs)
        if not name or name in _UNTRUSTED_TEXT_TOOLS:
            return
        result = getattr(inputs, "tool_result", None)
        if result is None or isinstance(result, BaseException) or _is_failure(result):
            return
        state = self._state(ctx)
        payloads, metas = _collect_payloads(result)
        for meta in metas:
            self._apply_session_meta(state, meta)
        for payload in payloads:
            self._apply_document_payload(state, payload, bind=name in _BIND_TOOL_NAMES)
            if name in self._save_tool_names:
                self._apply_save_receipt(state, payload)

    async def after_react_iteration(self, ctx: Any) -> None:
        inputs = getattr(ctx, "inputs", None)
        try:
            iteration = int(getattr(inputs, "react_iteration", 0) or 0)
        except (TypeError, ValueError):
            return
        state = self._sessions.get(self._session_id(ctx))
        max_iterations = self._resolve_max_iterations(ctx)
        if not isinstance(max_iterations, int) or max_iterations <= 0:
            return
        # react_iteration is 1-based.  AFTER_REACT_ITERATION carries
        # ModelCallInputs (not ToolCallInputs); a text-only final answer
        # breaks the loop before this hook fires, so response.tool_calls
        # is the completion signal available on this event.
        if iteration < max_iterations:
            return
        response = getattr(inputs, "response", None)
        if response is not None:
            tool_calls = getattr(response, "tool_calls", None)
            if tool_calls is None and isinstance(response, dict):
                tool_calls = response.get("tool_calls")
            if not tool_calls:
                return

        # 临时手段：``max_iterations_reached`` 前缀是给下游按文本归类错误用的标记。
        # 本改动已把 finish_reason 透传为 chat.error.code，但现网下游分类器尚无
        # 按 errorCode == "max_iterations" 的精确分支，仍依赖正则
        # /max iterations reached|max_iterations_reached/i 命中文案，故前缀暂留。
        # Web 展示路径会剥离该前缀，避免英文标记暴露给终端用户。
        # 移除条件：待下游分类器增加该 code 分支后，删除此前缀，恢复纯中文/英文用户文案。
        if self._language == "en":
            output = (
                "max_iterations_reached: The maximum number of reasoning "
                f"iterations ({iteration}/{max_iterations}) was reached and the "
                "task did not complete.\nNarrow the task scope or provide the "
                "missing data source, then retry."
            )
        else:
            output = (
                f"max_iterations_reached：已达到最大思考轮数"
                f"（{iteration}/{max_iterations}），任务未完成。\n"
                "建议：缩小任务范围，或补充数据源后重试。"
            )
        output += self._document_status_lines(state)

        doc = state.last_doc if state else None
        logger.warning(
            "[LastIterationFinalizeRail] final iteration session_id=%s "
            "iteration=%s/%s document=%s live_revision=%s saved_revision=%s",
            self._session_id(ctx),
            iteration,
            max_iterations,
            doc.document_id if doc else None,
            doc.live_revision if doc else None,
            doc.saved_revision if doc else None,
        )
        ctx.request_force_finish({"output": output, "result_type": "error", "finish_reason": "max_iterations"})

    # ---------------------------------------------------------- doc tracking

    @staticmethod
    def _apply_session_meta(state: _SessionState, meta: dict) -> None:
        document_id = meta.get("documentId")
        revision = _as_int(meta.get("revision"))
        if not isinstance(document_id, str) or not document_id or revision is None:
            return
        doc = state.touch(document_id)
        doc.observe(revision)
        doc.live_revision = revision

    @staticmethod
    def _apply_document_payload(state: _SessionState, payload: dict, *, bind: bool) -> None:
        # open_pptx / switch_file / save_pptx report the bound document and path.
        # Live revision is tracked from <session-meta> only: a bind payload's
        # revision is the document's state when bound (baseline), and a late
        # payload must not roll back the live revision.
        document_id = payload.get("documentId")
        if not isinstance(document_id, str) or not document_id:
            return
        doc = state.touch(document_id)
        path = payload.get("workingPath")
        if isinstance(path, str) and path:
            doc.path = path
        revision = _as_int(payload.get("revision"))
        if bind and revision is not None:
            doc.observe(revision)

    @staticmethod
    def _apply_save_receipt(state: _SessionState, payload: dict) -> None:
        if payload.get("status") == "saved" and payload.get("workingPath"):
            # save_pptx: {"status":"saved","documentId","revision","workingPath",...}
            saved_path, revision = payload["workingPath"], payload.get("revision")
        else:
            # execute_fill_plan: {"saved": {"path", "revision"}}
            saved = payload.get("saved")
            if not isinstance(saved, dict) or not saved.get("path"):
                return
            saved_path, revision = saved["path"], saved.get("revision")
        document_id = payload.get("documentId") or state.last_document_id or "__saved__"
        doc = state.touch(str(document_id))
        doc.saved_path = str(saved_path)
        saved_revision = _as_int(revision)
        if saved_revision is None:
            # 回执未带修订号：保存工具写的是当前 Live 状态，视为已保存到当前修订，
            # 避免同时得出「已保存」和「有未保存修改」两个结论。
            saved_revision = doc.live_revision if doc.live_revision is not None else doc.baseline_revision
        doc.saved_revision = saved_revision
        if doc.live_revision is None:
            doc.live_revision = saved_revision

    # --------------------------------------------------------------- texts

    def _document_status_lines(self, state: _SessionState | None) -> str:
        doc = state.last_doc if state else None
        if doc is None:
            return ""
        en = self._language == "en"
        lines: list[str] = []
        if doc.saved_path:
            # Relay's error transform matches this exact phrase; keep it stable.
            # The revision part is omitted when the receipt carried none.
            if en:
                version = f" (revision {doc.saved_revision})" if doc.saved_revision is not None else ""
                lines.append(
                    f"Last confirmed saved draft: {doc.saved_path}{version}. "
                    "Final checks may remain; continue the unfinished work."
                )
            else:
                version = f"（版本 {doc.saved_revision}）" if doc.saved_revision is not None else ""
                lines.append(
                    f"最近一次已确认保存的草稿：{doc.saved_path}{version}。"
                    "最终检查可能尚未完成，可继续处理未完成项。"
                )
        if doc.has_unsaved_edits:
            where = doc.path or doc.saved_path
            if en:
                location = f" ({where})" if where else ""
                lines.append(
                    f"Unsaved edits in the open document{location}: revision {doc.live_revision} "
                    "has not been written to the file. Save it before closing the preview, "
                    "or reply to continue so the save and remaining work can be finished."
                )
            else:
                location = f"（{where}）" if where else ""
                lines.append(
                    f"编辑中的文稿{location}有未保存的修改：版本 {doc.live_revision} 尚未写入文件。"
                    "关闭预览前请先保存，或回复“继续”完成保存和剩余工作。"
                )
        return "".join(f"\n{line}" for line in lines)

    def _budget_content(
        self, iteration: int, maximum: int, remaining: int, state: _SessionState | None
    ) -> str:
        doc = state.last_doc if state else None
        unsaved = doc is not None and doc.has_unsaved_edits
        if self._language == "en":
            text = (
                "## Iteration budget: finalize now\n"
                f"This is iteration {iteration} of {maximum}; {remaining} remain including this one. "
                "When the budget runs out the task is stopped and you can no longer save or answer.\n"
                "- Stop optional optimization, exploration and tool-source reading.\n"
                "- If files have unsaved edits, call the matching save tool first.\n"
                "- Then answer in text: what is done, what is not, and where the output is.\n"
                "- A saved file alone does not prove final checks passed; say which checks remain."
            )
            if unsaved:
                text += (
                    f"\nCurrent state: the open document has unsaved edits (revision {doc.live_revision})."
                )
            if remaining <= 1:
                text += (
                    "\nThis is the last iteration: if unsaved edits remain, only save; "
                    "otherwise do not call tools and give the text answer now."
                )
            return text
        text = (
            "## 轮数收尾预算\n"
            f"本次任务最多 {maximum} 轮，当前第 {iteration} 轮，含本轮还剩 {remaining} 轮。"
            "轮数用尽时任务会被强制结束，届时无法再保存或答复。\n"
            "- 停止非必要的优化、探索和工具源码阅读。\n"
            "- 若有未保存的文件修改，先调用对应的保存工具。\n"
            "- 然后用文字说明：已完成什么、未完成什么、产物在哪里。\n"
            "- 已保存不等于已通过最终检查，需说明哪些检查尚未完成。"
        )
        if unsaved:
            text += f"\n当前状态：打开的文稿有未保存的修改（版本 {doc.live_revision}）。"
        if remaining <= 1:
            text += "\n这是最后一轮：若仍有未保存的修改，本轮只做保存；否则不要再调用工具，直接给出文字总结。"
        return text

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _reserve(maximum: int) -> int:
        return min(_MAX_RESERVE, max(_MIN_RESERVE, maximum // 5))

    def _resolve_max_iterations(self, ctx: Any) -> int | None:
        return self._read_max_iterations(ctx) or self._max_iterations

    def _state(self, ctx: Any) -> _SessionState:
        session_id = self._session_id(ctx)
        state = self._sessions.get(session_id)
        if state is None:
            if len(self._sessions) >= _MAX_TRACKED_SESSIONS:
                # Session state is kept between runs; bound the per-instance map.
                self._sessions.pop(next(iter(self._sessions)))
            state = self._sessions[session_id] = _SessionState()
        return state

    def _begin_run(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is not None:
            state.begin_run()

    @staticmethod
    def _attachment_manager(ctx: Any) -> Any:
        return getattr(getattr(ctx, "agent", None), "prompt_attachment_manager", None)

    async def _clear_budget_attachment(self, ctx: Any, session_id: str) -> None:
        manager = self._attachment_manager(ctx)
        if manager is None or not session_id:
            return
        try:
            await manager.clear_section(session_id=session_id, section=_BUDGET_SECTION)
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
            logger.warning("[LastIterationFinalizeRail] budget attachment clear failed: %s", exc)

    @staticmethod
    def _is_fresh_query(ctx: Any) -> bool:
        extra = getattr(ctx, "extra", None)
        if isinstance(extra, dict) and extra.get("_resume_continuation"):
            return False
        query = getattr(getattr(ctx, "inputs", None), "query", None)
        return isinstance(query, str) and bool(query)

    @staticmethod
    def _effective_tool_name(inputs: Any) -> str:
        call = getattr(inputs, "tool_call", None)
        name = str(getattr(call, "name", "") or getattr(inputs, "tool_name", "") or "")
        if name != "invoke_tool":
            # MCP tools may be exposed as "<server>__<tool>" or "<server>.<tool>".
            return re.split(r"__|\.", name)[-1]
        arguments = getattr(call, "arguments", None)
        if arguments is None:
            arguments = getattr(inputs, "tool_args", None)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                return ""
        if isinstance(arguments, dict):
            return str(arguments.get("tool_name") or "")
        return ""

    @staticmethod
    def _read_max_iterations(ctx: Any) -> int | None:
        agent = getattr(ctx, "agent", None)
        for holder in (getattr(agent, "_react_agent", None), agent):
            for attr in ("_config", "config"):
                value = getattr(getattr(holder, attr, None), "max_iterations", None)
                if isinstance(value, int) and value > 0:
                    return value
        value = getattr(agent, "max_iterations", None)
        return value if isinstance(value, int) and value > 0 else None

    @staticmethod
    def _session_id(ctx: Any) -> str:
        session = getattr(ctx, "session", None)
        try:
            return str(session.get_session_id() or "") if session is not None else ""
        except Exception:  # noqa: BLE001
            return ""


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _is_failure(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("success") is False or value.get("isError"):
        return True
    return str(value.get("status") or "").lower() in ("error", "failed", "failure")


def _collect_payloads(value: Any) -> tuple[list[dict], list[dict]]:
    """Peel tool-result envelopes into JSON payload dicts and session-meta dicts."""
    payloads: list[dict] = []
    metas: list[dict] = []

    def visit(item: Any, depth: int) -> None:
        if depth > _MAX_UNWRAP_DEPTH or item is None:
            return
        if isinstance(item, str):
            for raw in _SESSION_META_RE.findall(item):
                meta = _loads_dict(raw)
                if meta is not None:
                    metas.append(meta)
            body = _loads_dict(_SESSION_META_RE.sub("", item))
            if body is not None:
                visit(body, depth + 1)
            return
        if isinstance(item, dict):
            if _is_failure(item) or (item.get("error") and not item.get("success")):
                return
            payloads.append(item)
            for key in ("result", "data", "content"):
                if key in item:
                    visit(item[key], depth + 1)
            return
        if isinstance(item, list):
            for block in item:
                if isinstance(block, dict) and block.get("type") == "text":
                    visit(block.get("text"), depth + 1)
                elif isinstance(block, (dict, str)):
                    visit(block, depth + 1)

    visit(value, 0)
    return payloads, metas


def _loads_dict(text: str) -> dict | None:
    try:
        parsed = json.loads(text.strip())
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = ["DEFAULT_SAVE_TOOL_NAMES", "LastIterationFinalizeRail"]
