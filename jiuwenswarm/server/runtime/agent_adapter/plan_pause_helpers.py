# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for plan-mode cancel (pause) and LLM-driven resume vs new-task routing."""

from __future__ import annotations

import json
import logging
from typing import Any
from pathlib import Path

from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.single_agent import create_agent_session
from openjiuwen.harness.schema.state import DeepAgentState
from openjiuwen.harness.schema.task import TodoItem, TodoStatus


PlanTaskStatus = TodoStatus

logger = logging.getLogger(__name__)


def resolve_context_engine(agent: Any) -> Any:
    """Resolve context_engine from DeepAgent or nested react_agent."""
    engine = getattr(agent, "context_engine", None)
    if engine is not None:
        return engine
    react_agent = getattr(agent, "react_agent", None) or getattr(agent, "_react_agent", None)
    if react_agent is not None:
        return getattr(react_agent, "context_engine", None)
    return None


def resolve_actual_session(session: Any) -> Any:
    """Unwrap sub-session to the parent session when present."""
    if session is None:
        return None
    return getattr(session, "_parent", session) or session


def session_id_from_session(session: Any) -> str:
    """Best-effort session id for rails / tools (multi-session safe lookups)."""
    getter = getattr(session, "get_session_id", None)
    if callable(getter):
        return str(getter() or "")
    sid = getattr(session, "session_id", None)
    if callable(sid):
        return str(sid() or "")
    return str(sid or "")


PLAN_PAUSED_SESSION_KEY = "jiuwenclaw_plan_paused"
PLAN_PAUSED_SNAPSHOT_KEY = "jiuwenclaw_plan_paused_snapshot"
INTERRUPT_ARTIFACTS_SUMMARY_KEY = "jiuwenclaw_interrupt_artifacts_summary"
# 生命周期：运行期 list[dict]（tool 产物日志，task_execution_rail 追加）
# → 取消时 str（格式化摘要文本，_persist_interrupt_artifacts_summary 转换）
# → 注入后 None（clear_interrupt_artifacts_summary_from_session 清空）
INTERRUPT_RECOVERY_INJECTED_KEY = "jiuwenclaw_interrupt_recovery_injected"

PAUSED_PLAN_DECISION_PROMPT_CN = """【系统：plan 暂停后的用户消息】
用户已 cancel；**未完成**待办已从 todo 文件移除（`todo_list` 通常仅含 completed）。取消时的计划状态见下方快照。

请先 `todo_list`，再结合**对话历史**、completed、快照与本条 **content**，**由你自行判断**意图并选用 todo 工具（勿依赖服务端预设结论）：

1. **续跑**：继续暂停前的同一目标 → 按快照用 `todo_create` / `todo_modify` 恢复未完成项（勿无谓整表覆盖 completed），再 `todo_start`；已完成项默认跳过，除非用户要求重做。
2. **全新任务**：与暂停计划无关的新需求 → 只为本条 `todo_create`；**不要**执行快照中的旧目标。
3. **调整原任务**：仍做同一目标但变更范围、约束或步骤 → `todo_modify` 更新计划后再 `todo_start`。
4. **非工作任务**：自然回复即可；**不要** `todo_start` 快照旧步骤，**不要** 为无实质工作内容的 message 建 todo 计划。

不要因刚 cancel 就默认续跑；不要同时推进快照旧目标与本条无关的新目标。
{snapshot_block}"""

PAUSED_PLAN_DECISION_PROMPT_EN = """[System: message after plan pause]
The user cancelled; **unfinished** todos were removed from the todo file (`todo_list` is usually completed-only). See the snapshot below for state at cancel.

Call `todo_list`, then using **chat history**, completed items, the snapshot, and this message's **content**, **decide yourself** the intent and which todo tools to use:

1. **Resume** the same goal: restore unfinished steps from the snapshot via `todo_create` / `todo_modify`, then `todo_start`; keep completed unless redo is needed.
2. **New task**: unrelated to the paused plan — `todo_create` for this content only; do not run old snapshot goals.
3. **Adjust the original task**: same goal but changed scope/constraints/steps — `todo_modify`, then `todo_start`.
4. **Non-work message**: reply naturally; do not `todo_start` old snapshot steps or create a work plan.

Do not assume resume because of a recent cancel; do not run old snapshot goals together with unrelated new content.
{snapshot_block}"""


