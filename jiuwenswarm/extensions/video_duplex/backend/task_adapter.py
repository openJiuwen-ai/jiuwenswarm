"""Video RPC/presentation and Agent RPC adapters for the shared Host task service."""

import asyncio
import json
import logging
import time

from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.backend.tasks.service import TaskModification
from jiuwenswarm.extensions.video_duplex.backend.tasks.store import task_database
from .tasks.errors import QueueVersionConflict, TaskRevisionConflict
from .tasks.prompts import (
    EMPTY_QUERY_INSTRUCTIONS,
    QUEUE_CONFLICT_INSTRUCTIONS,
    REORDER_INSTRUCTIONS,
    REVISION_CONFLICT_INSTRUCTIONS,
    task_query_instructions,
)
from .qwen_omni_tools import parse_qwen_omni_tool_call

logger = logging.getLogger(__name__)
AGENT_QUERY_TIMEOUT = 20
EVENT_SEND_TIMEOUT = 2


def task_identity(ws, scope):
    """Use WebChannel's user scope; connection admission belongs to the gateway."""
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

    if not isinstance(scope, str) or not scope:
        raise ValueError("Invalid task conversation")
    if len(scope) > 200 or any(c in scope for c in "/\\\x00"):
        raise ValueError("Invalid task conversation")
    # An empty user is the existing anonymous scope, not the server's OS user.
    owner = WebChannel.connection_user_id(ws) or ""
    if scope.startswith("task-duplex:"):
        session = scope.removeprefix("task-duplex:")
        if (
            session in {".", ".."}
            or any(c in session for c in '<>:"|?*')
            or session.endswith((".", " "))
        ):
            raise ValueError("Invalid saved conversation identity")
        if session == "new":
            raise ValueError("Conversation is unavailable to this user")
    return owner, scope


async def task_agent_query(client, method, params, session, owner):
    """Read from the routed AgentServer, never from Gateway's local session tree."""
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
    from jiuwenswarm.gateway.routing.e2a_proxy import send_agent_request_with_timeout

    client = client.get("value") if isinstance(client, dict) else client
    if client is None:
        raise RuntimeError("AgentServer client is unavailable")
    response = await send_agent_request_with_timeout(
        client, e2a_from_agent_fields(
            request_id="task-query-" + str(time.time_ns()),
            req_method=method, params=params, session_id=session,
            user_id=owner or None, channel_id="video_tool", timestamp=time.time(),
        ), label="voice.task", timeout_seconds=AGENT_QUERY_TIMEOUT,
    )
    payload = response.payload or {}
    if not response.ok:
        raise RuntimeError(str(payload.get("error") or "Task AgentServer query failed"))
    return payload


