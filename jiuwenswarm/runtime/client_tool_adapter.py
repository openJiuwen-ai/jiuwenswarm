# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Detached client tools: tools a connected client runs while the conversation goes on.

A client declares its tools with ``client_tool.register``. The agent sees each
one as ``client__<name>``. Calling it does not pause the turn: the call is
recorded as ``pending``, the client receives a ``chat.client_tool_dispatch``
event and the agent is told the result will arrive later. The turn ends and
the user can keep talking to the agent.

Every call ends exactly once (``ClientToolAdapter._settle``):

- ``completed`` / ``error``: the client answered with ``client_tool.result``.
  The result is delivered to the agent as a new input, queued behind any turn
  that is running.
- ``timeout`` / ``cancelled`` / ``client_gone``: the call failed on the host
  side. Nothing is delivered and the agent is not woken up; a result that
  arrives later is refused with ``CLIENT_TOOL_CALL_SETTLED``. The agent may
  not call the tool again with the same arguments until the user sends new
  input (``check_invocation``).

The client learns how each call ended from ``chat.client_tool_settled`` and,
for a result it sent, whether the agent got it from ``chat.client_tool_delivery``.

The detached events are named apart from ``chat.client_tool_request``, which
asks a client to answer a *paused* call through ``chat.send``
(``jiuwenswarm.runtime.client_tools``): a client of that wire never answers a
detached call the wrong way.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import ClientToolResult, ClientToolStatus, default_client_output
from pydantic import ValidationError

from gateway.common.protocol_ids import is_valid_session_id

logger = logging.getLogger(__name__)

CLIENT_TOOL_NAME_PREFIX = "client__"
"""Prefix of the name the agent sees for a client tool, so it never shadows a built-in tool."""

CLIENT_TOOL_DISPATCH_EVENT = "chat.client_tool_dispatch"
CLIENT_TOOL_SETTLED_EVENT = "chat.client_tool_settled"
CLIENT_TOOL_DELIVERY_EVENT = "chat.client_tool_delivery"
DETACHED_MODE = "detached"

DEFAULT_TIMEOUT_SECONDS = 120.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 3600.0
DEFAULT_DISCONNECT_GRACE_SECONDS = 30.0
DEFAULT_DELIVERY_WAIT_SECONDS = 600.0
MAX_TOOLS_PER_SESSION = 64

_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_STATE_FILE_NAME = "client_tools.json"
_STATE_VERSION = 1

RESULT_ENVELOPE_TAG = "client_tool_result"
# The chat ``source`` of a delivered result: it makes the input internal, never user-authored.
CLIENT_TOOL_RESULT_SOURCE = "client_tool_result"
"""Tag of the input that delivers a client tool result to the agent."""


class ClientToolCallStatus(str, Enum):
    """Where a detached client tool call is."""

    PENDING = "pending"
    COMPLETED = "completed"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CLIENT_GONE = "client_gone"


FAILED_STATUSES = frozenset(
    {ClientToolCallStatus.TIMEOUT, ClientToolCallStatus.CANCELLED, ClientToolCallStatus.CLIENT_GONE}
)
"""Ends of a call whose result the agent never sees and that it may not retry by itself."""

_CLIENT_REPORTED_FAILURES: dict[ClientToolStatus, ClientToolCallStatus] = {
    ClientToolStatus.TIMEOUT: ClientToolCallStatus.TIMEOUT,
    ClientToolStatus.CANCELLED: ClientToolCallStatus.CANCELLED,
    ClientToolStatus.CLIENT_GONE: ClientToolCallStatus.CLIENT_GONE,
}