def _format_snapshot_block(language: str, snapshot: str) -> str:
    text = snapshot.strip()
    if not text:
        return ""
    if language in ("en", "english"):
        return f"\nPlan state at cancel (reference):\n{text}\n"
    return f"\n取消时计划状态（参考）：\n{text}\n"


def build_paused_plan_decision_prompt(language: str, *, snapshot: str = "", has_new_file: bool = False) -> str:
    snapshot_block = _format_snapshot_block(language, snapshot)
    template = (
        PAUSED_PLAN_DECISION_PROMPT_EN
        if language in ("en", "english")
        else PAUSED_PLAN_DECISION_PROMPT_CN
    )
    result = template.format(snapshot_block=snapshot_block)
    if has_new_file:
        if language in ("en", "english"):
            result += ("\n\n⚠️ Note: The user uploaded a new file with this message. "
                       "This is likely a new task (option 2), not a resume of the old plan (option 1). "
                       "Unless the user explicitly says to continue the old task, prioritize the new file.")
        else:
            result += ("\n\n⚠️ 注意：用户本次上传了新文件，这很可能是一个全新任务（选项 2），而非续跑旧计划（选项 1）。"
                       "除非用户明确表示要继续旧任务，否则应优先处理新上传的文件。")
    return result


def build_paused_plan_decision_prompt_from_session_snapshot(
    language: str,
    snapshot: dict[str, Any] | None,
    has_new_file: bool = False,
) -> str:
    return build_paused_plan_decision_prompt(
        language,
        snapshot=format_plan_pause_handoff_snapshot(snapshot),
        has_new_file=has_new_file,
    )


def read_plan_pause_from_session(session: Any) -> tuple[bool, dict[str, Any] | None]:
    """Read persisted plan_paused flag and optional snapshot from session state."""
    paused = session.get_state(PLAN_PAUSED_SESSION_KEY)
    if paused is not True:
        return False, None
    snapshot = session.get_state(PLAN_PAUSED_SNAPSHOT_KEY)
    if isinstance(snapshot, dict):
        return True, snapshot
    return True, None


def write_plan_pause_to_session(
    session: Any,
    *,
    paused: bool,
    snapshot: dict[str, Any] | None = None,
) -> None:
    """Write plan_paused flag (and optional snapshot) into session state."""
    updates: dict[str, Any] = {PLAN_PAUSED_SESSION_KEY: paused}
    if snapshot is not None:
        updates[PLAN_PAUSED_SNAPSHOT_KEY] = snapshot
    elif not paused:
        updates[PLAN_PAUSED_SNAPSHOT_KEY] = None
    session.update_state(updates)


def clear_plan_pause_on_session(session: Any) -> None:
    write_plan_pause_to_session(session, paused=False, snapshot=None)


def _session_id_from_session(session: Any) -> str:
    return session_id_from_session(session)


async def _resolve_session_for_checkpoint(
    instance: Any,
    session_id: str,
    *,
    card: Any,
) -> tuple[Any, bool]:
    """Return (session, owned). owned=True means caller must pre_run/post_run."""
    loop_session = getattr(instance, "loop_session", None)
    if loop_session is not None and _session_id_from_session(loop_session) == session_id:
        return loop_session, False
    session = create_agent_session(session_id=session_id, card=card)
    return session, True


async def persist_checkpoint_for_session(
    instance: Any,
    session_id: str,
    *,
    card: Any,
    session: Any | None = None,
) -> None:
    """Persist context + agent state before abort (mirrors StreamEventRail early checkpoint)."""
    if not session_id or instance is None:
        return

    context_engine = resolve_context_engine(instance)
    if context_engine is None:
        logger.error(
            "[plan_pause] skip pre-abort checkpoint: no context_engine session_id=%s",
            session_id,
        )
        return

    owned = False
    reused_session = session is not None
    if session is None:
        session, owned = await _resolve_session_for_checkpoint(instance, session_id, card=card)
    try:
        if owned:
            await session.pre_run(inputs=None)
        actual_session = getattr(session, "_parent", session) or session
        await context_engine.save_contexts(actual_session)
        await post_agent_execute_for_session(session)
        logger.info(
            "[plan_pause] pre-abort checkpoint saved session_id=%s owned=%s reused_session=%s",
            session_id,
            owned,
            reused_session,
        )
    except Exception as exc:
        logger.error(
            "[plan_pause] pre-abort checkpoint failed session_id=%s: %s",
            session_id,
            exc,
            exc_info=True,
        )
    finally:
        if owned:
            await session.post_run()


