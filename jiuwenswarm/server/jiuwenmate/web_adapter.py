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
from jiuwenswarm.server.jiuwenmate.channel_records import ChannelRecords
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import configured_feishu_bindings, feishu_member_names
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef, PersistentContractError
from jiuwenswarm.server.jiuwenmate.models import ColleagueSettings, PersistentAgentError
from jiuwenswarm.server.jiuwenmate.model_refresh import ModelRefreshQueue
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WORKSPACE_READ, WORKSPACE_WRITE
from jiuwenswarm.server.jiuwenmate.resource_assembly import InstalledResourceCatalog
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService
from jiuwenswarm.server.jiuwenmate.web_conversations import WebConversationStore
from jiuwenswarm.server.jiuwenmate.agent_mail_runtime import AgentMailScope
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
        ReqMethod.JIUWENMATE_COLLEAGUE_SET_ENABLED.value,
    }
)
_RUNTIME_METHODS = frozenset(
    {
        ReqMethod.JIUWENMATE_CHAT_SEND.value,
        ReqMethod.JIUWENMATE_CHAT_HISTORY.value,
        ReqMethod.JIUWENMATE_REQUEST_GET.value,
        ReqMethod.JIUWENMATE_REQUEST_ABORT.value,
        ReqMethod.JIUWENMATE_PERMISSION_DECIDE.value,
        ReqMethod.JIUWENMATE_SHARING_LIST.value,
        ReqMethod.JIUWENMATE_SHARING_DECIDE.value,
        ReqMethod.JIUWENMATE_SHARING_REVOKE.value,
        ReqMethod.JIUWENMATE_CONVERSATION_LIST.value,
        ReqMethod.JIUWENMATE_CONVERSATION_RENAME.value,
        ReqMethod.JIUWENMATE_CONVERSATION_PIN.value,
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
        ReqMethod.JIUWENMATE_CHANNEL_LIST.value,
        ReqMethod.JIUWENMATE_CHANNEL_MESSAGES.value,
        ReqMethod.JIUWENMATE_CHANNEL_TRACE.value,
        ReqMethod.JIUWENMATE_AGENT_MAIL_STATUS.value,
        ReqMethod.JIUWENMATE_AGENT_MAIL_AUTH_BEGIN.value,
        ReqMethod.JIUWENMATE_AGENT_MAIL_AUTH_POLL.value,
        ReqMethod.JIUWENMATE_AGENT_MAIL_AUTH_CANCEL.value,
        ReqMethod.JIUWENMATE_AGENT_MAIL_DISCONNECT.value,
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _bound_apps() -> dict[str, set[tuple[str, str]]]:
    """The ``(app, agent)`` pairs the owner's channel configuration binds now, per platform."""
    from jiuwenswarm.server.jiuwenmate.dingtalk_inbound import configured_dingtalk_bindings

    from jiuwenswarm.server.jiuwenmate.welink_inbound import configured_welink_bindings, welink_connection_key

    welink = configured_welink_bindings()
    return {
        "welink": {(welink_connection_key(item.account, item.config_id), item.agent_id) for item in welink},
        "feishu": {(binding.app_id, binding.agent_id) for binding in configured_feishu_bindings()},
        "dingtalk": {(binding.app_id, binding.agent_id) for binding in configured_dingtalk_bindings()},
    }


def _colleague_wire(view, service: PersistentAgentService, bound: Mapping[str, set[tuple[str, str]]]) -> dict[str, Any]:
    """A colleague with its connections: the Web agent first, then each IM app its configuration binds."""
    payload = view.to_dict()
    connections = [{"channel": "web", "connection_key": "web", "agent_id": view.agent_id,
                    "workspace_dir": view.workspace_dir}]
    for item in service.connections(WEB_NAMESPACE, view.agent_id):
        # An app the configuration no longer binds keeps its agent and data, but is not shown.
        if (item.connection_key, item.agent_id) in bound.get(item.channel, ()):
            connections.append({
                "channel": item.channel,
                "connection_key": item.connection_key,
                "agent_id": item.agent_id,
                "workspace_dir": str(service.workspaces.path(WEB_NAMESPACE, item.agent_id)),
            })
    for platform in ("feishu", "dingtalk"):
        seen_apps = {item["connection_key"] for item in connections if item["channel"] == platform}
        for app_id, bound_agent_id in sorted(bound.get(platform, ())):
            if bound_agent_id == view.agent_id and app_id not in seen_apps:
                connections.append({
                    "channel": platform,
                    "connection_key": app_id,
                    "agent_id": view.agent_id,
                    "workspace_dir": view.workspace_dir,
                })
    payload["connections"] = connections
    payload["im_configured"] = len(connections) > 1
    return payload


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


def _optional_text_param(params: Mapping[str, Any], name: str) -> str | None:
    value = params.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PersistentAgentError(f"{name} must be text")
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
    return AgentManager.create_default(service, on_event=on_event, approval_channels=frozenset({"web"}))


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


def _channel_records(manager: AgentManager) -> ChannelRecords:
    """Group records for the page, with colleague names and Feishu member names."""
    colleagues = manager._service.list_agents("local")
    return ChannelRecords(
        manager.group_chats,
        manager.group_archive,
        manager.sessions,
        agent_names={item.agent_id: item.name for item in colleagues},
        app_agents={item.app_id: item.agent_id for item in configured_feishu_bindings()},
        member_names=feishu_member_names,
    )


class JiuwenMateWebAdapter(GatewayAdapter):
    methods = _COLLEAGUE_METHODS | _RUNTIME_METHODS | {
        ReqMethod.JIUWENMATE_MODELS_LIST.value, ReqMethod.MODELS_REFRESH_STATUS.value,
    }

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
        self._connection_operation_locks: dict[str, asyncio.Lock] = {}
        self._model_refresh_lock = asyncio.Lock()
        self._model_refresh = ModelRefreshQueue(lambda payload: self.refresh_models(payload))

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
                await self.wait_model_refresh()
                service = await asyncio.to_thread(self._colleagues)
                await self.refresh_models()
                observer = self._stream.observe if self._stream is not None else _ignore
                manager = await asyncio.to_thread(self._manager_factory, service, on_event=observer)
                if self._stream is not None:
                    self._stream.start()
                await manager.start()
                # Feishu routes continue on the fanout runtime, which can verify group membership.
                await manager.recover(WEB_NAMESPACE, channels=frozenset({"web"}), include_unrouted=True)
                self._conversations = await asyncio.to_thread(WebConversationStore, manager.db_path)
                self._manager = manager
            return self._manager

    async def refresh_models(self, config_payload: dict[str, Any] | None = None) -> bool:
        async with self._model_refresh_lock:
            return await self._refresh_models(config_payload)

    def schedule_model_refresh(self, config_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        models = config_payload if isinstance(config_payload, dict) and "models" in config_payload else None
        return self._model_refresh.schedule(models)

    def model_refresh_status(self) -> dict[str, Any]:
        return self._model_refresh.status()

    async def wait_model_refresh(self) -> None:
        await self._model_refresh.wait()

    async def _refresh_models(self, config_payload: dict[str, Any] | None = None) -> bool:
        """Refresh published models for future Web and Feishu Requests, without starting either runtime."""
        from jiuwenswarm.common.config import get_default_models
        from jiuwenswarm.server.jiuwenmate.service import ConfiguredModelProvider, default_data_dir

        if self._service is None and not (default_data_dir() / "agents.sqlite3").is_file():
            return True
        try:
            service = await asyncio.to_thread(self._colleagues)
            models = (
                ConfiguredModelProvider(lambda: get_default_models(config_payload))
                if isinstance(config_payload, dict) and "models" in config_payload else None
            )
            return await asyncio.to_thread(service.refresh_published, WEB_NAMESPACE, models=models)
        except Exception as exc:
            logger.warning("[JiuwenMate] model refresh failed: error_type=%s", type(exc).__name__)
            return False

    async def attach_connection(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Gateway asks for the agent of one IM app before it writes that binding."""
        config_id = _text_param(params, "config_id")
        lock = self._connection_operation_locks.setdefault(config_id, asyncio.Lock())
        async with lock:
            return await self._attach_connection(params)

    async def _attach_connection(self, params: Mapping[str, Any]) -> dict[str, Any]:
        service = await asyncio.to_thread(self._colleagues)
        agent_id = await asyncio.to_thread(
            service.attach_connection,
            WEB_NAMESPACE,
            _text_param(params, "config_id"),
            _text_param(params, "channel"),
            _text_param(params, "connection_key"),
        )
        return {"agent_id": agent_id}

    async def colleague_connections(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Every IM agent of a colleague, so Gateway can remove the apps bound to them.

        A hidden colleague still lists them; one already purged reports ``removed`` with none,
        so a deletion whose answer was lost can be repeated as it was sent.
        """
        service = await asyncio.to_thread(self._colleagues)
        records = await asyncio.to_thread(
            service.removal_connections, WEB_NAMESPACE, _text_param(params, "config_id"),
        )
        return {
            "connections": [
                {"channel": item.channel, "connection_key": item.connection_key, "agent_id": item.agent_id}
                for item in records or ()
            ],
            "removed": records is None,
        }

    async def remove_colleague(
        self, params: Mapping[str, Any], *, im_runtime: AgentManager | None = None,
    ) -> dict[str, Any]:
        """Serialize with binding even if Gateway has stopped waiting for deletion."""
        config_id = _text_param(params, "config_id")
        lock = self._connection_operation_locks.setdefault(config_id, asyncio.Lock())
        async with lock:
            return await self._remove_colleague(params, im_runtime=im_runtime)

    async def _remove_colleague(
        self, params: Mapping[str, Any], *, im_runtime: AgentManager | None = None,
    ) -> dict[str, Any]:
        """Hide a colleague and keep its data (``unbind``), or delete every agent and then the colleague (``purge``).

        Gateway has already removed the IM apps bound to its agents. Work of an agent ends in both
        runtimes before its data is removed, and each agent's rows go after its files, so after a
        failure the remaining agents are found again. The colleague's own row goes last and keeps
        a removal record; repeating a finished deletion succeeds.
        """
        config_id = _text_param(params, "config_id")
        mode = params.get("mode")
        if mode not in ("unbind", "purge"):
            raise PersistentAgentError("mode must be unbind or purge")
        service = await asyncio.to_thread(self._colleagues)
        pending = await asyncio.to_thread(service.removal_connections, WEB_NAMESPACE, config_id)
        if pending is None:
            return {"removed": True}
        if mode == "unbind":
            await asyncio.to_thread(service.hide_colleague, WEB_NAMESPACE, config_id)
            return {"removed": True}
        manager = await self._runtime()
        # Connections added while earlier ones were removed are read again; the colleague's row
        # cannot be deleted while any remain.
        for _ in range(3):
            if not pending:
                break
            for item in pending:
                await self._purge_agent(manager, item.agent_id, im_runtime)
            pending = await asyncio.to_thread(service.removal_connections, WEB_NAMESPACE, config_id) or []
        if pending:
            raise PersistentAgentError("connections are still being added to this colleague; retry", code="CONFLICT")
        await self._purge_agent(manager, config_id, im_runtime, record_removal=True)
        return {"removed": True}

    async def _purge_agent(
        self, manager: AgentManager, agent_id: str, im_runtime: AgentManager | None, *, record_removal: bool = False,
    ) -> None:
        if im_runtime is not None:
            await im_runtime.scheduler.end_agent(WEB_NAMESPACE, agent_id)
        try:
            await asyncio.to_thread(self._conversations.purge_agent, WEB_NAMESPACE, agent_id)
            await manager.delete_colleague(WEB_NAMESPACE, agent_id, record_removal=record_removal)
        finally:
            if im_runtime is not None:
                im_runtime.scheduler.release_agent(WEB_NAMESPACE, agent_id)

    async def prepare_feishu_cli(self, agent_id: str) -> dict[str, object]:
        manager = await self._runtime()
        return manager.schedule_feishu_cli(WEB_NAMESPACE, agent_id)

    async def aclose(self) -> None:
        """Stop running Executions with the host; a later call starts a new runtime."""
        await self._model_refresh.aclose()
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
            if method == ReqMethod.MODELS_REFRESH_STATUS.value:
                payload = self.model_refresh_status()
            elif method in _RUNTIME_METHODS:
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
        if self._stream is None:
            return
        if isinstance(agent_id, str) and agent_id:
            self._stream.watch(agent_id, request)

    async def _run(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        manager = await self._runtime()
        conversations = self._conversations
        if method.startswith("jiuwenmate.agent_mail."):
            agent_id = _text_param(params, "agent_id")
            await asyncio.to_thread(self._colleagues().get_colleague, WEB_NAMESPACE, agent_id)
            scope = AgentMailScope(WEB_NAMESPACE, agent_id)
            if method == ReqMethod.JIUWENMATE_AGENT_MAIL_STATUS.value:
                return {"agent_mail": await manager.agent_mail_runtime.status(scope)}
            if method == ReqMethod.JIUWENMATE_AGENT_MAIL_AUTH_BEGIN.value:
                return {"authorization": await manager.agent_mail_runtime.begin_auth(scope)}
            if method == ReqMethod.JIUWENMATE_AGENT_MAIL_AUTH_POLL.value:
                return {
                    "agent_mail": await manager.agent_mail_runtime.poll_auth(
                        scope, _text_param(params, "operation_id")
                    )
                }
            if method == ReqMethod.JIUWENMATE_AGENT_MAIL_AUTH_CANCEL.value:
                return {
                    "agent_mail": await manager.agent_mail_runtime.cancel_auth(
                        scope, _text_param(params, "operation_id")
                    )
                }
            if method == ReqMethod.JIUWENMATE_AGENT_MAIL_DISCONNECT.value:
                return {"agent_mail": await manager.agent_mail_runtime.disconnect(scope)}
        if method == ReqMethod.JIUWENMATE_REQUEST_GET.value:
            view = await manager.request_view(WEB_NAMESPACE, _text_param(params, "request_id"))
            return {"request": view.to_dict()}
        if method == ReqMethod.JIUWENMATE_REQUEST_ABORT.value:
            view = await manager.control_request(
                WEB_NAMESPACE, _text_param(params, "request_id"), reason=params.get("reason"), access=WEB_ACCESS
            )
            return {"request": view.to_dict()}
        if method == ReqMethod.JIUWENMATE_PERMISSION_DECIDE.value:
            decision = params.get("decision")
            if decision not in ("approve", "reject"):
                raise PersistentAgentError("invalid permission decision")
            view = await manager.decide_permission_approval(
                _text_param(params, "approval_id"),
                decision=decision,
                access=WEB_ACCESS,
                channel="web",
                target_id=_text_param(params, "conversation_id"),
                tool_call_id=params.get("tool_call_id") if isinstance(params.get("tool_call_id"), str) else None,
                approver_agent_id=params.get("agent_id") if isinstance(params.get("agent_id"), str) else None,
            )
            return {"request": view.to_dict()}
        if method == ReqMethod.JIUWENMATE_SHARING_LIST.value:
            return await manager.sharing_list(WEB_NAMESPACE, _text_param(params, "agent_id"), access=WEB_ACCESS)
        if method == ReqMethod.JIUWENMATE_SHARING_DECIDE.value:
            return await manager.decide_sharing(
                WEB_NAMESPACE, _text_param(params, "agent_id"), _text_param(params, "approval_id"),
                decision=_text_param(params, "decision"), access=WEB_ACCESS,
            )
        if method == ReqMethod.JIUWENMATE_SHARING_REVOKE.value:
            return await manager.revoke_sharing(
                WEB_NAMESPACE, _text_param(params, "agent_id"), _text_param(params, "grant_id"), access=WEB_ACCESS,
            )
        if method == ReqMethod.JIUWENMATE_KNOWLEDGE_LIST.value:
            pages = await asyncio.to_thread(InstalledResourceCatalog().list_knowledge)
            return {"pages": [{"name": page.name, "label": page.name} for page in pages]}
        if method == ReqMethod.JIUWENMATE_CHANNEL_LIST.value:
            records = _channel_records(manager)
            channels = await asyncio.to_thread(records.list_channels, WEB_NAMESPACE, _text_param(params, "agent_id"))
            return {"channels": channels}
        if method == ReqMethod.JIUWENMATE_CHANNEL_MESSAGES.value:
            records = _channel_records(manager)
            report = await asyncio.to_thread(
                records.messages, WEB_NAMESPACE, _text_param(params, "agent_id"), _text_param(params, "group_id"),
                _optional_text_param(params, "cursor"),
            )
            return report
        if method == ReqMethod.JIUWENMATE_CHANNEL_TRACE.value:
            records = _channel_records(manager)
            report = await asyncio.to_thread(
                records.trace, WEB_NAMESPACE, _text_param(params, "agent_id"), _text_param(params, "group_id"),
            )
            return report
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
        if method == ReqMethod.JIUWENMATE_CONVERSATION_LIST.value:
            await self._list_legacy_conversation(manager, conversations, agent_id)
            listed = await asyncio.to_thread(conversations.list, WEB_NAMESPACE, agent_id)

            async def summarize(item):
                last_message = await manager.latest_conversation_message(
                    WEB_NAMESPACE, agent_id, ConversationRef.web(item.conversation_id)
                )
                return {**item.to_dict(), "last_message": last_message}

            return {"conversations": await asyncio.gather(*(summarize(item) for item in listed))}
        conversation_id = _text_param(params, "conversation_id")
        if method == ReqMethod.JIUWENMATE_CONVERSATION_RENAME.value:
            renamed = await asyncio.to_thread(
                conversations.rename, WEB_NAMESPACE, agent_id, conversation_id, params.get("title")
            )
            return {"conversation": renamed.to_dict()}
        if method == ReqMethod.JIUWENMATE_CONVERSATION_PIN.value:
            pinned = await asyncio.to_thread(
                conversations.set_pinned,
                WEB_NAMESPACE,
                agent_id,
                conversation_id,
                params.get("pinned"),
            )
            return {"conversation": pinned.to_dict()}
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
            return {
                "request": view.to_dict(),
                "conversation": {**listed.to_dict(), "last_message": text},
            }
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
        bound = _bound_apps()
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT.value:
            view = service.ensure_default_colleague(WEB_NAMESPACE)
            return {"colleague": None if view is None else _colleague_wire(view, service, bound)}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_LIST.value:
            return {"colleagues": [
                _colleague_wire(view, service, bound) for view in service.list_colleagues(WEB_NAMESPACE)
            ]}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_CREATE.value:
            template_id = params.get("template_id")
            if template_id is not None and not isinstance(template_id, str):
                raise PersistentAgentError("template_id is required")
            source_template_id = params.get("source_template_id")
            if source_template_id is not None and not isinstance(source_template_id, str):
                raise PersistentAgentError("source_template_id is required")
            return {
                "colleague": _colleague_wire(
                    service.create_colleague(
                        WEB_NAMESPACE,
                        _settings(params),
                        template_id=template_id or None,
                        source_template_id=source_template_id or None,
                        unique_source=params.get("unique_source") is True,
                    ),
                    service,
                    bound,
                )
            }
        # Management works on a colleague's configuration, whose ID is its Web agent's.
        agent_id = _text_param(params, "agent_id")
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_GET.value:
            return {"colleague": _colleague_wire(service.get_config(WEB_NAMESPACE, agent_id), service, bound)}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_SETTINGS_GET.value:
            service.get_config(WEB_NAMESPACE, agent_id)
            return {"settings": service.read_settings(WEB_NAMESPACE, agent_id).to_dict()}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_SETTINGS_UPDATE.value:
            updated = service.update_settings(WEB_NAMESPACE, agent_id, _settings(params))
            return {"colleague": _colleague_wire(updated, service, bound)}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_PUBLISH.value:
            return {"version": service.publish(WEB_NAMESPACE, agent_id).to_dict()}
        if method == ReqMethod.JIUWENMATE_COLLEAGUE_SET_ENABLED.value:
            enabled = params.get("enabled")
            if not isinstance(enabled, bool):
                raise PersistentAgentError("enabled must be a boolean")
            updated = service.set_enabled(WEB_NAMESPACE, agent_id, enabled=enabled)
            return {"colleague": _colleague_wire(updated, service, bound)}
        raise PersistentAgentError(f"unsupported method {method!r}")
