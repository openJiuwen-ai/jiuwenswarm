# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ToolIdempotencyRail: serve repeated tool calls from a session cache.

A15 (2026-09-17 consumption audit). Two measured behaviours motivate this
rail:

* ``view_task`` invoked with no selector (``{"action": "list"}``) is issued
  227 / 138 / 134 / 187 times per audit line, and 90%+ of those calls are
  repeats inside the same session while the board did not change.
* the leader uses ``send_message`` as a heartbeat: when a member running a
  long task does not answer, the leader re-sends, and after a context replay
  it cannot tell whether it already sent.

The framework's ``AbilityManager._railed_execute_single_tool_call`` consults
``ctx.extra["_skip_tool"]`` *after* the ``BEFORE_TOOL_CALL`` hooks and, when
set, returns ``ctx.inputs.tool_result`` / ``ctx.inputs.tool_msg`` **without
executing the downstream tool**. That is the tool-call boundary. This rail
hangs off it and replays a cached result for a repeated call.

**Scope of the saving (do not overstate it).** This rail removes

  (a) the downstream side effect (a duplicate delivery, a duplicate board
      read plus its rendering), and
  (b) the tool-result payload that would be written back into the *next*
      model call.

It does **not** remove the model round trip that decided to call the tool.
That call already happened before this rail sees ``BEFORE_TOOL_CALL``. The
only component here that can reduce *wasted* round trips is the
``before_model_call`` nudge, and it does so by changing what the model does
next, not by shortening the current iteration.

Measured on the audit logs (2026-09-17, four lines):

* exactly-duplicated ``send_message`` content is **0.0%** of sends, so the
  content-hash rule is a correctness guard, not a saving;
* ``read_file`` re-reads the same path >=3 times per session in 14-31% of
  calls, and because the rule compares content hashes it never serves a stale
  body;
* only **34-44%** of consecutive ``view_task`` list snapshots are unchanged
  once the ever-changing relative-time rendering is normalised away. The
  other 56-66% of polls saw a board that *had* moved.

That last number dictates the board rule. A blind TTL would hand the model a
stale board on the majority of repeat polls, and the model schedules from it,
so the snapshot cache is **stability-gated**: a snapshot becomes cacheable
only after the previous poll returned byte-identical content (relative times
normalised), and any board write from this member drops it immediately. The
TTL then bounds how long a *known-stable* board may be reused. This is the
one rule here with a correctness trade-off; it is gated, TTL-bounded, and
``view_task_ttl_seconds=0`` disables it outright.

Scope is one session: every cache key embeds the session id when the
framework exposes one, so a rail instance reused across sessions cannot
leak a result across them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

#: A board snapshot goes stale as soon as anything writes to the board, and
#: the writers below invalidate explicitly. The TTL is the backstop for the
#: writes this rail cannot see (another member's task transition), and it only
#: ever applies to a snapshot already observed to be stable.
#:
#: **默认 0 = 关闭**（2026-09-17 用户裁定）：实测只有 34-44% 的连续
#: ``view_task`` 快照未变，即 56-66% 的重复轮询看到的是**已经变了的板**；
#: 缓存命中时按缓存返回 = 把**过时的任务板**交给模型，而模型据此排程。
#: 用户原则「缓存过时信息就是严重错误」⇒ 平台期归零，只保留 ``read_file``
#: 的同哈希短路（那个不涉及过时：内容逐字相同）。需要冒险换 token 的部署
#: 可显式传 ``view_task_ttl_seconds=N``。
DEFAULT_VIEW_TASK_TTL_SECONDS = 0.0

#: Consecutive identical snapshots required before a snapshot may be cached.
#: 1 means "the previous poll matched the one before it".
DEFAULT_VIEW_TASK_STABLE_POLLS = 1

#: 1st and 2nd read of identical bytes are served normally; from the 3rd the
#: body is replaced by a reference. Two live copies are the point at which a
#: re-read stops adding information and starts adding payload.
DEFAULT_READ_REPEAT_THRESHOLD = 3

#: Consecutive cache hits on one key before the model is nudged to stop.
DEFAULT_NUDGE_AFTER_HITS = 3

#: Files above this size are not hashed at the boundary: the hash itself
#: would cost more than the re-read it is trying to avoid.
MAX_HASH_BYTES = 2 * 1024 * 1024

MAX_ENTRIES = 256

SEND_MESSAGE_TOOL = "send_message"
VIEW_TASK_TOOL = "view_task"
READ_FILE_TOOL = "read_file"

#: Tools that mutate the board. A completed call to any of them drops the
#: cached snapshots of the member that ran it.
_TASK_WRITE_TOOLS = frozenset({
    "create_task",
    "claim_task",
    "start_task",
    "update_task",
    "complete_task",
    "verify_task",
    "review_task",
    "assign_task",
    "submit_plan",
    "review_plan",
    "cancel_task",
    "fail_task",
    "reject_task",
    "delete_task",
})