async def post_agent_execute_for_session(session: Any, checkpointer: Any | None = None) -> None:
    """Flush session state to checkpointer without post_run."""
    actual_session = getattr(session, "_parent", session) or session
    inner = getattr(actual_session, "_inner", actual_session)
    cp = checkpointer if checkpointer is not None else CheckpointerFactory.get_checkpointer()
    await cp.post_agent_execute(inner)


def format_paused_plan_snapshot(todos: list[TodoItem]) -> str:
    if not todos:
        return ""
    lines: list[str] = []
    for index, todo in enumerate(todos, start=1):
        lines.append(f"{index}. [{todo.status.value}] {todo.content}")
    return "\n".join(lines)


def split_todos_for_pause_handoff(
    todos: list[TodoItem],
) -> tuple[list[TodoItem], list[TodoItem]]:
    """Split unfinished todos from completed (used for optional cancel snapshot)."""
    archived: list[TodoItem] = []
    kept: list[TodoItem] = []
    for todo in todos:
        if todo.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS):
            archived.append(todo)
        elif todo.status == TodoStatus.COMPLETED:
            kept.append(todo)
    return archived, kept


def todos_to_snapshot_payload(todos: list[TodoItem]) -> list[dict[str, str]]:
    return [
        {
            "id": todo.id,
            "content": todo.content,
            "status": todo.status.value,
        }
        for todo in todos
    ]


def build_plan_pause_snapshot_payload(
    archived: list[TodoItem],
    kept: list[TodoItem],
) -> dict[str, list[dict[str, str]]]:
    return {
        "completed": todos_to_snapshot_payload(kept),
        "unfinished": todos_to_snapshot_payload(archived),
    }


def format_plan_pause_handoff_snapshot(snapshot: dict[str, Any] | None) -> str:
    if not isinstance(snapshot, dict):
        return ""
    lines: list[str] = []
    completed = snapshot.get("completed")
    if isinstance(completed, list) and completed:
        lines.append("已完成步骤：")
        for index, item in enumerate(completed, start=1):
            if isinstance(item, dict):
                content = str(item.get("content") or "").strip()
                if content:
                    lines.append(f"  {index}. [completed] {content}")
    unfinished = snapshot.get("unfinished")
    if isinstance(unfinished, list) and unfinished:
        lines.append("暂停时未完成步骤：")
        for index, item in enumerate(unfinished, start=1):
            if isinstance(item, dict):
                content = str(item.get("content") or "").strip()
                status = str(item.get("status") or "pending").strip()
                if content:
                    lines.append(f"  {index}. [{status}] {content}")
    return "\n".join(lines)


def repair_task_plan_after_pause(state: DeepAgentState) -> bool:
    """Reset in-progress / abort-cancelled plan steps to pending for resume."""
    plan = state.task_plan
    if plan is None or not plan.tasks:
        return False

    changed = False
    for task in plan.tasks:
        if task.status == PlanTaskStatus.IN_PROGRESS:
            task.status = PlanTaskStatus.PENDING
            changed = True
        elif (
            task.status == TodoStatus.CANCELLED
            and (task.result_summary or "").strip().lower() == "cancelled"
        ):
            task.status = PlanTaskStatus.PENDING
            task.result_summary = ""
            changed = True

    if plan.current_task_id:
        current = plan.get_task(plan.current_task_id)
        if current is not None and current.status == PlanTaskStatus.PENDING:
            plan.current_task_id = None
            changed = True

    return changed


def clear_task_plan_on_state(state: DeepAgentState) -> bool:
    """Clear task_plan so TaskLoop does not auto-bind a stale task_id on next run."""
    if state.task_plan is None:
        return False
    state.task_plan = None
    return True


