"""Video RPC/presentation and Agent RPC adapters for the shared Host task service."""

import asyncio
import getpass
import time
from urllib.parse import urlsplit

from jiuwenswarm.runtime.tasks import TaskService, TaskStore
from jiuwenswarm.runtime.tasks.store import task_database
from .qwen_omni_tools import parse_qwen_omni_tool_call


def task_identity(ws, scope):
    """Local product authority comes from the server, never a user_id argument."""
    remote = getattr(ws, "remote_address", None)
    headers = getattr(getattr(ws, "request", None), "headers", None) or getattr(
        ws, "request_headers", {}
    )
    origin = urlsplit(headers.get("Origin") or "")
    if (
        not remote
        or remote[0] not in {"127.0.0.1", "::1"}
        or origin.hostname not in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("Task management requires a local browser connection")
    if (
        not isinstance(scope, str)
        or not scope
        or len(scope) > 200
        or any(c in scope for c in "/\\\x00")
    ):
        raise ValueError("Invalid task conversation")
    owner = "local:" + getpass.getuser()
    auth_id = getattr(ws, "_jiuwen_auth_session", None)
    if auth_id:
        from jiuwenswarm.common.auth.service import get_auth_service

        auth = get_auth_service().resolve_session(auth_id)
        if auth is None:
            raise ValueError("Login expired")
        owner = auth.user_id
    if scope.startswith("task-duplex:"):
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )
        from jiuwenswarm.server.runtime.session import lifecycle

        session = scope.removeprefix("task-duplex:")
        if (
            session in {".", ".."}
            or any(c in session for c in '<>:"|?*')
            or session.endswith((".", " "))
        ):
            raise ValueError("Invalid saved conversation identity")
        metadata = get_session_metadata(
            session, cache_bust=True, enable_writeback=False
        )
        if (
            not metadata
            or session == "new"
            or (metadata.get("user_id") and metadata["user_id"] != owner)
        ):
            raise ValueError("Conversation is unavailable to this user")
        if lifecycle.state("session", session).get("write_blocked"):
            raise ValueError("Conversation is closing")
    return owner, scope


class AgentTaskExecutor:
    def __init__(self, client, store, normalize_media):
        self.client, self.store, self.normalize_media = client, store, normalize_media

    async def run(self, task, progress):
        from . import video_search

        with self.store.transaction() as db:
            completed = [
                t
                for t in self.store.rows(db, task["owner"], task["session"])
                if t["status"] == "completed" and t["result"]
            ]
        context = [
            {"question": t["instruction"], "result": t["result"].get("answer", "")}
            for t in completed[-6:]
        ]
        if task["request"].get("prior_result"):
            context.append(
                {
                    "question": "Previous revision",
                    "result": task["request"]["prior_result"].get("answer", ""),
                }
            )
        return await video_search.execute_core_agent(
            self.client,
            question=task["instruction"],
            query=task["request"].get("query") or task["instruction"],
            visual_context=task["request"].get("visual_context", ""),
            search_session_id=task["session"],
            core_session_id=task["core_session_id"],
            request_id=task["request_id"],
            user_id=None if task["owner"].startswith("local:") else task["owner"],
            delegation_context=context,
            frame_data_url=task["request"].get("frame_data_url", ""),
            normalize_media_attachments=self.normalize_media,
            on_progress=progress,
        )

    async def cancel(self, task):
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        client = (
            self.client.get("value") if isinstance(self.client, dict) else self.client
        )
        request = e2a_from_agent_fields(
            request_id="cancel-" + task["request_id"],
            channel_id="video_tool",
            session_id=task["core_session_id"],
            req_method=ReqMethod.CHAT_CANCEL,
            user_id=None if task["owner"].startswith("local:") else task["owner"],
            params={"intent": "cancel", "mode": "agent", "work_mode": "work"},
            is_stream=False,
            timestamp=time.time(),
        )
        response = await asyncio.wait_for(client.send_request(request), 20)
        if not response.ok or (response.payload or {}).get("success") is False:
            raise RuntimeError("Agent has not confirmed the stop request")
        while not self.store.read(task["id"])["execution_settled"]:
            await asyncio.sleep(0.2)


