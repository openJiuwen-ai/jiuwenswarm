"""Web RPCs for JiuwenMate colleagues, served from the AgentServer adapter registry.

Every call works in the single local namespace. Results are the public views of
``PersistentAgentService`` and ``AgentManager``: no Spec, model connection or
secret is returned, and a refused operation keeps its error code so the page can
show it. Private messages run in the AgentServer process, so a page that closes
or reconnects does not end them; the page reads progress again by querying.
Each colleague has several Web conversations, listed with their titles; every
conversation is a separate Session with its own history and window.
While a reply is streaming, its fragments are also pushed as
``jiuwenmate.chat.stream`` events to the Web connections that last asked about
that colleague; a missed push is made up by the next query.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.jiuwenmate.activity_query import ActivityQuery
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef, PersistentContractError
from jiuwenswarm.server.jiuwenmate.models import ColleagueSettings, PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WORKSPACE_READ, WORKSPACE_WRITE
from jiuwenswarm.server.jiuwenmate.resource_assembly import InstalledResourceCatalog
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService
from jiuwenswarm.server.jiuwenmate.web_conversations import WebConversationStore
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter, build_error_response
from openjiuwen.harness.persistent_agent import AgentEvent, EventType

logger = logging.getLogger(__name__)

WEB_NAMESPACE = "local"
# The local Web user has one private conversation with each colleague.
# The single Web conversation of each colleague before conversations were listed.
LEGACY_CONVERSATION_ID = "local"
# The local user owns every colleague, so a Web Request may read and write the colleague's office directory.
WEB_ACCESS = AccessSnapshot(
    principal_ref="local",
    startup_scope=frozenset({"direct_message", WORKSPACE_READ, WORKSPACE_WRITE}),
    source_scope=frozenset({"direct_message", WORKSPACE_READ, WORKSPACE_WRITE}),
    policy_revision="local",
)
WEB_SENDER_NAME = "用户"
STREAM_EVENT = "jiuwenmate.chat.stream"
# Fragments arriving within this window are sent as one push.
STREAM_COALESCE_SECONDS = 0.03
MAX_WATCHERS = 8
_STREAM_KINDS = {
    EventType.REPLY_DELTA: "delta",
    EventType.MODEL_COMPLETED: "segment",
    EventType.EXECUTION_FINAL: "ended",
    EventType.EXECUTION_PAUSED: "ended",
}

PushSink = Callable[[dict[str, Any]], Awaitable[Any]]
ManagerFactory = Callable[..., AgentManager]

_REQUEST_METHODS = frozenset({ReqMethod.JIUWENMATE_REQUEST_GET.value, ReqMethod.JIUWENMATE_REQUEST_ABORT.value})
_COLLEAGUE_METHODS = frozenset(
    {
        ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT.value,
        ReqMethod.JIUWENMATE_COLLEAGUE_LIST.value,
        ReqMethod.JIUWENMATE_COLLEAGUE_GET.value,
        ReqMethod.JIUWENMATE_COLLEAGUE_CREATE.value,
        ReqMethod.JIUWENMATE_COLLEAGUE_SETTINGS_GET.value,
        ReqMethod.JIUWENMATE_COLLEAGUE_SETTINGS_UPDATE.value,
        ReqMethod.JIUWENMATE_COLLEAGUE_PUBLISH.value,
    }
)
_RUNTIME_METHODS = frozenset(
    {
        ReqMethod.JIUWENMATE_COLLEAGUE_DELETE.value,
        ReqMethod.JIUWENMATE_CHAT_SEND.value,
        ReqMethod.JIUWENMATE_CHAT_HISTORY.value,
        ReqMethod.JIUWENMATE_REQUEST_GET.value,
        ReqMethod.JIUWENMATE_REQUEST_ABORT.value,
        ReqMethod.JIUWENMATE_CONVERSATION_LIST.value,
        ReqMethod.JIUWENMATE_CONVERSATION_RENAME.value,
        ReqMethod.JIUWENMATE_CONVERSATION_DELETE.value,
        ReqMethod.JIUWENMATE_ACTIVITY_GET.value,
        ReqMethod.JIUWENMATE_KNOWLEDGE_LIST.value,
        ReqMethod.JIUWENMATE_SCHEDULE_LIST.value,
        ReqMethod.JIUWENMATE_SCHEDULE_CREATE.value,
        ReqMethod.JIUWENMATE_SCHEDULE_SET_ENABLED.value,
        ReqMethod.JIUWENMATE_SCHEDULE_DELETE.value,
        ReqMethod.JIUWENMATE_SCHEDULE_UPDATE.value,
        ReqMethod.JIUWENMATE_SCHEDULE_RUN.value,
        ReqMethod.JIUWENMATE_SCHEDULE_HISTORY.value,
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ignore(event: AgentEvent) -> None:
    return None


def configured_model_catalog() -> dict[str, Any]:
    """The models the colleague service resolves, without connection details."""
    from jiuwenswarm.common.config import get_default_models
    from jiuwenswarm.runtime.model_catalog import build_model_catalog

    return build_model_catalog(get_default_models()).to_dict()


def _text_param(params: Mapping[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise PersistentAgentError(f"{name} is required")
    return value


def _schedule_fields(params: Mapping[str, Any]) -> dict[str, Any]:
    interval = params.get("interval_seconds")
    return {
        "name": _text_param(params, "name"),
        "prompt": _text_param(params, "prompt"),
        "cron_expr": str(params.get("cron_expr") or ""),
        "timezone_name": str(params.get("timezone") or "Asia/Shanghai"),
        "schedule_kind": str(params.get("schedule_kind") or "cron"),
        "interval_seconds": interval if isinstance(interval, int) else None,
        "run_at": params.get("run_at") or None,
        "starts_at": params.get("starts_at") or None,
        "ends_at": params.get("ends_at") or None,
    }


def _settings(params: Mapping[str, Any]) -> ColleagueSettings:
    return ColleagueSettings.from_dict(params.get("settings"))


def _default_manager(service: PersistentAgentService, *, on_event: Callable[[AgentEvent], None]) -> AgentManager:
    return AgentManager.create_default(service, on_event=on_event)


def _coalesce(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for frame in frames:
        last = merged[-1] if merged else None
        if (
            last is not None
            and frame["kind"] == "delta"
            and last["kind"] == "delta"
            and last["execution_id"] == frame["execution_id"]
        ):
            merged[-1] = {**last, "seq": frame["seq"], "text": last["text"] + frame["text"]}
        else:
            merged.append(frame)
    return merged


class StreamPublisher:
    """Pushes reply progress of each colleague, in event order, to the Web connections watching it."""

    def __init__(self, push: PushSink) -> None:
        self._push = push
        self._watchers: dict[str, dict[str, str]] = {}
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None

    def watch(self, agent_id: str, request: AgentRequest) -> None:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        ws_id = metadata.get("ws_id")
        if not isinstance(ws_id, str) or not ws_id:
            return
        watchers = self._watchers.setdefault(agent_id, {})
        watchers.pop(ws_id, None)
        watchers[ws_id] = request.channel_id
        while len(watchers) > MAX_WATCHERS:
            watchers.pop(next(iter(watchers)))

    def observe(self, event: AgentEvent) -> None:
        kind = _STREAM_KINDS.get(event.event_type)
        if kind is None or self._queue is None:
            return
        self._queue.put_nowait(
            {
                "kind": kind,
                "agent_id": event.agent_id,
                "request_id": event.request_id,
                "execution_id": event.execution_id,
                "seq": event.local_seq,
                "text": event.payload.text if kind == "delta" else "",
            }
        )

    def start(self) -> None:
        if self._task is None:
            self._queue = asyncio.Queue()
            self._task = asyncio.create_task(self._run(self._queue), name="jiuwenmate-stream-push")

    async def aclose(self) -> None:
        """Stop pushing; frames not yet sent are dropped, the page reads them by querying."""
        task, self._task, self._queue = self._task, None, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            frames = [await queue.get()]
            await asyncio.sleep(STREAM_COALESCE_SECONDS)
            while not queue.empty():
                frames.append(queue.get_nowait())
            for frame in _coalesce(frames):
                for ws_id, channel_id in list(self._watchers.get(frame["agent_id"], {}).items()):
                    await self._send(frame, ws_id, channel_id)

    async def _send(self, frame: dict[str, Any], ws_id: str, channel_id: str) -> None:
        message = {
            "request_id": f"{frame['execution_id']}:{frame['seq']}",
            "channel_id": channel_id,
            "payload": {"event_type": STREAM_EVENT, **frame},
            "metadata": {"ws_id": ws_id},
        }
        try:
            await self._push(message)
        except Exception as exc:
            logger.warning("[JiuwenMate] stream push failed: request=%s error=%s", frame["request_id"], exc)


class JiuwenMateWebAdapter(GatewayAdapter):
    methods = _COLLEAGUE_METHODS | _RUNTIME_METHODS | {ReqMethod.JIUWENMATE_MODELS_LIST.value}

    def __init__(
        self,
        service_factory: Callable[[], PersistentAgentService] = PersistentAgentService.create_default,
        model_catalog: Callable[[], dict[str, Any]] = configured_model_catalog,
        manager_factory: ManagerFactory = _default_manager,
        push: PushSink | None = None,
    ) -> None:
        self._service_factory = service_factory
        self._model_catalog = model_catalog
        self._manager_factory = manager_factory
        self._stream = None if push is None else StreamPublisher(push)
        self._service: PersistentAgentService | None = None
        self._service_lock = threading.Lock()
        self._manager: AgentManager | None = None
        self._conversations: WebConversationStore | None = None
        self._manager_lock = asyncio.Lock()

    def _colleagues(self) -> PersistentAgentService:
        with self._service_lock:
            if self._service is None:
                self._service = self._service_factory()
            return self._service

    async def _runtime(self) -> AgentManager:
        """Start the runtime on first use and recover the Requests a previous process left unfinished.

        Colleagues are republished first, so new Requests use what this host opens.
        """
        async with self._manager_lock:
            if self._manager is None:
                service = await asyncio.to_thread(self._colleagues)
                await asyncio.to_thread(service.refresh_published, WEB_NAMESPACE)
                observer = self._stream.observe if self._stream is not None else _ignore
                manager = await asyncio.to_thread(self._manager_factory, service, on_event=observer)
                if self._stream is not None:
                    self._stream.start()
                await manager.start()
                await manager.recover(WEB_NAMESPACE)
                self._conversations = await asyncio.to_thread(WebConversationStore, manager.db_path)
                self._manager = manager
            return self._manager

    async def aclose(self) -> None:
        """Stop running Executions with the host; a later call starts a new runtime."""
        async with self._manager_lock:
            manager, self._manager = self._manager, None
        if manager is not None:
            await manager.aclose()
        if self._stream is not None:
            await self._stream.aclose()

    async def handle(self, request: AgentRequest) -> AgentResponse:
        method = request.req_method.value if request.req_method is not None else ""
        params = request.params if isinstance(request.params, dict) else {}
        try:
            if method in _RUNTIME_METHODS:
                self._watch(method, params.get("agent_id"), request)
                payload = await self._run(method, params)
                view = payload.get("request")
                if method in _REQUEST_METHODS and isinstance(view, dict):
                    self._watch(method, view["agent_id"], request)
            else:
                payload = await asyncio.to_thread(self._call, method, params)
        except (PersistentAgentError, PersistentContractError) as exc:
            return build_error_response(request, str(exc), code=exc.code)
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    def _watch(self, method: str, agent_id: Any, request: AgentRequest) -> None:
        """The connection that sent, listed or polled a conversation receives its stream.

        A sending connection is registered before the message is accepted, so the first fragments are not missed.
        """
        if self._stream is None or method == ReqMethod.JIUWENMATE_COLLEAGUE_DELETE.value:
            return
        if isinstance(agent_id, str) and agent_id:
            self._stream.watch(agent_id, request)

    async def _run(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        manager = await self._runtime()
        conversations = self._conversations
        if method == ReqMethod.JIUWENMATE_REQUEST_GET.value:
            view = await manager.request_view(WEB_NAMESPACE, _text_param(params, "request_id"))
            return {"request": view.to_dict()}
        if method == ReqMethod.JIUWENMATE_REQUEST_ABORT.value:
            view = await manager.control_request(
                WEB_NAMESPACE, _text_param(params, "request_id"), reason=params.get("reason"), access=WEB_ACCESS
            )
            return {"request": view.to_dict()}
        if method == ReqMethod.JIUWENMATE_KNOWLEDGE_LIST.value:
            pages = await asyncio.to_thread(InstalledResourceCatalog().list_knowledge)
            return {"pages": [{"name": page.name, "label": page.name} for page in pages]}
        if method == ReqMethod.JIUWENMATE_ACTIVITY_GET.value:
            report = await asyncio.to_thread(ActivityQuery(manager.db_path).report, WEB_NAMESPACE, now=_utc_now())
            return {"activity": report}
        if method == ReqMethod.JIUWENMATE_SCHEDULE_LIST.value:
            agent_id = params.get("agent_id")
            plans = await asyncio.to_thread(
                manager.schedules.list, WEB_NAMESPACE, agent_id if isinstance(agent_id, str) and agent_id else None
            )
            return {"plans": [plan.to_dict() for plan in plans]}
        if method == ReqMethod.JIUWENMATE_SCHEDULE_CREATE.value:
            plan = await asyncio.to_thread(
                manager.schedules.create, WEB_NAMESPACE, _text_param(params, "agent_id"), **_schedule_fields(params)
            )
            return {"plan": plan.to_dict()}
        if method == ReqMethod.JIUWENMATE_SCHEDULE_UPDATE.value:
            plan = await asyncio.to_thread(
                manager.schedules.update, WEB_NAMESPACE, _text_param(params, "plan_id"), **_schedule_fields(params)
            )
            return {"plan": plan.to_dict()}
        if method == ReqMethod.JIUWENMATE_SCHEDULE_RUN.value:
            request_id = await manager.schedules.run(WEB_NAMESPACE, _text_param(params, "plan_id"))
            return {"request_id": request_id}
        if method == ReqMethod.JIUWENMATE_SCHEDULE_HISTORY.value:
            report = await asyncio.to_thread(manager.schedules.history, WEB_NAMESPACE, _text_param(params, "plan_id"))
            return report
        if method == ReqMethod.JIUWENMATE_SCHEDULE_SET_ENABLED.value:
            plan = await asyncio.to_thread(
                manager.schedules.set_enabled,
                WEB_NAMESPACE,
                _text_param(params, "plan_id"),
                enabled=bool(params.get("enabled")),
            )
            return {"plan": plan.to_dict()}
        if method == ReqMethod.JIUWENMATE_SCHEDULE_DELETE.value:
            await asyncio.to_thread(manager.schedules.delete, WEB_NAMESPACE, _text_param(params, "plan_id"))
            return {"deleted": True}
        agent_id = _text_param(params, "agent_id")
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_DELETE.value:
            await manager.delete_colleague(WEB_NAMESPACE, agent_id)
            await asyncio.to_thread(conversations.purge_agent, WEB_NAMESPACE, agent_id)
            return {"deleted": True}
        if method == ReqMethod.JIUWENMATE_CONVERSATION_LIST.value:
            await self._list_legacy_conversation(manager, conversations, agent_id)
            listed = await asyncio.to_thread(conversations.list, WEB_NAMESPACE, agent_id)
            return {"conversations": [item.to_dict() for item in listed]}
        conversation_id = _text_param(params, "conversation_id")
        if method == ReqMethod.JIUWENMATE_CONVERSATION_RENAME.value:
            renamed = await asyncio.to_thread(
                conversations.rename, WEB_NAMESPACE, agent_id, conversation_id, params.get("title")
            )
            return {"conversation": renamed.to_dict()}
        if method == ReqMethod.JIUWENMATE_CONVERSATION_DELETE.value:
            await asyncio.to_thread(conversations.delete, WEB_NAMESPACE, agent_id, conversation_id, now=_utc_now())
            return {"deleted": True}
        await asyncio.to_thread(conversations.require_open, WEB_NAMESPACE, agent_id, conversation_id)
        conversation = ConversationRef.web(conversation_id)
        if method == ReqMethod.JIUWENMATE_CHAT_SEND.value:
            text = _text_param(params, "text")
            view = await manager.submit_direct_message(
                WEB_NAMESPACE,
                agent_id,
                conversation,
                message_id=_text_param(params, "message_id"),
                text=text,
                access=WEB_ACCESS,
                sender_name=WEB_SENDER_NAME,
            )
            listed = await asyncio.to_thread(
                conversations.touch, WEB_NAMESPACE, agent_id, conversation_id, text=text, now=_utc_now()
            )
            return {"request": view.to_dict(), "conversation": listed.to_dict()}
        views = await manager.conversation_requests(WEB_NAMESPACE, agent_id, conversation)
        return {"requests": [view.to_dict() for view in views]}

    @staticmethod
    async def _list_legacy_conversation(
        manager: AgentManager, conversations: WebConversationStore, agent_id: str
    ) -> None:
        """Messages sent before conversations were listed stay reachable as the oldest conversation."""
        if await asyncio.to_thread(conversations.known, WEB_NAMESPACE, agent_id, LEGACY_CONVERSATION_ID):
            return
        views = await manager.conversation_requests(
            WEB_NAMESPACE, agent_id, ConversationRef.web(LEGACY_CONVERSATION_ID)
        )
        if not views:
            return
        first, last = views[0].state.record, views[-1].state.record
        await asyncio.to_thread(
            conversations.adopt,
            WEB_NAMESPACE,
            agent_id,
            LEGACY_CONVERSATION_ID,
            text=first.request_input.text,
            created_at=first.received_at,
            updated_at=last.received_at,
        )

    def _call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method == ReqMethod.JIUWENMATE_MODELS_LIST.value:
            return self._model_catalog()
        service = self._colleagues()
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT.value:
            view = service.ensure_default_colleague(WEB_NAMESPACE)
            return {"colleague": None if view is None else view.to_dict()}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_LIST.value:
            return {"colleagues": [view.to_dict() for view in service.list_colleagues(WEB_NAMESPACE)]}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_CREATE.value:
            return {"colleague": service.create_colleague(WEB_NAMESPACE, _settings(params)).to_dict()}
        agent_id = _text_param(params, "agent_id")
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_GET.value:
            return {"colleague": service.get_colleague(WEB_NAMESPACE, agent_id).to_dict()}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_SETTINGS_GET.value:
            return {"settings": service.read_settings(WEB_NAMESPACE, agent_id).to_dict()}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_SETTINGS_UPDATE.value:
            return {"colleague": service.update_settings(WEB_NAMESPACE, agent_id, _settings(params)).to_dict()}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_PUBLISH.value:
            return {"version": service.publish(WEB_NAMESPACE, agent_id).to_dict()}
        raise PersistentAgentError(f"unsupported method {method!r}")