def pause_todo_items(todos: list[TodoItem]) -> bool:
    """Mark in_progress todos as pending; return True if any item changed."""
    changed = False
    for todo in todos:
        if todo.status == TodoStatus.IN_PROGRESS:
            todo.status = TodoStatus.PENDING
            changed = True
    return changed


async def pause_pending_todos_on_tool(modify_tool: Any, session_id: str) -> bool:
    """Persist in_progress -> pending on the session todo file."""
    todos = await modify_tool.load_todos(session_id)
    if not todos:
        return False
    if not pause_todo_items(todos):
        return False
    await modify_tool.save_todos(session_id, todos)
    return True


async def snapshot_and_isolate_unfinished_todos(modify_tool: Any, session_id: str) -> dict[str, Any] | None:
    """Snapshot cancel state, then remove unfinished todos from file (keep completed only).

    Prevents todo_list from exposing runnable pending items on the next turn, so a
    clearly new user message cannot parallelize with old plan steps via todo_start.
    """
    todos = await modify_tool.load_todos(session_id)
    if not todos:
        return None

    pause_todo_items(todos)
    archived, kept = split_todos_for_pause_handoff(todos)
    snapshot = build_plan_pause_snapshot_payload(archived, kept)

    ids_to_delete: list[str] = []
    seen: set[str] = set()
    for todo in archived:
        if todo.id not in seen:
            seen.add(todo.id)
            ids_to_delete.append(todo.id)
    for todo in todos:
        if todo.status == TodoStatus.CANCELLED and todo.id not in seen:
            seen.add(todo.id)
            ids_to_delete.append(todo.id)

    if ids_to_delete:
        await delete_todos_via_modify_tool(modify_tool, ids_to_delete, session_id=session_id)

    return snapshot


async def delete_todos_via_modify_tool(modify_tool: Any, ids: list[str], *, session_id: str) -> None:
    """Delete todos by id, bypassing ``invoke`` (which expects a session object).

    TodoModifyTool.invoke reads ``kwargs['session']`` (an AgentSession) and
    derives session_id from it; callers in the interrupt-recovery prepare hooks
    only have a session_id string. Use ``load_todos`` + ``_delete_todos``
    directly — same path ``load_todos`` takes, both accept session_id as a
    plain string.
    """
    if not ids:
        return
    current = await modify_tool.load_todos(session_id)
    await modify_tool._delete_todos(session_id, ids, current)  # pylint: disable=protected-access


async def cancel_todos_via_modify_tool(modify_tool: Any, ids: list[str], *, session_id: str) -> None:
    """Cancel todos by id, bypassing ``invoke`` (same reason as delete above)."""
    if not ids:
        return
    current = await modify_tool.load_todos(session_id)
    await modify_tool._cancel_todos(session_id, ids, current)  # pylint: disable=protected-access


async def cancel_pending_todos_on_tool(modify_tool: Any, session_id: str) -> bool:
    """Mark all non-completed todos cancelled (isolate old plan for a new-task turn)."""
    todos = await modify_tool.load_todos(session_id)
    if not todos:
        return False

    done_statuses = {
        TodoStatus.COMPLETED.value,
        TodoStatus.CANCELLED.value,
    }
    ids_to_cancel = [todo.id for todo in todos if todo.status.value not in done_statuses]
    if not ids_to_cancel:
        return False

    await cancel_todos_via_modify_tool(modify_tool, ids_to_cancel, session_id=session_id)
    return True


def merge_supplementary_into_request_params(
    params: dict[str, Any],
    supplementary: str,
) -> None:
    """Append system supplementary text to request.params for build_user_prompt."""
    if not supplementary.strip():
        return
    existing = params.get("supplementary_info")
    if isinstance(existing, str) and existing.strip():
        params["supplementary_info"] = f"{existing.strip()}\n\n{supplementary.strip()}"
    else:
        params["supplementary_info"] = supplementary.strip()