class ClientToolError(ValueError):
    """A stable, client-facing refusal of a ``client_tool.*`` request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


BAD_REQUEST = "CLIENT_TOOL_BAD_REQUEST"
UNKNOWN_TOOL = "CLIENT_TOOL_UNKNOWN_TOOL"
UNKNOWN_CALL = "CLIENT_TOOL_UNKNOWN_CALL"
CALL_SETTLED = "CLIENT_TOOL_CALL_SETTLED"
NOT_OWNER = "CLIENT_TOOL_NOT_OWNER"
SESSION_NOT_FOUND = "CLIENT_TOOL_SESSION_NOT_FOUND"


class ClientToolDeliveryError(RuntimeError):
    """A result the host accepted could not be handed to the agent."""


class DeliveryState(str, Enum):
    """Whether the result of a completed call reached the agent."""

    NONE = ""
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


def config_seconds(settings: Any, key: str, default: float, *, minimum: float = 0.0) -> float:
    """A duration setting, or ``default`` (with a warning) when it is missing or not a number."""
    value = settings.get(key) if isinstance(settings, Mapping) else None
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < minimum:
        logger.warning("[ClientTools] client_tools.%s=%r is not a duration; using %s", key, value, default)
        return default
    return float(value)


def valid_session_id(value: Any) -> str:
    """``value`` as a Session id; anything else is refused, since a Session id names a directory."""
    if not is_valid_session_id(value):
        raise ClientToolError(BAD_REQUEST, "session_id is invalid")
    return value


def client_tool_name(name: str) -> str:
    """The name the agent sees for the client tool ``name``."""
    return f"{CLIENT_TOOL_NAME_PREFIX}{name}"


def is_client_tool_name(tool_name: str) -> bool:
    """Whether ``tool_name`` is the agent-side name of a client tool."""
    return isinstance(tool_name, str) and tool_name.startswith(CLIENT_TOOL_NAME_PREFIX)


def call_fingerprint(tool_name: str, arguments: Any) -> str:
    """Identify a call by its tool and arguments, whatever their key order."""
    canonical = json.dumps(arguments if arguments is not None else {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{tool_name}\n{canonical}".encode("utf-8")).hexdigest()


def _bounded_timeout(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClientToolError(BAD_REQUEST, "timeout_s must be a number")
    return float(min(MAX_TIMEOUT_SECONDS, max(MIN_TIMEOUT_SECONDS, value)))


@dataclass(frozen=True, slots=True)
class ClientToolSpec:
    """One tool a client declared it can run."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    timeout_s: float
    owner_client_id: str

    @property
    def tool_name(self) -> str:
        """The name the agent calls this tool by."""
        return client_tool_name(self.name)

    @classmethod
    def from_wire(cls, item: Any, *, owner_client_id: str, default_timeout_s: float) -> ClientToolSpec:
        """Validate one entry of ``client_tool.register`` ``tools``."""
        if not isinstance(item, Mapping):
            raise ClientToolError(BAD_REQUEST, "each tool must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not _TOOL_NAME_PATTERN.match(name):
            raise ClientToolError(BAD_REQUEST, "tool name must match [A-Za-z0-9_-]{1,64}")
        description = item.get("description") or ""
        if not isinstance(description, str):
            raise ClientToolError(BAD_REQUEST, f"tool {name}: description must be a string")
        schema = item.get("input_schema")
        if schema is None:
            schema = {"type": "object", "properties": {}}
        if not isinstance(schema, Mapping) or schema.get("type", "object") != "object":
            raise ClientToolError(BAD_REQUEST, f"tool {name}: input_schema must be an object schema")
        mode = item.get("mode", DETACHED_MODE)
        if mode != DETACHED_MODE:
            raise ClientToolError(BAD_REQUEST, f"tool {name}: only mode 'detached' is supported")
        return cls(
            name=name,
            description=description.strip(),
            input_schema=json.loads(json.dumps(dict(schema))),
            timeout_s=_bounded_timeout(item.get("timeout_s"), default_timeout_s),
            owner_client_id=owner_client_id,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "timeout_s": self.timeout_s,
            "owner_client_id": self.owner_client_id,
            "mode": DETACHED_MODE,
        }

    @classmethod
    def from_state(cls, data: Mapping[str, Any]) -> ClientToolSpec:
        return cls(
            name=str(data["name"]),
            description=str(data.get("description") or ""),
            input_schema=dict(data.get("input_schema") or {"type": "object", "properties": {}}),
            timeout_s=float(data.get("timeout_s") or DEFAULT_TIMEOUT_SECONDS),
            owner_client_id=str(data.get("owner_client_id") or ""),
        )


@dataclass(frozen=True, slots=True)
class ClientToolCall:
    """One call of a client tool, from the moment the agent made it to how it ended."""

    call_id: str
    session_id: str
    tool_name: str
    client_tool: str
    arguments: Mapping[str, Any]
    fingerprint: str
    owner_client_id: str
    status: ClientToolCallStatus
    created_at: float
    deadline: float
    settled_at: float | None = None
    reason: str = ""
    delivery: DeliveryState = DeliveryState.NONE

    @property
    def pending(self) -> bool:
        return self.status is ClientToolCallStatus.PENDING

    def to_state(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "client_tool": self.client_tool,
            "arguments": dict(self.arguments),
            "fingerprint": self.fingerprint,
            "owner_client_id": self.owner_client_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "deadline": self.deadline,
            "settled_at": self.settled_at,
            "reason": self.reason,
            "delivery": self.delivery.value,
        }

    @classmethod
    def from_state(cls, data: Mapping[str, Any]) -> ClientToolCall:
        return cls(
            call_id=str(data["call_id"]),
            session_id=str(data["session_id"]),
            tool_name=str(data["tool_name"]),
            client_tool=str(data.get("client_tool") or ""),
            arguments=dict(data.get("arguments") or {}),
            fingerprint=str(data["fingerprint"]),
            owner_client_id=str(data.get("owner_client_id") or ""),
            status=ClientToolCallStatus(data["status"]),
            created_at=float(data["created_at"]),
            deadline=float(data["deadline"]),
            settled_at=float(data["settled_at"]) if data.get("settled_at") is not None else None,
            reason=str(data.get("reason") or ""),
            delivery=DeliveryState(data.get("delivery") or ""),
        )


def dispatch_event(call: ClientToolCall) -> dict[str, Any]:
    """The ``chat.client_tool_dispatch`` event asking the client to run ``call``."""
    return {
        "event_type": CLIENT_TOOL_DISPATCH_EVENT,
        "mode": DETACHED_MODE,
        "request_id": call.call_id,
        "call_id": call.call_id,
        "session_id": call.session_id,
        "tool_name": call.client_tool,
        "agent_tool_name": call.tool_name,
        "arguments": dict(call.arguments),
        "deadline": int(call.deadline * 1000),
        "target_client_id": call.owner_client_id,
    }


def settled_event(call: ClientToolCall) -> dict[str, Any]:
    """The ``chat.client_tool_settled`` event telling the client how ``call`` ended."""
    return {
        "event_type": CLIENT_TOOL_SETTLED_EVENT,
        "mode": DETACHED_MODE,
        "call_id": call.call_id,
        "session_id": call.session_id,
        "tool_name": call.client_tool,
        "status": call.status.value,
        "reason": call.reason,
        "accepted": call.status not in FAILED_STATUSES,
        "target_client_id": call.owner_client_id,
    }


def delivery_event(call: ClientToolCall) -> dict[str, Any]:
    """The ``chat.client_tool_delivery`` event telling the client whether the agent got the result."""
    return {
        "event_type": CLIENT_TOOL_DELIVERY_EVENT,
        "mode": DETACHED_MODE,
        "call_id": call.call_id,
        "session_id": call.session_id,
        "tool_name": call.client_tool,
        "delivered": call.delivery is DeliveryState.DELIVERED,
        "reason": call.reason,
        "target_client_id": call.owner_client_id,
    }


def _xml_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def result_envelope(call: ClientToolCall, result: ClientToolResult) -> str:
    """The input that hands a client tool result to the agent."""
    output = default_client_output(result, tool_name=call.tool_name)
    data = output.data if isinstance(output.data, Mapping) else {}
    text = str(data.get("content") or "") if output.success else str(output.error or "")
    images = data.get("multimodal") or []
    if images:
        text = f"{text}\n[{len(images)} image(s) returned by the client are not shown]".strip()
    status = ClientToolCallStatus.COMPLETED.value if output.success else ClientToolCallStatus.ERROR.value
    return (
        f'<{RESULT_ENVELOPE_TAG} call_id="{_xml_attr(call.call_id)}" tool="{_xml_attr(call.tool_name)}" '
        f'status="{status}">\n{text}\n</{RESULT_ENVELOPE_TAG}>\n'
        f"This is the result of the client tool call {call.call_id} you made earlier. "
        "Continue the task with it."
    )


def _host_input_content(text: str) -> str:
    """The ``content`` of an input the host rendered as a lead-in plus a JSON envelope, else ``text``."""
    start = text.find("{")
    if start < 0:
        return text
    try:
        envelope, end = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return text
    if isinstance(envelope, dict) and isinstance(envelope.get("content"), str) and not text[start + end:].strip():
        return envelope["content"]
    return text


def is_result_envelope(text: Any) -> bool:
    """Whether an input is a delivered client tool result rather than something the user wrote.

    Rails see inputs as the host rendered them (``UserTurn.render``): the envelope is
    then the ``content`` of a JSON wrapper.
    """
    if not isinstance(text, str):
        return False
    return _host_input_content(text).lstrip().startswith(f"<{RESULT_ENVELOPE_TAG} ")


class ClientToolStateStore:
    """Keep each Session's client tools and calls in ``<sessions>/<session_id>/client_tools.json``."""

    def __init__(self, sessions_dir: Callable[[], Path]) -> None:
        self._sessions_dir = sessions_dir

    def _path(self, session_id: str) -> Path:
        if not is_valid_session_id(session_id):
            raise ValueError("invalid session_id")  # never a path outside the sessions directory
        return self._sessions_dir() / session_id / _STATE_FILE_NAME

    def load(self, session_id: str) -> dict[str, Any] | None:
        path = self._path(session_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning("[ClientTools] unreadable state ignored: %s", path, exc_info=True)
            return None
        return data if isinstance(data, dict) and data.get("version") == _STATE_VERSION else None

    def save(self, session_id: str, data: dict[str, Any]) -> None:
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"version": _STATE_VERSION, **data}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, session_id: str) -> None:
        try:
            self._path(session_id).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("[ClientTools] state not removed: session_id=%s", session_id, exc_info=True)