_READ_WINDOW_KEYS = ("mode", "offset", "limit", "head", "tail")

_UNCHANGED_TEMPLATE = (
    "[unchanged] {path} 与本次会话第 {seen} 次读取的内容逐字节相同 "
    "(sha256={digest})，正文不再重复返回。需要正文请换参数 "
    "(offset/head/tail) 或先修改文件。"
)

_NUDGE_TEMPLATE = (
    "[A15 幂等] 你对 {tool} 的相同调用已连续 {hits} 次命中缓存且结果未变，"
    "框架已直接回填缓存结果。请不要再重复轮询/重发：推进下一步，"
    "或改用不同参数以获取新信息。"
)

_SKIP_MARKER = "__tool_idempotency_skip__"


def _as_mapping(raw: Any) -> Dict[str, Any]:
    """Normalise ``ToolCallInputs.tool_args`` to a dict.

    The framework stores the raw ``ToolCall.arguments`` here, which is a JSON
    *string*; a test double or a non-schema caller may hand over a mapping.
    """
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


#: ``format_time_context`` renders a task's transition as
#: ``2026-09-17 10:00:00 +08:00 (3 分钟前)``, so a board that did not move
#: still renders a different string on every poll. Stability has to be judged
#: on the board, not on the clock.
_SNAPSHOT_TIME_GROUP = re.compile(r"\(\d{4}-\d{2}-\d{2}[^()]*\([^()]*\)\)")


def normalize_snapshot(text: str) -> str:
    """Strip the relative-time rendering so two equal boards compare equal."""
    return _SNAPSHOT_TIME_GROUP.sub("(T)", text or "")


def sha256_file(path: str, *, max_bytes: int = MAX_HASH_BYTES) -> Optional[str]:
    """Digest the bytes of ``path``, or ``None`` when that is not affordable.

    ``None`` means "do not short-circuit": an unreadable, missing or oversized
    file must fall through to the real tool so its own error handling runs.
    """
    try:
        digest = hashlib.sha256()
        total = 0
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    return None
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


@dataclass
class _Entry:
    """A previously produced result, bound to the args that produced it."""

    result: Any
    message: ToolMessage
    created_at: float