def append_supplementary_to_inputs_query(inputs: dict[str, Any], supplementary: str) -> None:
    """Fallback: inject supplementary into an already-built user prompt JSON payload."""
    if not supplementary.strip():
        return
    query = inputs.get("query")
    if not isinstance(query, str):
        return
    prefixes = ("你收到一条消息：\n", "You receive a new message:\n")
    for prefix in prefixes:
        if not query.startswith(prefix):
            continue
        try:
            payload = json.loads(query[len(prefix):])
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        existing = payload.get("supplementary_info")
        if isinstance(existing, str) and existing.strip():
            payload["supplementary_info"] = f"{existing.strip()}\n\n{supplementary.strip()}"
        else:
            payload["supplementary_info"] = supplementary.strip()
        inputs["query"] = prefix + json.dumps(payload, ensure_ascii=False)
        return


# ---------------------------------------------------------------------------
# Interrupt artifacts summary (fallback when plan_pause / interrupt_resume fail)
# ---------------------------------------------------------------------------

ARTIFACTS_RESUME_PROMPT_CN = """【中断恢复提示】
之前的任务被中断取消。以下是中断前已完成的工作产物摘要：

{summary}

请据此判断当前任务状态：
- 如果产物显示目标文件已存在且内容较完整，请先 read_file 查看当前状态，再决定是补充完善还是从头重建
- 如果产物显示目标文件尚未创建或内容很少，可以重新创建
- 不要盲目从头 write_file 重建一个已存在的完整文件"""

ARTIFACTS_RESUME_PROMPT_EN = """[Interrupt recovery hint]
The previous task was interrupted and cancelled. Here is a summary of completed work artifacts before the interruption:

{summary}

Based on this, judge the current task state:
- If artifacts show the target file already exists with substantial content, read_file first before deciding to supplement or rebuild from scratch
- If artifacts show the target file was not created or has minimal content, you may create it anew
- Do not blindly write_file to rebuild a file that already exists and is complete"""


def build_interrupt_artifacts_resume_prompt(language: str, *, summary: str) -> str:
    template = (
        ARTIFACTS_RESUME_PROMPT_EN
        if language in ("en", "english")
        else ARTIFACTS_RESUME_PROMPT_CN
    )
    return template.format(summary=summary.strip() or "(empty)")


def write_interrupt_artifacts_summary_to_session(session: Any, summary: str) -> None:
    """Write interrupt artifacts summary into session state (one-shot, cleared after use)."""
    session.update_state({INTERRUPT_ARTIFACTS_SUMMARY_KEY: summary})


def read_interrupt_artifacts_summary_from_session(session: Any) -> str | None:
    """Read interrupt artifacts summary from session state."""
    value = session.get_state(INTERRUPT_ARTIFACTS_SUMMARY_KEY)
    if isinstance(value, str) and value.strip():
        return value
    return None


def clear_interrupt_artifacts_summary_from_session(session: Any) -> None:
    """Clear interrupt artifacts summary from session state (after injection)."""
    session.update_state({INTERRUPT_ARTIFACTS_SUMMARY_KEY: None})


# ---------------------------------------------------------------------------
# Unified sentinel: prevents multiple recovery mechanisms from injecting
# into the same request cycle
# ---------------------------------------------------------------------------

def is_interrupt_recovery_injected(session: Any) -> bool:
    """Check whether any interrupt recovery mechanism has already injected a prompt."""
    return bool(session.get_state(INTERRUPT_RECOVERY_INJECTED_KEY))


def mark_interrupt_recovery_injected(session: Any) -> None:
    """Mark that an interrupt recovery prompt has been injected for this request."""
    session.update_state({INTERRUPT_RECOVERY_INJECTED_KEY: True})


def clear_interrupt_recovery_injected(session: Any) -> None:
    """Clear the recovery injected marker (for new conversation cycles)."""
    session.update_state({INTERRUPT_RECOVERY_INJECTED_KEY: None})


# ---------------------------------------------------------------------------
# Recovery file on disk (fallback when session state is lost, e.g. process restart)
# ---------------------------------------------------------------------------

_RECOVERY_DIR_NAME = "recovery"
_RECOVERY_FILE_NAME = "recovery.json"
_PLAN_PAUSE_FILE_NAME = "plan_pause.json"


def _get_recovery_dir(workspace_dir: Path, session_id: str) -> Path:
    """Get the recovery directory for a given session.

    Path: <workspace_dir>/context/<session_id>/recovery/
    """
    return workspace_dir / "context" / session_id / _RECOVERY_DIR_NAME