class AgentTaskExecutor:
    def __init__(self, client, store, normalize_media):
        self.client, self.store, self.normalize_media = client, store, normalize_media
        from .tasks.bridge import GatewayTaskEndpoint

        self.endpoint = GatewayTaskEndpoint(store)

    async def close(self):
        self.endpoint.close()

    async def run(self, task, progress):
        independent = task["request"].get("independent", False)
        dependencies = task["request"].get("depends_on", [])
        completed = []
        with self.store.transaction() as db:
            for previous in self.store.rows(db, task["owner"], task["session"]):
                if previous["status"] != "completed" or not previous["result"]:
                    continue
                if independent and previous["id"] not in dependencies:
                    continue
                completed.append(previous)
        context = []
        for previous in completed if independent else completed[-6:]:
            result = previous["result"]
            files = [
                {"name": f.get("name", ""), "path": f.get("path", "")}
                for f in result.get("files", [])
            ]
            context.append({
                "question": previous["instruction"],
                "result": result.get("answer", ""),
                "files": files,
            })
        if task["request"].get("prior_result"):
            context.append(
                {
                    "question": "Previous revision",
                    "result": task["request"]["prior_result"].get("answer", ""),
                }
            )
        from jiuwenswarm.extensions.video_duplex.backend.tasks.service import (
            InteractionPending,
            ExecutionUncertain,
        )

        try:
            result = await self._run(task, progress, context)
            current = self.store.read(task["id"])
            if (current.get("interaction") or {}).get("state") == "pending":
                raise ExecutionUncertain(
                    "Answer channel closed before the question was answered"
                )
            if current.get("execution_bound"):
                await self.wait_settled(task)
            # Native send_file_to_user is a server push, not part of this RPC stream.
            from jiuwenswarm.common.schema.message import ReqMethod

            files = list(result.get("files") or [])
            for execution_id in [*task.get("prior_request_ids", []), task["request_id"]]:
                try:
                    payload = await task_agent_query(
                        self.client, ReqMethod.VOICE_TASK_FILES,
                        {"execution_request_id": execution_id}, task["core_session_id"], task["owner"],
                    )
                    for item in payload.get("files", []):
                        if item not in files:
                            files.append(item)
                except Exception:
                    # Keep the result and collect other rounds even if one lookup fails.
                    result["artifact_lookup_failed"] = True
                    logger.warning("Task file lookup failed for %s/%s", task["id"], execution_id, exc_info=True)
            if files:
                result["files"] = files
            return result
        except InteractionPending:
            await self.wait_settled(task)
            raise
        except Exception as exc:
            current = self.store.read(task["id"])
            if current.get("execution_bound") and not current.get("execution_settled"):
                raise ExecutionUncertain("Execution cleanup is unconfirmed; no work was replayed") from exc
            raise

    async def wait_settled(self, task):
        from jiuwenswarm.extensions.video_duplex.backend.tasks.service import ExecutionUncertain

        try:
            async with asyncio.timeout(20):
                while not self.store.read(task["id"])["execution_settled"]:
                    await asyncio.sleep(0.05)
        except TimeoutError as exc:
            raise ExecutionUncertain(
                "Execution cleanup is unconfirmed; no work was replayed"
            ) from exc

    @staticmethod
    def answer_input(task):
        from jiuwenswarm.runtime.interaction import InteractionAnswerInput

        interaction = task["interaction"]
        return InteractionAnswerInput(
            request_id=task["request_id"]
            if task.get("resume_answer")
            else "answer-" + interaction["operation_id"],
            channel_id="video_tool",
            session_id=task["core_session_id"],
            interaction_id=interaction["request_id"],
            answers=tuple(
                {
                    "question": a["question"],
                    "custom_input": a.get("answer", ""),
                    "selected_options": a.get("selected_options", []),
                }
                for a in interaction["answers"]
            ),
            source=interaction["source"],
            mode="agent",
            work_mode="work",
        )

    async def answer(self, task):
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields

        request = self.answer_input(task).to_agent_request()
        client = (
            self.client.get("value") if isinstance(self.client, dict) else self.client
        )
        response = await client.send_request(
            e2a_from_agent_fields(
                request_id=request.request_id,
                channel_id=request.channel_id,
                session_id=request.session_id,
                req_method=request.req_method,
                params=request.params,
                is_stream=False,
                timestamp=time.time(),
                user_id=task["owner"] or None,
            )
        )
        if not response.ok or (response.payload or {}).get("success") is False:
            raise RuntimeError("Agent did not accept the answer")

    async def _run(self, task, progress, context):
        from . import video_search

        return await video_search.execute_core_agent(
            self.client,
            question=task["instruction"],
            query=task["request"].get("query") or task["instruction"],
            visual_context=task["request"].get("visual_context", ""),
            search_session_id=task["session"],
            core_session_id=task["core_session_id"],
            request_id=task["request_id"],
            user_id=task["owner"] or None,
            delegation_context=context,
            frame_data_url=task["request"].get("frame_data_url", ""),
            normalize_media_attachments=self.normalize_media,
            on_progress=progress,
            interaction_answer=self.answer_input(task)
            if task.get("resume_answer")
            else None,
            brief_nonce=task["id"],
            managed_task_binding=self.endpoint.binding(task),
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
            user_id=task["owner"] or None,
            params={"intent": "cancel", "mode": "agent", "work_mode": "work",
                    "managed_task_request": task["request_id"]},
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
                notification_timeout=AGENT_QUERY_TIMEOUT + 2 * EVENT_SEND_TIMEOUT + 1,
            )
        return self._service

    async def scope(self, ws, params):
        owner, session = await self.authorized_scope(ws, params.get("search_session_id", ""))
        self.subscribers[(owner, session, id(ws))] = ws
        return owner, session

    async def authorized_scope(self, ws, scope):
        owner, session = self.authorize(ws, scope)
        if session.startswith("task-duplex:"):
            from jiuwenswarm.common.schema.message import ReqMethod

            saved_session = session.removeprefix("task-duplex:")
            metadata = await task_agent_query(
                self.client, ReqMethod.SESSION_GET_METADATA,
                {"session_id": saved_session}, saved_session, owner,
            )
            if not metadata or str(metadata.get("user_id") or "").strip() != owner:
                raise ValueError("Conversation is unavailable to this user")
        return owner, session

    @staticmethod
    def public(task):
        request, result = task["request"], task.get("result") or {}
        files = result.get("files") or []
        if not files:
            for entry in task["progress"]:
                if entry.get("stage") == "file":
                    files.extend(entry.get("files", []))
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
            files=files,
            artifact_lookup_failed=bool(result.get("artifact_lookup_failed")),
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
            independent=request.get("independent", False),
            depends_on=request.get("depends_on", []),
            resources=request.get("resources", []),
            interaction=task.get("interaction"),
            wait_reason=task.get("wait_reason", ""),
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
            "max_concurrency": self.concurrency,
            "concurrency_scope": "owned_active_executions; unknown execution activity is unconfirmed",
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

    @staticmethod
    def voice_job(job):
        """Return control facts, not the UI's repeated reasoning/tool history."""
        fields = (
            "id", "job_id", "status", "revision", "queue_version", "queue_position",
            "independent", "parent_id", "successor_id", "wait_reason",
        )
        result = {key: job.get(key) for key in fields}
        for key in ("question", "query", "error"):
            text = str(job.get(key) or "")
            result[key] = text[:1000]
            if len(text) > 1000:
                result[key + "_truncated"] = True
        brief = job.get("realtime_brief") or {}
        text = str(brief.get("summary") or job.get("display_result") or job.get("result") or "")
        result["result_summary"] = text[:1200]
        result["result_truncated"] = len(text) > 1200
        changes = job.get("adjustments") or []
        result["adjustment_count"] = len(changes)
        result["adjustments"] = [
            {key: str(change.get(key) or "")[:500] for key in ("id", "state", "instruction", "error")}
            for change in changes[-3:]
        ]
        files = job.get("files") or []
        result["artifact_status"] = "reported_by_agent" if files else "not_confirmed"
        result["artifact_lookup_failed"] = bool(job.get("artifact_lookup_failed"))
        result["files"] = [
            {key: f.get(key, "") for key in ("name", "path")}
            for f in files[:5]
            if len(str(f.get("path", ""))) <= 1024 and len(str(f.get("name", ""))) <= 256
        ]
        result["files_truncated"] = len(files) != len(result["files"])
        interaction = job.get("interaction")
        if interaction and len(json.dumps(interaction, ensure_ascii=False).encode("utf-8")) > 8000:
            # Never truncate the exact question that an answer must be bound to.
            result["interaction_requires_ui"] = True
        else:
            result["interaction"] = interaction
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 12000:
            # Five jobs remain well below the provider's 256 KiB frame limit,
            # including the second JSON encoding of function_call_output.
            result = {key: job.get(key) for key in fields}
            result.update(
                question=str(job.get("question") or "")[:1000],
                details_omitted=True,
                artifact_status="not_in_response",
            )
        return result

    async def changed(self, task):
        state = task["status"]
        if state in {"failed", "completed", "cancelled"}:
            self.log_event({"stage": "search_" + state, "job_id": task["id"]})
        event = state if state in {"completed", "failed", "cancelled"} else "progress"

        async def deliver(key, ws):
            try:
                if await self.authorized_scope(ws, task["session"]) != key[:2]:
                    raise ValueError("Task subscription identity changed")
            except ValueError:
                self.subscribers.pop(key, None)
                return
            await asyncio.wait_for(self.channel.send_event(
                ws, "video.search." + event, self.public(task)
            ), EVENT_SEND_TIMEOUT)
            await asyncio.wait_for(self.channel.send_event(
                ws, "video.search.queue", self.snapshot(*key[:2])
            ), EVENT_SEND_TIMEOUT)

        # One slow connection must not consume the budget of other subscribers.
        results = await asyncio.gather(*(
            deliver(key, ws) for key, ws in list(self.subscribers.items())
            if key[:2] == (task["owner"], task["session"])
        ), return_exceptions=True)
        if any(isinstance(result, Exception) for result in results):
            raise RuntimeError("Task notification delivery was incomplete")

    async def start(
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
        scheduling=None,
    ):
        owner, scope = await self.scope(ws, {"search_session_id": search_session_id})
        request = dict(
            query=query,
            visual_context=visual_context,
            frame_data_url=frame_data_url,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            turn_id=turn_id,
        )
        request.update(scheduling or {})
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
            owner, scope = await self.scope(ws, params)
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
                task = await self.start(
                    ws,
                    question=params.get("question") or call.task,
                    query=call.task,
                    search_session_id=scope,
                    frame_data_url=frame,
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                    turn_id=params.get("turn_id", ""),
                    scheduling={
                        k: call.arguments[k]
                        for k in ("independent", "depends_on", "resources")
                        if k in call.arguments
                    },
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
            snapshot = self.snapshot(owner, scope)
            jobs = snapshot["jobs"]
            if args.get("job_id"):
                self.service.get(owner, scope, args["job_id"])  # Keep ownership validation.
                jobs = [t for t in jobs if t["id"] == args["job_id"]]
            else:
                query = args.get("query", "").casefold()
                jobs = [t for t in jobs if query in t["question"].casefold()]
            counts = {}
            for job in jobs:
                counts[job["status"]] = counts.get(job["status"], 0) + 1
            unfinished = sum(n for state, n in counts.items()
                             if state not in {"completed", "failed", "cancelled"})
            selected = jobs
            status = args.get("status", "")
            if status == "unfinished":
                selected = [t for t in jobs if t["status"] not in {"completed", "failed", "cancelled"}]
            elif status:
                selected = [t for t in jobs if t["status"] == status]
            offset = args.get("offset", 0)
            page = selected[offset:offset + 5]
            result = {
                **snapshot,
                "jobs": [self.voice_job(t) for t in page],
                "scope": {"session_id": scope, "job_id": args.get("job_id"),
                          "query": args.get("query", ""), "status": status},
                "summary": {"total": len(jobs), "status_counts": counts,
                            "unfinished": unfinished, "all_finished": bool(jobs) and unfinished == 0,
                            "all_succeeded": bool(jobs) and counts.get("completed", 0) == len(jobs)},
                "matched_total": len(selected),
                "next_offset": offset + 5 if offset + 5 < len(selected) else None,
                "message": task_query_instructions(len(jobs), counts, unfinished, len(page), len(selected)),
            }
            if not jobs:
                result["message"] = EMPTY_QUERY_INSTRUCTIONS
        elif call.name == "jiuwen_task_cancel":
            result = await self.service.cancel(
                owner, scope, args["job_id"], call.call_id
            )
        elif call.name == "jiuwen_task_reorder":
            if args["action"] == "before" and not args.get("before_job_id"):
                raise ValueError("before requires before_job_id")
            try:
                receipt = self.service.reorder(
                    owner, scope, args["job_id"], args["queue_version"],
                    args.get("before_job_id"), command_id=call.call_id,
                )
            except QueueVersionConflict as exc:
                receipt = dict(
                    task_id=args["job_id"], state="rejected", applied=False,
                    code="QUEUE_VERSION_CONFLICT", error=str(exc),
                )
            snapshot = self.snapshot(owner, scope)
            waiting = sorted((t for t in snapshot["jobs"] if t["status"] == "queued"),
                             key=lambda t: t["queue_position"])
            target = next(t for t in snapshot["jobs"] if t["id"] == args["job_id"])
            index = next((i for i, t in enumerate(waiting) if t["id"] == target["id"]), 0)
            result = {
                "operation": receipt,
                "queue_version": snapshot["queue_version"],
                "target": self.voice_job(target),
                "jobs": [self.voice_job(t) for t in waiting[max(0, index - 1):index + 4]],
                "waiting_total": len(waiting),
                "message": REORDER_INSTRUCTIONS,
            }
            if receipt.get("state") == "rejected":
                result.update(receipt, message=QUEUE_CONFLICT_INSTRUCTIONS)
        elif call.name == "jiuwen_task_answer":
            result = await self.service.answer(
                owner,
                scope,
                args["job_id"],
                call.call_id,
                args["interaction_id"],
                answers=args["answers"],
            )
        else:
            try:
                result = self.service.modify(
                    owner, scope, args["job_id"], call.call_id,
                    TaskModification(args["revision"], args["instruction"]),
                )
            except TaskRevisionConflict as exc:
                current = self.service.get(owner, scope, args["job_id"])
                result = dict(
                    state="rejected", error=str(exc), applied=False,
                    current_task=self.voice_job(self.public(current)),
                    message=REVISION_CONFLICT_INSTRUCTIONS,
                )
        return result

    async def handle_status(self, ws, req_id, params, session_id):
        async def run():
            owner, scope = await self.scope(ws, params)
            return self.public(self.service.get(owner, scope, params.get("job_id")))

        await self._respond(ws, req_id, run)

    async def handle_list(self, ws, req_id, params, session_id):
        async def run():
            owner, scope = await self.scope(ws, params)
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
            owner, scope = await self.scope(ws, params)
            task_id, action = params.get("job_id"), params.get("action")
            if action == "cancel":
                await self.service.cancel(
                    owner, scope, task_id, params.get("command_id") or str(req_id)
                )
            elif action in {"next", "before"}:
                if (action == "before") != bool(params.get("before_job_id")):
                    raise ValueError("Only before requires before_job_id")
                self.service.reorder(
                    owner,
                    scope,
                    task_id,
                    params.get("queue_version"),
                    params.get("before_job_id"),
                    command_id=params.get("command_id") or str(req_id),
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
                    TaskModification(params.get("revision"), params.get("instruction")),
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