class ToolIdempotencyRail(DeepAgentRail):
    """Replay repeated tool calls at the tool-call boundary.

    Args:
        view_task_ttl_seconds: Lifetime of a *known-stable* board snapshot.
            ``0`` disables the board rule entirely; point lookups are never
            cached regardless.
        view_task_stable_polls: How many consecutive identical snapshots make
            a board cacheable. 1 => the previous poll matched the one before.
        read_repeat_threshold: Call number from which identical reads are
            served as a reference (3 => the 3rd and later calls).
        nudge_after_hits: Consecutive cache hits on one key before the rail
            asks the model to stop repeating itself.
        max_entries: Upper bound on cached entries; oldest evicted first.
        clock: Monotonic time source, injectable for tests.
    """

    #: Below the security / interrupt rails so their ``_skip_tool`` decision
    #: is already visible here and is never overridden.
    priority = 30

    def __init__(
        self,
        *,
        view_task_ttl_seconds: float = DEFAULT_VIEW_TASK_TTL_SECONDS,
        view_task_stable_polls: int = DEFAULT_VIEW_TASK_STABLE_POLLS,
        read_repeat_threshold: int = DEFAULT_READ_REPEAT_THRESHOLD,
        nudge_after_hits: int = DEFAULT_NUDGE_AFTER_HITS,
        max_entries: int = MAX_ENTRIES,
        clock: Callable[[], float] = None,
    ) -> None:
        super().__init__()
        self.view_task_ttl_seconds = max(0.0, float(view_task_ttl_seconds))
        self.view_task_stable_polls = max(1, int(view_task_stable_polls))
        self.read_repeat_threshold = max(2, int(read_repeat_threshold))
        self.nudge_after_hits = max(1, int(nudge_after_hits))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock or _monotonic
        self._entries: Dict[Tuple, _Entry] = {}
        self._pending: Dict[int, Tuple] = {}
        self._read_counts: Dict[Tuple, int] = {}
        self._hits: Dict[Tuple, int] = {}
        self._nudged: set = set()
        #: Board stability bookkeeping: last normalised snapshot per key and
        #: how many consecutive polls returned that same snapshot.
        self._last_snapshot: Dict[Tuple, str] = {}
        self._stable_polls: Dict[Tuple, int] = {}
        #: Observable counters, for auditing and tests.
        self.skipped_calls = 0
        self.nudges_sent = 0

    # -- framework hooks -------------------------------------------------

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Replay a cached result instead of re-running a repeated tool.

        **fail-open**（2026-09-17）：内部异常只记 warning 并放行原调用。本 rail 是
        "省钱"装置，**不许**因为它自身出错而让工具调用失败或中断会话——首次上线
        时它会在每个会话的每次工具调用上跑，故障面必须收在"最多少省一次"。
        """
        if ctx.extra.get("_skip_tool"):
            # A security / interrupt rail has already ruled on this call.
            return
        try:
            inputs = getattr(ctx, "inputs", None)
            name = getattr(inputs, "tool_name", "") or ""
            args = _as_mapping(getattr(inputs, "tool_args", None))
            if name == SEND_MESSAGE_TOOL:
                self._before_send(ctx, args)
            elif name == VIEW_TASK_TOOL:
                self._before_view_task(ctx, args)
            elif name == READ_FILE_TOOL:
                self._before_read_file(ctx, args)
        except Exception as exc:  # noqa: BLE001 - 省钱装置不得成为故障源
            logger.warning("[ToolIdempotencyRail] before_tool_call 跳过: %s", exc)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Record the result of a call that did run, or invalidate a cache.

        **fail-open**（同 `before_tool_call`）：记账失败只 warning，不影响本轮结果。
        """
        skipped = self._pending.pop(id(getattr(ctx, "inputs", None)), None)
        if ctx.extra.get("_skip_tool"):
            return
        try:
            inputs = getattr(ctx, "inputs", None)
            name = getattr(inputs, "tool_name", "") or ""
            if name in _TASK_WRITE_TOOLS:
                self._invalidate_view_task(ctx)
                return
            if skipped is None:
                return
            message = getattr(inputs, "tool_msg", None)
            if message is None:
                return
            if name == VIEW_TASK_TOOL and skipped[0] == VIEW_TASK_TOOL:
                self._note_snapshot(
                    skipped,
                    normalize_snapshot(getattr(message, "content", "") or ""),
                )
            self._put(skipped, _Entry(
                result=getattr(inputs, "tool_result", None),
                message=message,
                created_at=self._clock(),
            ))
        except Exception as exc:  # noqa: BLE001 - 记账失败不得影响工具结果
            logger.warning("[ToolIdempotencyRail] after_tool_call 跳过: %s", exc)

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        """Drop the pending marker of a call that failed.

        A failed call must never be cached, and must not leave a marker that
        the next call's ``after_tool_call`` could pick up.
        """
        self._pending.pop(id(getattr(ctx, "inputs", None)), None)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Nudge the model once per key after it keeps repeating itself.

        This is the only part of this rail that can reduce wasted model round
        trips, and it does so indirectly: the steering note changes what the
        model does in the *following* iteration, so it stops spending
        iterations on a call that is already answered from cache.
        """
        hot = [
            (key, hits)
            for key, hits in self._hits.items()
            if hits >= self.nudge_after_hits and key not in self._nudged
        ]
        if not hot:
            return
        push = getattr(ctx, "push_steering", None)
        if not callable(push):
            return
        for key, hits in hot[:2]:
            self._nudged.add(key)
            self.nudges_sent += 1
            push(_NUDGE_TEMPLATE.format(tool=key[0], hits=hits))

    # -- send_message ----------------------------------------------------

    def _before_send(self, ctx: AgentCallbackContext, args: Dict[str, Any]) -> None:
        content = args.get("content")
        if not isinstance(content, str):
            return
        key = (
            SEND_MESSAGE_TOOL,
            self._scope(ctx),
            str(args.get("to", "")),
            _sha256_text(content),
        )
        if not self._replay(ctx, key, SEND_MESSAGE_TOOL):
            self._pending[id(ctx.inputs)] = key

    # -- view_task -------------------------------------------------------

    def _before_view_task(
        self, ctx: AgentCallbackContext, args: Dict[str, Any]
    ) -> None:
        # A point lookup (``task_id``) is never cached: it is cheap and the
        # caller is asking about one task it already knows about.
        if args.get("task_id"):
            return
        key = self._view_task_key(ctx, args)
        entry = self._entries.get(key)
        # Stability gate: a snapshot is reusable only when the board was
        # already observed to be standing still. Measured on the audit logs,
        # 56-66% of repeat polls saw a board that had moved, so an ungated TTL
        # would hand the model a stale board more often than a fresh one.
        stable = self._stable_polls.get(key, 0) >= self.view_task_stable_polls
        if entry is not None and self.view_task_ttl_seconds > 0 and stable:
            age = self._clock() - entry.created_at
            if age <= self.view_task_ttl_seconds:
                if self._replay(ctx, key, VIEW_TASK_TOOL):
                    return
        self._pending[id(ctx.inputs)] = key

    def _view_task_key(self, ctx: AgentCallbackContext, args: Dict[str, Any]) -> Tuple:
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        return (VIEW_TASK_TOOL, self._scope(ctx), canonical)

    def _note_snapshot(self, key: Tuple, normalized: str) -> None:
        """Track how long a board listing has been standing still."""
        if self._last_snapshot.get(key) == normalized:
            self._stable_polls[key] = self._stable_polls.get(key, 0) + 1
        else:
            self._stable_polls[key] = 0
        self._last_snapshot[key] = normalized

    def _invalidate_view_task(self, ctx: AgentCallbackContext) -> None:
        """Drop cached snapshots and their stability credit for this member.

        Clearing the stability credit too means the member has to observe the
        board standing still again before anything is served from cache.
        """
        scope = self._scope(ctx)
        stale = [
            key for key in set(self._entries) | set(self._last_snapshot)
            if key[0] == VIEW_TASK_TOOL and key[1] == scope
        ]
        for key in stale:
            self._entries.pop(key, None)
            self._last_snapshot.pop(key, None)
            self._stable_polls.pop(key, None)

    # -- read_file -------------------------------------------------------

    def _before_read_file(
        self, ctx: AgentCallbackContext, args: Dict[str, Any]
    ) -> None:
        path = args.get("file_path") or args.get("path")
        if not isinstance(path, str) or not path:
            return
        digest = sha256_file(path)
        if digest is None:
            return
        window = tuple(str(args.get(part)) for part in _READ_WINDOW_KEYS)
        key = (READ_FILE_TOOL, self._scope(ctx), path, digest, window)
        seen = self._read_counts.get(key, 0) + 1
        self._read_counts[key] = seen
        if seen < self.read_repeat_threshold:
            self._pending[id(ctx.inputs)] = key
            return
        entry = self._entries.get(key)
        if entry is None:
            # No recorded body to point back at; the real tool must run.
            self._pending[id(ctx.inputs)] = key
            return
        message = ToolMessage(
            content=_UNCHANGED_TEMPLATE.format(
                path=path, seen=seen, digest=digest[:12],
            ),
            tool_call_id=self._tool_call_id(ctx),
        )
        self._short_circuit(ctx, entry.result, message, key, READ_FILE_TOOL)

    # -- shared plumbing -------------------------------------------------

    def _replay(
        self, ctx: AgentCallbackContext, key: Tuple, tool_name: str
    ) -> bool:
        """Serve ``key`` from cache, returning whether it was served."""
        entry = self._entries.get(key)
        if entry is None:
            return False
        # The reply must address the *current* tool call, or the provider
        # rejects the conversation: a cached ToolMessage keeps the call id of
        # the call that produced it.
        message = ToolMessage(
            content=getattr(entry.message, "content", "") or "",
            tool_call_id=self._tool_call_id(ctx),
        )
        self._short_circuit(ctx, entry.result, message, key, tool_name)
        return True

    def _short_circuit(
        self,
        ctx: AgentCallbackContext,
        result: Any,
        message: ToolMessage,
        key: Tuple,
        tool_name: str,
    ) -> None:
        """Cancel the downstream call and answer it from cache.

        Mirrors the contract of ``AbilityManager._railed_execute_single_tool_call``
        (see also ``BaseSecurityRail._skip_tool``): ``_skip_tool`` set with
        ``inputs.tool_result`` / ``inputs.tool_msg`` populated makes the
        railed executor return the cached pair instead of invoking the tool.
        """
        ctx.extra["_skip_tool"] = True
        # Per-call breadcrumb: ``_skip_tool`` itself lives on the shared
        # ``ctx.extra`` dict, so upstream can key the skip by call id.
        ctx.extra[_SKIP_MARKER] = {
            "tool": tool_name,
            "tool_call_id": self._tool_call_id(ctx),
        }
        ctx.inputs.tool_result = result
        ctx.inputs.tool_msg = message
        self.skipped_calls += 1
        self._hits[key] = self._hits.get(key, 0) + 1

    def _put(self, key: Tuple, entry: _Entry) -> None:
        self._entries[key] = entry
        while len(self._entries) > self.max_entries:
            oldest = next(iter(self._entries))
            if oldest == key:
                break
            self._entries.pop(oldest, None)

    @staticmethod
    def _tool_call_id(ctx: AgentCallbackContext) -> str:
        call = getattr(getattr(ctx, "inputs", None), "tool_call", None)
        return getattr(call, "id", None) or ""

    @staticmethod
    def _scope(ctx: AgentCallbackContext) -> str:
        """Session identity for this call, so caches never cross sessions."""
        session = getattr(ctx, "session", None)
        for attr in ("session_id", "id", "conversation_id"):
            value = getattr(session, attr, None)
            if isinstance(value, str) and value:
                return value
        return "-"


def _monotonic() -> float:
    import time

    return time.monotonic()