def write_plan_pause_to_file(
    workspace_dir: Path,
    session_id: str,
    snapshot: dict[str, Any] | None = None,
) -> None:
    """Persist plan_paused to disk so a later live checkpoint overwrite cannot drop it."""
    if not session_id:
        return
    recovery_dir = _get_recovery_dir(workspace_dir, session_id)
    recovery_dir.mkdir(parents=True, exist_ok=True)
    file_path = recovery_dir / _PLAN_PAUSE_FILE_NAME
    try:
        with file_path.open("w", encoding="utf-8") as fh:
            json.dump(
                {"paused": True, "snapshot": snapshot, "session_id": session_id},
                fh,
                ensure_ascii=False,
            )
    except Exception:
        logger.warning("[recovery] write_plan_pause_to_file failed session=%s", session_id)


def read_plan_pause_from_file(
    workspace_dir: Path,
    session_id: str,
) -> tuple[bool, dict[str, Any] | None]:
    """Read plan_paused flag/snapshot from disk. Returns (False, None) when absent."""
    file_path = _get_recovery_dir(workspace_dir, session_id) / _PLAN_PAUSE_FILE_NAME
    try:
        if not file_path.exists():
            return False, None
        with file_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("paused") is not True:
            return False, None
        snapshot = data.get("snapshot")
        if isinstance(snapshot, dict):
            return True, snapshot
        return True, None
    except Exception:
        logger.debug("[recovery] read_plan_pause_from_file failed session=%s", session_id)
        return False, None


def clear_plan_pause_file(workspace_dir: Path, session_id: str) -> None:
    """Delete the plan_pause recovery file after successful decision-prompt injection."""
    file_path = _get_recovery_dir(workspace_dir, session_id) / _PLAN_PAUSE_FILE_NAME
    try:
        if file_path.exists():
            file_path.unlink()
    except Exception:
        logger.debug("[recovery] clear_plan_pause_file failed session=%s", session_id)


def write_interrupt_artifacts_to_file(workspace_dir: Path, session_id: str, summary: str) -> None:
    """Write interrupt artifacts summary to a JSON file on disk (process-restart safe)."""
    if not summary.strip():
        return
    recovery_dir = _get_recovery_dir(workspace_dir, session_id)
    recovery_dir.mkdir(parents=True, exist_ok=True)
    file_path = recovery_dir / _RECOVERY_FILE_NAME
    try:
        json.dump({"summary": summary, "session_id": session_id}, file_path.open("w", encoding="utf-8"))
    except Exception:
        logger.warning("[recovery] write_interrupt_artifacts_to_file failed session=%s", session_id)


def read_interrupt_artifacts_from_file(workspace_dir: Path, session_id: str) -> str | None:
    """Read interrupt artifacts summary from the JSON file on disk.

    Returns the summary string, or None if the file doesn't exist or is invalid.
    """
    file_path = _get_recovery_dir(workspace_dir, session_id) / _RECOVERY_FILE_NAME
    try:
        if not file_path.exists():
            return None
        data = json.load(file_path.open(encoding="utf-8"))
        summary = data.get("summary", "")
        if isinstance(summary, str) and summary.strip():
            return summary
    except Exception:
        logger.debug("[recovery] read_interrupt_artifacts_from_file failed session=%s", session_id)
    return None


def clear_interrupt_artifacts_file(workspace_dir: Path, session_id: str) -> None:
    """Delete the recovery file for a session (after successful injection)."""
    file_path = _get_recovery_dir(workspace_dir, session_id) / _RECOVERY_FILE_NAME
    try:
        if file_path.exists():
            file_path.unlink()
    except Exception:
        logger.debug("[recovery] clear_interrupt_artifacts_file failed session=%s", session_id)


def clear_session_interrupt_state(session: Any) -> None:
    """Clear persisted interrupt-related state for one session."""
    if session is None:
        return
    try:
        from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

        session.update_state({INTERRUPTION_KEY: None})
    except Exception:
        logger.debug(
            "[plan_pause] clear_session_interrupt_state failed (session may lack INTERRUPTION_KEY)",
            exc_info=True,
        )