@dataclass
class _SessionState:
    specs: dict[str, ClientToolSpec] = field(default_factory=dict)
    calls: dict[str, ClientToolCall] = field(default_factory=dict)
    # Fingerprints of failed calls the agent may not retry until the user speaks again.
    retry_blocked: set[str] = field(default_factory=set)


EmitEvent = Callable[[str, dict[str, Any]], Awaitable[None]]
DeliverResult = Callable[[ClientToolCall, str], Awaitable[None]]

# Settled calls kept per Session for the reminder and late-result refusals.
_MAX_SETTLED_CALLS = 50


class ClientToolAdapter:
    """Own the client tools of every Session and the life of each call.

    Args:
        store: Where each Session's tools and calls are persisted.
        emit: Sends an event to the Session's clients.
        deliver: Hands a result envelope to the agent of the call's Session,
            after any turn that is running there.
        default_timeout_s: Timeout of a tool that declares none.
        disconnect_grace_s: How long a call waits for its client to come back.
        clock: Wall clock, in seconds.
    """

    def __init__(
        self,
        *,
        store: ClientToolStateStore,
        emit: EmitEvent,
        deliver: DeliverResult,
        default_timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
        disconnect_grace_s: float = DEFAULT_DISCONNECT_GRACE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._emit = emit
        self._deliver = deliver
        self._default_timeout_s = _bounded_timeout(default_timeout_s, DEFAULT_TIMEOUT_SECONDS)
        self._disconnect_grace_s = max(0.0, float(disconnect_grace_s))
        self._clock = clock
        self._sessions: dict[str, _SessionState] = {}
        self._deadline_timers: dict[str, asyncio.TimerHandle] = {}
        self._grace_timers: dict[str, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- state -----------------------------------------------------------

    def _state(self, session_id: str) -> _SessionState:
        state = self._sessions.get(session_id)
        if state is not None:
            return state
        valid_session_id(session_id)
        state = _SessionState()
        data = self._store.load(session_id) or {}
        for item in data.get("tools") or ():
            try:
                spec = ClientToolSpec.from_state(item)
            except (KeyError, TypeError, ValueError):
                continue
            state.specs[spec.name] = spec
        for item in data.get("calls") or ():
            try:
                call = ClientToolCall.from_state(item)
            except (KeyError, TypeError, ValueError):
                continue
            state.calls[call.call_id] = call
        state.retry_blocked = {str(item) for item in data.get("retry_blocked") or ()}
        self._sessions[session_id] = state
        interrupted = [call for call in state.calls.values() if call.delivery is DeliveryState.PENDING]
        for call in interrupted:
            # The process that was delivering this result is gone; the result went with it.
            lost = replace(call, delivery=DeliveryState.FAILED, reason="the server restarted before delivery")
            state.calls[call.call_id] = lost
            if self._loop() is not None:
                self._spawn(self._emit_safely(session_id, delivery_event(lost)))
        if interrupted:
            self._persist(session_id)
        for call in state.calls.values():
            if call.pending:
                self._arm_deadline(call)
        return state

    def _persist(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is None:
            return
        settled = [call for call in state.calls.values() if not call.pending]
        for call in settled[:-_MAX_SETTLED_CALLS]:
            state.calls.pop(call.call_id, None)
        try:
            self._store.save(session_id, {
                "tools": [spec.to_wire() for spec in state.specs.values()],
                "calls": [call.to_state() for call in state.calls.values()],
                "retry_blocked": sorted(state.retry_blocked),
            })
        except OSError:
            logger.warning("[ClientTools] state not persisted: session_id=%s", session_id, exc_info=True)

    # -- timers ----------------------------------------------------------

    def _loop(self) -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _arm_deadline(self, call: ClientToolCall) -> None:
        loop = self._loop()
        if loop is None:
            return
        self._cancel_timer(self._deadline_timers, call.call_id)
        delay = max(0.0, call.deadline - self._clock())
        self._deadline_timers[call.call_id] = loop.call_later(
            delay, self._spawn_settle, call.session_id, call.call_id, ClientToolCallStatus.TIMEOUT,
            "the client did not return a result in time",
        )

    @staticmethod
    def _cancel_timer(timers: dict[str, asyncio.TimerHandle], call_id: str) -> None:
        handle = timers.pop(call_id, None)
        if handle is not None:
            handle.cancel()

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _spawn_settle(self, session_id: str, call_id: str, status: ClientToolCallStatus, reason: str) -> None:
        self._spawn(self._settle_quietly(session_id, call_id, status, reason))

    async def _settle_quietly(
        self, session_id: str, call_id: str, status: ClientToolCallStatus, reason: str,
    ) -> None:
        try:
            await self._settle(session_id, call_id, status, reason)
        except ClientToolError:
            pass  # already settled by something else

    # -- registration ----------------------------------------------------

    def specs(self, session_id: str) -> list[ClientToolSpec]:
        """The client tools the agent of ``session_id`` is offered."""
        return list(self._state(session_id).specs.values())

    async def register(
        self, session_id: str, client_id: str, tools: Any, *, replace_owned: bool = False,
    ) -> list[ClientToolSpec]:
        """Declare (or redeclare, by name) the tools ``client_id`` runs for ``session_id``.

        With ``replace_owned`` the client's earlier tools it does not list again are withdrawn.
        """
        session_id, client_id = valid_session_id(session_id), _required("client_id", client_id)
        if not isinstance(tools, list):
            raise ClientToolError(BAD_REQUEST, "tools must be a list")
        specs = [
            ClientToolSpec.from_wire(item, owner_client_id=client_id, default_timeout_s=self._default_timeout_s)
            for item in tools
        ]
        names = [spec.name for spec in specs]
        if len(set(names)) != len(names):
            raise ClientToolError(BAD_REQUEST, "tool names must be unique")
        state = self._state(session_id)
        merged = dict(state.specs)
        if replace_owned:
            merged = {name: spec for name, spec in merged.items() if spec.owner_client_id != client_id}
        merged.update({spec.name: spec for spec in specs})
        if len(merged) > MAX_TOOLS_PER_SESSION:
            raise ClientToolError(BAD_REQUEST, f"a Session takes at most {MAX_TOOLS_PER_SESSION} client tools")
        state.specs = merged
        self._persist(session_id)
        self.on_client_attached(session_id, client_id)
        logger.info("[ClientTools] registered: session_id=%s client_id=%s tools=%s", session_id, client_id, names)
        return specs

    # -- calls -----------------------------------------------------------

    async def open_call(self, session_id: str, call_id: str, tool_name: str, arguments: Any) -> ClientToolCall:
        """Record the agent's call of a client tool and ask the client to run it.

        Opening the same ``call_id`` again returns the call already recorded.
        """
        state = self._state(session_id)
        existing = state.calls.get(call_id)
        if existing is not None:
            return existing
        spec = next((item for item in state.specs.values() if item.tool_name == tool_name), None)
        if spec is None:
            raise ClientToolError(UNKNOWN_TOOL, f"no client runs the tool {tool_name}")
        arguments = dict(arguments) if isinstance(arguments, Mapping) else {}
        now = self._clock()
        call = ClientToolCall(
            call_id=call_id,
            session_id=session_id,
            tool_name=tool_name,
            client_tool=spec.name,
            arguments=arguments,
            fingerprint=call_fingerprint(tool_name, arguments),
            owner_client_id=spec.owner_client_id,
            status=ClientToolCallStatus.PENDING,
            created_at=now,
            deadline=now + spec.timeout_s,
        )
        state.calls[call_id] = call
        self._persist(session_id)
        self._arm_deadline(call)
        logger.info("[ClientTools] call opened: session_id=%s call_id=%s tool=%s", session_id, call_id, tool_name)
        await self._emit_safely(session_id, dispatch_event(call))
        return call

    async def submit_result(self, session_id: str, call_id: str, result: Any, *, client_id: str = "") -> ClientToolCall:
        """Settle a call with the result its client sent.

        A result is accepted once, from the call's own client and only while
        the call is pending. A status of ``timeout``, ``cancelled`` or
        ``client_gone`` ends the call as failed: its result is never delivered.
        """
        call = self._pending_call(session_id, call_id, client_id)
        try:
            parsed = ClientToolResult.coerce(result)
        except ValidationError as exc:
            raise ClientToolError(BAD_REQUEST, f"invalid result: {exc.errors()[0]['msg']}") from exc
        failure = _CLIENT_REPORTED_FAILURES.get(parsed.status)
        if failure is not None:
            return await self._settle(session_id, call.call_id, failure, "reported by the client")
        status = ClientToolCallStatus.ERROR if parsed.failed else ClientToolCallStatus.COMPLETED
        settled = await self._settle(session_id, call.call_id, status, "", delivery=DeliveryState.PENDING)
        self._spawn(self._deliver_tracked(settled, result_envelope(settled, parsed)))
        return settled

    async def cancel(self, session_id: str, call_id: str, *, reason: str = "", client_id: str = "") -> ClientToolCall:
        """Cancel a pending call; nothing it returns afterwards reaches the agent."""
        call = self._pending_call(session_id, call_id, client_id)
        return await self._settle(
            session_id, call.call_id, ClientToolCallStatus.CANCELLED, reason or "cancelled by the client",
        )

    async def cancel_session(self, session_id: str, *, reason: str) -> int:
        """Cancel every pending call of ``session_id``; return how many were cancelled."""
        pending = [call.call_id for call in self._state(session_id).calls.values() if call.pending]
        for call_id in pending:
            await self._settle_quietly(session_id, call_id, ClientToolCallStatus.CANCELLED, reason)
        return len(pending)

    async def close_session(self, session_id: str) -> None:
        """Cancel the Session's pending calls and forget its client tools (archive / delete)."""
        if session_id not in self._sessions and self._store.load(session_id) is None:
            return
        await self.cancel_session(session_id, reason="the session was closed")
        self._sessions.pop(session_id, None)
        self._store.delete(session_id)

    def list_pending(self, session_id: str) -> list[dict[str, Any]]:
        """The request events of the Session's pending calls, oldest first."""
        calls = sorted(
            (call for call in self._state(session_id).calls.values() if call.pending),
            key=lambda call: call.created_at,
        )
        return [dispatch_event(call) for call in calls]

    def _pending_call(self, session_id: str, call_id: str, client_id: str) -> ClientToolCall:
        session_id, call_id = valid_session_id(session_id), _required("call_id", call_id)
        call = self._state(session_id).calls.get(call_id)
        if call is None:
            raise ClientToolError(UNKNOWN_CALL, f"no client tool call {call_id} in this session")
        if client_id and call.owner_client_id and client_id != call.owner_client_id:
            raise ClientToolError(NOT_OWNER, f"call {call_id} belongs to another client")
        if not call.pending:
            logger.info(
                "[ClientTools] late answer dropped: session_id=%s call_id=%s status=%s",
                session_id, call_id, call.status.value,
            )
            raise ClientToolError(CALL_SETTLED, f"call {call_id} already ended: {call.status.value}")
        return call

    async def _settle(
        self, session_id: str, call_id: str, status: ClientToolCallStatus, reason: str,
        *, delivery: DeliveryState = DeliveryState.NONE,
    ) -> ClientToolCall:
        # Check and transition without awaiting in between: the first end of a call wins.
        state = self._state(session_id)
        call = state.calls.get(call_id)
        if call is None or not call.pending:
            raise ClientToolError(CALL_SETTLED, f"call {call_id} already ended")
        settled = replace(call, status=status, settled_at=self._clock(), reason=reason, delivery=delivery)
        state.calls[call_id] = settled
        if status in FAILED_STATUSES:
            state.retry_blocked.add(settled.fingerprint)
        self._cancel_timer(self._deadline_timers, call_id)
        self._cancel_timer(self._grace_timers, call_id)
        self._persist(session_id)
        logger.info(
            "[ClientTools] call settled: session_id=%s call_id=%s status=%s reason=%s",
            session_id, call_id, status.value, reason,
        )
        await self._emit_safely(session_id, settled_event(settled))
        return settled

    async def _emit_safely(self, session_id: str, payload: dict[str, Any]) -> None:
        try:
            await self._emit(session_id, payload)
        except Exception:  # noqa: BLE001 - a lost event must not undo the transition
            logger.warning("[ClientTools] event not sent: %s", payload.get("event_type"), exc_info=True)

    async def _deliver_tracked(self, call: ClientToolCall, envelope: str) -> None:
        """Hand a result to the agent, then tell the client whether that worked."""
        reason = ""
        try:
            await self._deliver(call, envelope)
            delivery = DeliveryState.DELIVERED
        except asyncio.CancelledError:
            raise  # shutdown: the call keeps ``delivery=pending`` in its record
        except ClientToolDeliveryError as exc:
            delivery, reason = DeliveryState.FAILED, str(exc)
        except Exception:  # noqa: BLE001
            delivery, reason = DeliveryState.FAILED, "the agent could not take the result"
            logger.exception(
                "[ClientTools] delivery failed: session_id=%s call_id=%s", call.session_id, call.call_id,
            )
        state = self._sessions.get(call.session_id)
        current = state.calls.get(call.call_id) if state is not None else None
        if current is None:
            return  # the Session was closed meanwhile
        updated = replace(current, delivery=delivery, reason=reason or current.reason)
        state.calls[call.call_id] = updated
        self._persist(call.session_id)
        if delivery is DeliveryState.FAILED:
            logger.warning(
                "[ClientTools] result not delivered: session_id=%s call_id=%s reason=%s",
                call.session_id, call.call_id, reason,
            )
        await self._emit_safely(call.session_id, delivery_event(updated))

    # -- clients ---------------------------------------------------------

    def on_client_attached(self, session_id: str, client_id: str = "") -> None:
        """A client of the Session is back: its calls stop waiting to be declared gone."""
        for call in self._state(session_id).calls.values():
            if call.pending and (not client_id or call.owner_client_id in ("", client_id)):
                self._cancel_timer(self._grace_timers, call.call_id)

    def on_client_detached(self, session_id: str, client_id: str = "") -> None:
        """A client of the Session went away: its pending calls end as ``client_gone`` unless it returns in time."""
        loop = self._loop()
        if loop is None:
            return
        for call in self._state(session_id).calls.values():
            if not call.pending or call.call_id in self._grace_timers:
                continue
            if client_id and call.owner_client_id not in ("", client_id):
                continue
            self._grace_timers[call.call_id] = loop.call_later(
                self._disconnect_grace_s, self._spawn_settle, session_id, call.call_id,
                ClientToolCallStatus.CLIENT_GONE, "the client disconnected",
            )

    # -- agent guard -----------------------------------------------------

    def check_invocation(self, session_id: str, tool_name: str, arguments: Any) -> str | None:
        """Why the agent may not make this client tool call now, or None when it may."""
        state = self._state(session_id)
        fingerprint = call_fingerprint(tool_name, arguments if isinstance(arguments, Mapping) else {})
        running = next(
            (call for call in state.calls.values() if call.pending and call.fingerprint == fingerprint), None,
        )
        if running is not None:
            return (
                f"This client tool call is already running as call_id={running.call_id}. "
                "Its result will arrive as a separate message; do not call it again."
            )
        if fingerprint in state.retry_blocked:
            return (
                "This client tool call already failed (timed out, cancelled, or the client went away) and "
                "will not be retried automatically. Tell the user, and call it again only if the user asks."
            )
        return None

    async def note_user_input(self, session_id: str) -> None:
        """The user spoke: the agent may call again the client tools that failed before."""
        state = self._state(session_id)
        if state.retry_blocked:
            state.retry_blocked.clear()
            self._persist(session_id)

    def reminder(self, session_id: str) -> str | None:
        """What the agent should know about the Session's client tool calls, if anything."""
        state = self._sessions.get(session_id)
        if state is None:
            return None
        pending = [call for call in state.calls.values() if call.pending]
        failed = [
            call for call in state.calls.values()
            if call.status in FAILED_STATUSES and call.fingerprint in state.retry_blocked
        ]
        if not pending and not failed:
            return None
        lines = ["Client tool calls of this conversation:"]
        lines += [f"- {call.tool_name} call_id={call.call_id}: running on the client" for call in pending]
        lines += [
            f"- {call.tool_name} call_id={call.call_id}: {call.status.value}; do not call it again "
            "unless the user asks" for call in failed
        ]
        lines.append(
            "A running call's result arrives as a separate message. Never invent or assume the result of "
            "a call that is running or failed."
        )
        return "\n".join(lines)

    # -- shutdown --------------------------------------------------------

    async def aclose(self) -> None:
        """Stop the timers; pending calls stay recorded and resume their deadlines on the next start."""
        for timers in (self._deadline_timers, self._grace_timers):
            for handle in timers.values():
                handle.cancel()
            timers.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._sessions.clear()


def _required(name: str, value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ClientToolError(BAD_REQUEST, f"{name} is required")
    return text


def parse_tools_param(value: Any) -> list[Any]:
    """The ``tools`` list of a register request, accepting a JSON string from loose clients."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ClientToolError(BAD_REQUEST, "tools must be a list") from exc
    if not isinstance(value, list):
        raise ClientToolError(BAD_REQUEST, "tools must be a list")
    return value


def specs_wire(specs: Iterable[ClientToolSpec]) -> list[dict[str, Any]]:
    return [{**spec.to_wire(), "agent_tool_name": spec.tool_name} for spec in specs]


__all__ = [
    "CLIENT_TOOL_DELIVERY_EVENT",
    "CLIENT_TOOL_DISPATCH_EVENT",
    "CLIENT_TOOL_NAME_PREFIX",
    "CLIENT_TOOL_RESULT_SOURCE",
    "CLIENT_TOOL_SETTLED_EVENT",
    "FAILED_STATUSES",
    "ClientToolAdapter",
    "ClientToolCall",
    "ClientToolCallStatus",
    "ClientToolDeliveryError",
    "ClientToolError",
    "ClientToolSpec",
    "ClientToolStateStore",
    "DeliveryState",
    "call_fingerprint",
    "client_tool_name",
    "config_seconds",
    "delivery_event",
    "dispatch_event",
    "is_client_tool_name",
    "is_result_envelope",
    "parse_tools_param",
    "result_envelope",
    "settled_event",
    "specs_wire",
    "valid_session_id",
]