class VideoSearchManager:
    """Only transport and provider projection; TaskService owns all task state."""

    def __init__(
        self,
        channel,
        agent_client,
        *,
        normalize_media_attachments=None,
        log_event,
        qwen_active,
        max_concurrency=2,
        path=None,
        authorize=task_identity,
    ):
        self.channel, self.log_event, self.qwen_active = channel, log_event, qwen_active
        self.client, self.normalize_media = agent_client, normalize_media_attachments
        self.path, self.authorize, self.concurrency = path, authorize, max_concurrency
        self._service = None
        self.subscribers = {}

    @property
    def service(self):
        if self._service is None:
            store = TaskStore(self.path or task_database())
            self._service = TaskService(
                store,
                AgentTaskExecutor(self.client, store, self.normalize_media),
                on_change=self.changed,
                concurrency=self.concurrency,
            )
        return self._service

    def scope(self, ws, params):
        owner, session = self.authorize(ws, params.get("search_session_id", ""))
        self.subscribers[(owner, session, id(ws))] = ws
        return owner, session

    @staticmethod
    def public(task):
        request, result = task["request"], task.get("result") or {}
        return dict(
            engine="Jiuwen Core Agent",
            id=task["id"],
            job_id=task["id"],
            search_session_id=task["session"],
            question=task["instruction"],
            query=task["request"].get("query") or task["instruction"],
            status=task["status"],
            revision=task["revision"],
            sequence=task["sequence"],
            result=result.get("answer", ""),
            display_result=result.get("display_result", ""),
            realtime_brief=result.get("realtime_brief"),
            progress_history=task["progress"],
            progress=task["progress"][-1] if task["progress"] else None,
            error=task["error"],
            adjustments=task["changes"],
            successor_id=task["successor_id"],
            parent_id=task["parent_id"],
            tool_call_id=request.get("tool_call_id", ""),
            tool_name=request.get("tool_name", ""),
            turn_id=request.get("turn_id", ""),
            reused=task.get("reused", False),
        )

    def snapshot(self, owner, scope):
        tasks, version = self.service.snapshot(owner, scope)
        queued = sorted(
            (t for t in tasks if t["status"] == "queued"), key=lambda t: t["position"]
        )
        positions = {t["id"]: i + 1 for i, t in enumerate(queued)}
        return {
            "search_session_id": scope,
            "queue_version": version,
            "jobs": [
                {
                    **self.public(t),
                    "queue_version": version,
                    "replay": True,
                    "queue_position": positions.get(t["id"], 0),
                }
                for t in tasks
            ],
        }

    async def changed(self, task):
        state = task["status"]
        if state in {"failed", "completed", "cancelled"}:
            self.log_event({"stage": "search_" + state, "job_id": task["id"]})
        event = state if state in {"completed", "failed", "cancelled"} else "progress"
        for key, ws in list(self.subscribers.items()):
            if key[:2] != (task["owner"], task["session"]):
                continue
            try:
                self.authorize(ws, task["session"])
                await self.channel.send_event(
                    ws, "video.search." + event, self.public(task)
                )
                await self.channel.send_event(
                    ws, "video.search.queue", self.snapshot(*key[:2])
                )
            except Exception:
                self.subscribers.pop(key, None)

    def start(
        self,
        ws,
        *,
        question,
        query,
        search_session_id,
        visual_context="",
        frame_data_url="",
        tool_call_id="",
        tool_name="",
        turn_id="",
        command_id="",
    ):
        owner, scope = self.scope(ws, {"search_session_id": search_session_id})
        request = dict(
            query=query,
            visual_context=visual_context,
            frame_data_url=frame_data_url,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            turn_id=turn_id,
        )
        task = self.service.submit(
            owner, scope, command_id or tool_call_id, question or query, request
        )
        return self.public(task)

    async def _respond(self, ws, req_id, action):
        try:
            payload = await action()
        except Exception as exc:
            await self.channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="TASK_REQUEST_REJECTED"
            )
            return
        await self.channel.send_response(ws, req_id, ok=True, payload=payload)

    async def handle_qwen_tool(self, ws, req_id, params, session_id):
        async def run():
            owner, scope = self.scope(ws, params)
            if not self.qwen_active():
                raise ValueError("Qwen Omni Realtime is not the active provider")
            call = parse_qwen_omni_tool_call(params)
            if call.name in {"jiuwen_delegate", "jiuwen_research"}:
                from .video_search import _frame_media_item, MAX_FRAME_CHARS

                frame = params.get("frame_data_url", "")
                if frame and (
                    len(frame) > MAX_FRAME_CHARS or _frame_media_item(frame) is None
                ):
                    raise ValueError("Invalid video frame")
                task = self.start(
                    ws,
                    question=params.get("question") or call.task,
                    query=call.task,
                    search_session_id=scope,
                    frame_data_url=frame,
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                    turn_id=params.get("turn_id", ""),
                )
                return {"search_job": task, "call_id": call.call_id}
            return {
                "tool_result": await self.operate(owner, scope, call),
                "call_id": call.call_id,
            }

        await self._respond(ws, req_id, run)

    async def operate(self, owner, scope, call):
        args = call.arguments
        if call.name == "jiuwen_task_query":
            if args.get("job_id"):
                tasks, cursor = [self.service.get(owner, scope, args["job_id"])], None
            else:
                tasks, cursor = self.service.list(
                    owner,
                    scope,
                    query=args.get("query", ""),
                    offset=args.get("offset", 0),
                    limit=20,
                )
            result = {"jobs": [self.public(t) for t in tasks], "next_offset": cursor}
        elif call.name == "jiuwen_task_cancel":
            result = await self.service.cancel(
                owner, scope, args["job_id"], call.call_id
            )
        else:
            result = self.service.modify(
                owner,
                scope,
                args["job_id"],
                call.call_id,
                args["revision"],
                args["instruction"],
            )
        return result

    async def handle_status(self, ws, req_id, params, session_id):
        async def run():
            owner, scope = self.scope(ws, params)
            return self.public(self.service.get(owner, scope, params.get("job_id")))

        await self._respond(ws, req_id, run)

    async def handle_list(self, ws, req_id, params, session_id):
        async def run():
            owner, scope = self.scope(ws, params)
            tasks, cursor = self.service.list(
                owner, scope, offset=params.get("offset", 0)
            )
            return {
                "jobs": [self.public(t) for t in tasks],
                "next_offset": cursor,
                "replay": True,
            }

        await self._respond(ws, req_id, run)

    async def handle_control(self, ws, req_id, params, session_id):
        async def run():
            owner, scope = self.scope(ws, params)
            task_id, action = params.get("job_id"), params.get("action")
            if action == "cancel":
                await self.service.cancel(
                    owner, scope, task_id, params.get("command_id") or str(req_id)
                )
            elif action in {"next", "before"}:
                self.service.reorder(
                    owner,
                    scope,
                    task_id,
                    params.get("queue_version"),
                    params.get("before_job_id"),
                )
            elif action == "preempt":
                await self.service.preempt(
                    owner,
                    scope,
                    task_id,
                    params.get("queue_version"),
                    params.get("command_id") or str(req_id),
                )
            elif action == "modify":
                self.service.modify(
                    owner,
                    scope,
                    task_id,
                    params.get("command_id") or str(req_id),
                    params.get("revision"),
                    params.get("instruction"),
                )
            else:
                raise ValueError(
                    "Unsupported control; stop the active task before reordering"
                )
            snapshot = self.snapshot(owner, scope)
            await self.channel.send_event(ws, "video.search.queue", snapshot)
            return snapshot

        await self._respond(ws, req_id, run)

    async def close(self):
        if self._service is not None:
            await self._service.close()
