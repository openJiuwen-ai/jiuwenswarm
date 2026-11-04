"""Video RPC/presentation and Agent RPC adapters for the shared Host task service."""

import asyncio
import json
import logging
import time
from typing import Any

from jiuwenswarm.extensions.video_duplex.backend.tasks import TaskService, TaskStore
from jiuwenswarm.extensions.video_duplex.backend.tasks.service import TaskModification
from jiuwenswarm.extensions.video_duplex.backend.tasks.store import task_database
from .tasks.errors import CancellationSettlementTimeout, QueueVersionConflict, TaskRevisionConflict, TaskNotFound
from .tasks.interactions import approval_cards, approval_option_labels, voice_approval_answers
from .tasks.diagnostics import receipt_state, task_state, trace_task
from .tasks.prompts import (
    EMPTY_QUERY_INSTRUCTIONS,
    QUEUE_CONFLICT_INSTRUCTIONS,
    SCHEDULED_JOBS_INSTRUCTIONS,
    REORDER_INSTRUCTIONS,
    REVISION_CONFLICT_INSTRUCTIONS,
    task_query_instructions,
)
from .qwen_omni_tools import delegate_matches_input, parse_qwen_omni_tool_call
from .core_receipts import receipt_payload

logger = logging.getLogger(__name__)


def _login_credential_ref(ws, target) -> str:
    """登录下发的免费模型：把发起连接上的华为账号绑定到任务（凭据句柄，不存 token）。

    任务直发 AgentServer，不经过 Gateway 转发时挂凭据那一步；与 cron 一样创建时绑定、执行时补挂。
    """
    from jiuwenswarm.common.auth.passthrough import _is_login_model
    from jiuwenswarm.gateway.cron.controller import _login_credential_ref_from_session

    model_name = str(target.get("model_name") or "").strip()
    auth_session = str(getattr(ws, "_jiuwen_auth_session", "") or "").strip()
    if not model_name or not _is_login_model(model_name, auth_session or None):
        return ""
    return _login_credential_ref_from_session(auth_session)


async def _login_model_auth(task) -> dict[str, Any] | None:
    ref = (task.get("request") or {}).get("credential_ref")
    if not ref:
        return None
    from types import SimpleNamespace
    from jiuwenswarm.gateway.cron.scheduler import _login_model_auth_for_job

    return await asyncio.to_thread(_login_model_auth_for_job, SimpleNamespace(credential_ref=ref))


AGENT_QUERY_TIMEOUT = 20
EVENT_SEND_TIMEOUT = 2
STOP_SETTLEMENT_TIMEOUT = 90
SCHEDULED_JOBS_LIMIT = 20
# How long a voice answer to a typed chat card waits for Core to resume the turn.
CHAT_ANSWER_CONFIRM_TIMEOUT = 10


async def gateway_scheduled_jobs(owner):
    """Read the Gateway cron store with the same owner scope as the sidebar's ``cron.job.list``."""
    from jiuwenswarm.gateway.cron.controller import CronController

    # An empty owner is the anonymous single-user scope; the sidebar then lists every job too.
    return await CronController.get_instance().list_jobs(user_id=owner or None)


class AgentRequestTimeoutError(TimeoutError):
    """AgentServer unary 请求超时（与 gateway.routing.agent_request_timeout 同契约的最小子集）."""

    code = "AGENT_SERVER_TIMEOUT"


async def send_agent_request_with_timeout(
    agent_client: Any,
    env: Any,
    *,
    label: str,
    timeout_seconds: float,
) -> Any:
    """限时等待 AgentServer unary 响应（gateway 薄代理同语义的最小本地实现）。

    task 查询始终显式传 timeout_seconds，不涉及 gateway 的 TUI 通道超时策略；
    仅依赖注入的 AgentServerClient.send_request，不引用 gateway 树实现。
    """
    try:
        return await asyncio.wait_for(
            agent_client.send_request(env), timeout=timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        logger.warning(
            "[%s] AgentServer unary request timed out: request_id=%s method=%s "
            "channel=%s timeout=%ss",
            label,
            getattr(env, "request_id", ""),
            getattr(env, "method", ""),
            getattr(env, "channel", ""),
            timeout_seconds,
        )
        raise AgentRequestTimeoutError() from exc


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
        if "execution_target" not in task["request"]:
            from .mode_routing import resolve_execution_target

            target = await resolve_execution_target(self.client, task["owner"], task["session"])
            task["request"]["execution_target"] = target
            self.store.update(task["id"], lambda t: t["request"].update(execution_target=target))
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
                raise ExecutionUncertain(f"Execution cleanup is unconfirmed; no work was replayed: {exc}") from exc
            raise

    async def pending_interrupt(self, task):
        """Whether the task's chat still waits for a card answer; only saved chats share Core sessions."""
        if not task["session"].startswith("task-duplex:"):
            return False
        from jiuwenswarm.common.schema.message import ReqMethod

        payload = await task_agent_query(
            self.client, ReqMethod.VOICE_TASK_PENDING_INTERRUPT, {},
            task["core_session_id"], task["owner"],
        )
        return bool(payload.get("pending"))

    async def wait_settled(self, task):
        """Retain ownership until Core confirms settlement, including suspended turns."""
        # Stream closure is not execution completion. Match ordinary chat by
        # waiting for the actual execution fact without an extra duplex deadline.
        # User cancellation and service shutdown can still cancel this observer.
        while not self.store.read(task["id"])["execution_settled"]:
            await asyncio.sleep(0.05)

    @staticmethod
    def answer_input(task):
        from jiuwenswarm.runtime.interaction import InteractionAnswerInput

        interaction = task["interaction"]
        target = (task.get("request") or {}).get("execution_target") or {"mode": "agent", "work_mode": "work"}
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
                    **(
                        {"card_id": a["card_id"]}
                        if isinstance(a.get("card_id"), str) and a["card_id"]
                        else {}
                    ),
                }
                for a in interaction["answers"]
            ),
            source=interaction["source"],
            mode=target["mode"],
            work_mode="" if target["mode"] == "team.design" else target.get("work_mode", "work"),
            project_dir=target.get("project_dir", ""),
        )

    async def answer(self, task):
        from jiuwenswarm.common.e2a.constants import E2A_MODEL_AUTH_PARAM_KEY
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields

        request = self.answer_input(task).to_agent_request()
        model_auth = await _login_model_auth(task)
        client = (
            self.client.get("value") if isinstance(self.client, dict) else self.client
        )
        response = await client.send_request(
            e2a_from_agent_fields(
                request_id=request.request_id,
                channel_id=request.channel_id,
                session_id=request.session_id,
                req_method=request.req_method,
                params={
                    **request.params,
                    "managed_task_request": task["request_id"],
                    "managed_task_binding": self.endpoint.binding(task),
                    **({E2A_MODEL_AUTH_PARAM_KEY: model_auth} if model_auth else {}),
                },
                is_stream=False,
                timestamp=time.time(),
                user_id=task["owner"] or None,
            )
        )
        if not response.ok or (response.payload or {}).get("success") is False:
            raise RuntimeError("Agent did not accept the answer")

    async def _run(self, task, progress, context):
        from . import video_search
        from .mode_routing import resolve_execution_target

        current = await resolve_execution_target(self.client, task["owner"], task["session"])
        target = task["request"].get("execution_target", current)
        if any(target.get(key) != current.get(key) for key in ("mode", "work_mode", "project_id", "project_dir")):
            raise ValueError("Conversation mode or project changed; submit a new task")

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
            managed_task_binding=self.endpoint.binding(task),
            execution_target=target,
            model_auth=await _login_model_auth(task),
        )

    async def cancel(self, task, *, probe=False):
        """Stop the exact request; with probe=True only ask whether Core still runs it."""
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        if not probe:
            # Revoke unclaimed admission in the same transaction as bind.
            with self.store.transaction() as db:
                current = self.store.get(db, task["id"])
                if current["request_id"] != task["request_id"]:
                    raise RuntimeError("Task execution changed before cancellation")
                if current["execution_cancelled"] and current["execution_settled"]:
                    return True
                if current["status"] == "cancelling" and not current["execution_bound"]:
                    current.update(checkpoint_open=False, execution_settled=True,
                                   execution_cancelled=True, output_closed=True,
                                   sequence=current["sequence"] + 1)
                    self.store.put(db, current)
                    trace_task("cancel_before_execution_bound", task=task_state(current))
                    return True

        client = (
            self.client.get("value") if isinstance(self.client, dict) else self.client
        )
        request = e2a_from_agent_fields(
            request_id="cancel-" + task["request_id"],
            channel_id="video_tool",
            session_id=task["core_session_id"],
            req_method=ReqMethod.CHAT_CANCEL,
            user_id=task["owner"] or None,
            params={"intent": "cancel", **task["request"].get("execution_target", {"mode": "agent", "work_mode": "work"}),
                    "managed_task_request": task["request_id"],
                    **({"managed_task_probe": True} if probe else {})},
            is_stream=False,
            timestamp=time.time(),
        )
        trace_task("cancel_rpc_sent", task=task_state(task), cancel_request_id=request.request_id, probe=probe)
        response = await asyncio.wait_for(client.send_request(request), 20)
        trace_task("cancel_rpc_response", task=task_state(task), ok=response.ok, payload=receipt_state(response.payload))
        if not response.ok or (response.payload or {}).get("success") is False:
            detail = str((response.payload or {}).get("code") or (response.payload or {}).get("error") or "")[:500]
            raise RuntimeError("Agent has not confirmed the stop request" + (": " + detail if detail else ""))
        if (response.payload or {}).get("managed_task_settled"):
            # Core holds no binding, reporter or CUA for this request (#5486):
            # the execution is gone, so no settle callback will ever arrive.
            # A probe of a task the user is stopping counts as stopped (#5545).
            def settle(t):
                if not t["execution_settled"]:
                    t.update(checkpoint_open=False, execution_settled=True,
                             execution_cancelled=not probe or t["status"] == "cancelling")
            self.store.update(task["id"], settle)
            return True
        if probe:
            return False
        try:
            async with asyncio.timeout(STOP_SETTLEMENT_TIMEOUT):
                while not self.store.read(task["id"])["execution_settled"]:
                    await asyncio.sleep(0.2)
        except TimeoutError as exc:
            trace_task("cancel_settlement_timeout", task=task_state(self.store.read(task["id"])))
            raise CancellationSettlementTimeout(
                f"Stop accepted, but execution cleanup was not confirmed within {STOP_SETTLEMENT_TIMEOUT} seconds"
            ) from exc
        trace_task("cancel_settled", task=task_state(self.store.read(task["id"])))
        return False


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
        scheduled_jobs=gateway_scheduled_jobs,
    ):
        self.channel, self.log_event, self.qwen_active = channel, log_event, qwen_active
        self.client, self.normalize_media = agent_client, normalize_media_attachments
        self.path, self.authorize, self.concurrency = path, authorize, max_concurrency
        self._service = None
        self.list_scheduled_jobs = scheduled_jobs
        self.subscribers = {}
        self._approval_cards = {}
        self._execution_targets = {}
        # Typed chat cards the web channel pushed, by Core session (latest only).
        self._chat_cards = {}
        self._chat_resumed = {}
        listen = getattr(channel, "add_session_event_listener", None)
        if callable(listen):
            listen(self._on_session_event)
        self._queue_requests = {}

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
            from .mode_routing import execution_target

            self._execution_targets[(owner, session)] = execution_target(metadata)
            if len(self._execution_targets) > 128:
                self._execution_targets.pop(next(iter(self._execution_targets)))
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
            title=task.get("title", ""),
            id=task["id"],
            job_id=task["id"],
            search_session_id=task["session"],
            question=task["instruction"],
            query=task["request"].get("query") or task["instruction"],
            status=task["status"],
            revision=task["revision"],
            sequence=task["sequence"],
            **receipt_payload(result, question=task["instruction"]),
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
            waiting_on=task.get("waiting_on", []),
        )

    async def scheduled_jobs(self, owner):
        """The user's Cron jobs from every conversation; Core invocations stay conversation-scoped."""
        reader = getattr(self, "list_scheduled_jobs", gateway_scheduled_jobs)
        try:
            jobs = list(await reader(owner) or [])
        except Exception as exc:  # noqa: BLE001 - report the gap; never present it as "no jobs"
            logger.warning("[VideoTasks] scheduled job lookup failed", exc_info=True)
            return {"available": False, "error": str(exc)[:300],
                    "message": "Scheduled jobs could not be read; do not claim there are none."}
        fields = ("id", "name", "enabled", "cron_expr", "timezone", "description", "targets", "expired")
        return {
            "available": True,
            "scope": "all conversations of this user (same list as the sidebar)",
            "total": len(jobs),
            "jobs": [
                {key: str(job.get(key) or "")[:300] if key == "description" else job.get(key) for key in fields}
                for job in jobs[:SCHEDULED_JOBS_LIMIT]
            ],
            "truncated": len(jobs) > SCHEDULED_JOBS_LIMIT,
            "message": SCHEDULED_JOBS_INSTRUCTIONS,
        }

    def snapshot(self, owner, scope):
        tasks, version = self.service.snapshot(owner, scope)
        queued = sorted(
            (t for t in tasks if t["status"] == "queued"), key=lambda t: t["position"]
        )
        positions = {t["id"]: i + 1 for i, t in enumerate(queued)}
        return {
            "search_session_id": scope,
            "conversation": {key: value for key, value in self._execution_targets.get((owner, scope), {}).items()
                             if key in {"mode", "work_mode", "project_id"}},
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
            "id", "job_id", "title", "status", "revision", "queue_version", "queue_position",
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
        # Voice answers approval cards with one of each card's own option labels.
        cards = approval_cards(interaction)
        approval = {"approval_cards": cards} if cards else {}
        result.update(approval)
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 12000:
            # Five jobs remain well below the provider's 256 KiB frame limit,
            # including the second JSON encoding of function_call_output.
            result = {key: job.get(key) for key in fields}
            result.update(
                question=str(job.get("question") or "")[:1000],
                details_omitted=True,
                artifact_status="not_in_response",
                **approval,
            )
        return result

    async def changed(self, task):
        state = task["status"]
        trace_task("task_state_published", task=task_state(task),
                   queue_version=self.snapshot(task["owner"], task["session"])["queue_version"],
                   subscriber_count=sum(key[:2] == (task["owner"], task["session"])
                                        for key in self.subscribers))
        if state in {"failed", "completed", "cancelled"}:
            self.log_event({"stage": "search_" + state, "job_id": task["id"]})
        event = state if state in {"completed", "failed", "cancelled"} else "progress"

        async def deliver(key, ws):
            try:
                if await self.authorized_scope(ws, task["session"]) != key[:2]:
                    raise ValueError("Task subscription identity changed")
            except ValueError:
                self.subscribers.pop(key, None)
                for card_key in tuple(self._approval_cards):
                    if card_key[:3] == key:
                        self._approval_cards.pop(card_key, None)
                return
            await asyncio.wait_for(self.channel.send_event(
                ws, "video.search." + event, self.public(task)
            ), EVENT_SEND_TIMEOUT)
            await asyncio.wait_for(self.channel.send_event(
                ws, "video.search.queue", self.snapshot(*key[:2])
            ), EVENT_SEND_TIMEOUT)
            await self._sync_approval_card(ws, task, key)
            trace_task("task_push_sent", task=task_state(task), connection_id=key[2])

        # One slow connection must not consume the budget of other subscribers.
        results = await asyncio.gather(*(
            deliver(key, ws) for key, ws in list(self.subscribers.items())
            if key[:2] == (task["owner"], task["session"])
        ), return_exceptions=True)
        if any(isinstance(result, Exception) for result in results):
            trace_task("task_push_failed", task=task_state(task),
                       errors=[str(result)[:1000] for result in results if isinstance(result, Exception)])
            raise RuntimeError("Task notification delivery was incomplete")

    def _on_session_event(self, event_name, session_id, payload):
        """Follow typed chat cards and turn ends from the web channel's own pushes."""
        if not session_id:
            return
        if event_name == "chat.ask_user_question":
            if payload.get("duplex_job_id") or payload.get("source") not in {"permission_interrupt", "confirm_interrupt"}:
                return
            self._chat_cards[session_id] = {
                key: payload.get(key) for key in ("request_id", "source", "questions", "approval_schema")
            }
            if len(self._chat_cards) > 128:
                self._chat_cards.pop(next(iter(self._chat_cards)))
        elif event_name == "chat.processing_status":
            if payload.get("is_processing"):
                # A resumed turn consumed the card, whoever answered it.
                self._chat_cards.pop(session_id, None)
                resumed = self._chat_resumed.get(session_id)
                if resumed is not None:
                    resumed.set()
            elif self._service is not None:
                # A turn ended: the card was answered or a new one is up; Core decides.
                self._service.interrupt_settled(session_id)

    async def chat_card(self, owner, scope):
        """The typed chat card Core still waits on in this conversation, for voice."""
        if not scope.startswith("task-duplex:"):
            return None
        session = scope.removeprefix("task-duplex:")
        card = self._chat_cards.get(session)
        if not card:
            return None
        from jiuwenswarm.common.schema.message import ReqMethod

        try:
            payload = await task_agent_query(
                self.client, ReqMethod.VOICE_TASK_PENDING_INTERRUPT, {}, session, owner,
            )
        except Exception:
            logger.warning("Chat card lookup failed for %s", session, exc_info=True)
            return None
        if not payload.get("pending"):
            self._chat_cards.pop(session, None)
            return None
        cards = approval_cards({**card, "state": "pending"}) or []
        # Same shape as a job's interaction; the answer is one of each question's option labels.
        questions = [
            {"question": q.get("question", ""), "options": [{"label": label} for label in approval_option_labels(q)]}
            for q in card.get("questions") or [] if isinstance(q, dict)
        ]
        return {"job_id": "", "interaction_id": card["request_id"], "approval_cards": cards,
                "interaction": {"id": card["request_id"], "source": card["source"], "questions": questions}}

    async def answer_chat_card(self, ws, owner, scope, interaction_id, answers):
        """Answer a typed chat card through the same web request a screen click sends."""
        card = await self.chat_card(owner, scope)
        rejected = dict(job_id="", interaction_id=interaction_id, state="rejected", applied=False)
        if card is None or card["interaction_id"] != interaction_id:
            return {**rejected, "error": "This card is no longer pending; query again"}
        session = scope.removeprefix("task-duplex:")
        payload = self._chat_cards.get(session)
        if payload is None:
            # Answered on screen while the card was being looked up.
            return {**rejected, "error": "This card is no longer pending; query again"}
        try:
            bound = voice_approval_answers({**payload, "state": "pending"}, answers)
        except ValueError as exc:
            return {**rejected, "error": str(exc), "approval_cards": card["approval_cards"]}
        submit = getattr(self.channel, "dispatch_client_request", None)
        if ws is None or not callable(submit):
            raise RuntimeError("This channel cannot submit a chat answer")
        from jiuwenswarm.runtime.interaction import InteractionAnswerInput

        target = self._execution_targets.get((owner, scope)) or {}
        mode = target.get("mode") or "agent"
        request = InteractionAnswerInput(
            request_id="voice-answer-" + str(time.time_ns()),
            channel_id="web",
            session_id=session,
            interaction_id=interaction_id,
            answers=tuple({"custom_input": "", **answer} for answer in bound),
            source=payload["source"],
            approval_schema=payload.get("approval_schema") or "",
            mode=mode,
            work_mode="" if mode == "team.design" else target.get("work_mode", "work"),
            project_dir=target.get("project_dir", ""),
        ).to_agent_request()
        resumed = self._chat_resumed[session] = asyncio.Event()
        try:
            await submit(ws, "chat.send", request.params)
            # The web request returns no result; Core resuming the turn is the confirmation.
            await asyncio.wait_for(resumed.wait(), CHAT_ANSWER_CONFIRM_TIMEOUT)
        except asyncio.TimeoutError:
            pass
        finally:
            if self._chat_resumed.get(session) is resumed:
                self._chat_resumed.pop(session)
        chosen = [a["selected_options"][0] for a in bound]
        if not resumed.is_set():
            return dict(job_id="", interaction_id=interaction_id, state="submitted", applied=False, answers=chosen,
                        message="Submitted, but Core has not resumed the task yet; query again before saying it was approved")
        self._chat_cards.pop(session, None)
        closed = {"session_id": session, "request_id": interaction_id}
        await asyncio.gather(*(
            asyncio.wait_for(self.channel.send_event(other, "video.search.confirmation_closed", closed),
                             EVENT_SEND_TIMEOUT)
            for key, other in list(self.subscribers.items()) if key[:2] == (owner, scope)
        ), return_exceptions=True)
        return dict(job_id="", interaction_id=interaction_id, state="accepted", applied=True, answers=chosen)

    async def _sync_approval_card(self, ws, task, key):
        interaction = task.get("interaction") or {}
        card_key = (*key, task["id"])
        pending = (
            task["session"].startswith("task-duplex:")
            and task["status"] == "waiting_user"
            and interaction.get("state") == "pending"
            and interaction.get("source") in {"permission_interrupt", "confirm_interrupt", "ask_user", "ask_user_interrupt", ""}
        )
        if pending:
            if self._approval_cards.get(card_key) == interaction["id"]:
                return
            payload = {
                "session_id": task["session"].removeprefix("task-duplex:"),
                "duplex_job_id": task["id"],
                "request_id": interaction["request_id"],
                "source": interaction["source"],
                "questions": interaction["questions"],
            }
            if interaction.get("approval_schema"):
                payload["approval_schema"] = interaction["approval_schema"]
            await asyncio.wait_for(
                self.channel.send_event(ws, "chat.ask_user_question", payload),
                EVENT_SEND_TIMEOUT,
            )
            self._approval_cards[card_key] = interaction["id"]
        elif card_key in self._approval_cards:
            await asyncio.wait_for(self.channel.send_event(ws, "video.search.confirmation_closed", {
                "session_id": task["session"].removeprefix("task-duplex:"),
                "duplex_job_id": task["id"],
                "request_id": interaction.get("request_id", ""),
            }), EVENT_SEND_TIMEOUT)
            self._approval_cards.pop(card_key, None)

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
        title="",
    ):
        owner, scope = await self.scope(ws, {"search_session_id": search_session_id})
        from .mode_routing import resolve_execution_target

        target = await resolve_execution_target(self.client, owner, scope)
        credential_ref = _login_credential_ref(ws, target)
        request = dict(
            title=title,
            query=query,
            visual_context=visual_context,
            frame_data_url=frame_data_url,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            turn_id=turn_id,
            execution_target=target,
        )
        request.update(scheduling or {})
        request["execution_target"] = target
        if credential_ref:
            request["credential_ref"] = credential_ref
        task = self.service.submit(
            owner, scope, command_id or tool_call_id, question or query, request
        )
        return self.public(task)

    async def _respond(self, ws, req_id, action, *, trace=None):
        started = time.monotonic()
        if trace is not None:
            trace_task("task_rpc_received", rpc_request_id=req_id, **trace)
        try:
            payload = await action()
        except Exception as exc:
            if trace is not None:
                trace_task("task_rpc_rejected", rpc_request_id=req_id, **trace,
                           error_type=type(exc).__name__, error=str(exc)[:2000],
                           elapsed_ms=round((time.monotonic() - started) * 1000))
            await self.channel.send_response(
                ws, req_id, ok=False, error=str(exc), code="TASK_REQUEST_REJECTED"
            )
            return
        if trace is not None:
            trace_task("task_rpc_completed", rpc_request_id=req_id, **trace,
                       receipt=receipt_state(payload), elapsed_ms=round((time.monotonic() - started) * 1000))
        await self.channel.send_response(ws, req_id, ok=True, payload=payload)

    async def handle_qwen_tool(self, ws, req_id, params, session_id):
        async def run():
            owner, scope = await self.scope(ws, params)
            if not self.qwen_active():
                raise ValueError("Qwen Omni Realtime is not the active provider")
            call = parse_qwen_omni_tool_call(params)
            if call.name == "jiuwen_queue_manage":
                instruction = str(params.get("question") or call.task).strip()
                result = await self.manage_queue_call(owner, scope, call, instruction)
                return {"tool_result": result, "call_id": call.call_id}
            if call.name in {"jiuwen_delegate", "jiuwen_research"}:
                question = str(params.get("question") or "").strip()
                if question and not delegate_matches_input(question, call.task):
                    return {
                        "tool_result": {
                            "state": "rejected", "applied": False, "retryable": True,
                            "code": "TASK_INPUT_MISMATCH",
                            "message": (
                                "No task was created. The proposed task does not match the latest user utterance. "
                                "Read that utterance again and call jiuwen_delegate with the user's actual goal. "
                                "If the utterance is only a short answer, use the preceding conversation for context."
                            ),
                        },
                        "call_id": call.call_id,
                    }
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
                    title=(call.arguments.get("title", "")
                           if delegate_matches_input(question or call.task, call.arguments.get("title", ""))
                           else ""),
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
                "tool_result": await self.operate(
                    owner, scope, call, ws=ws, user_instruction=params.get("question") or "",
                ),
                "call_id": call.call_id,
            }

        await self._respond(ws, req_id, run, trace={
            "source": "qwen", "call_id": params.get("call_id"),
            "name": params.get("name"), "search_session_id": params.get("search_session_id"),
            "arguments": str(params.get("arguments") or "")[:6000],
            "question": str(params.get("question") or "")[:1000],
        })

    async def manage_queue_call(self, owner, scope, call, instruction, *, allow_answers=True, reply_language="match"):
        """Share deduplication and lifecycle for provider queue management calls."""
        from .cron_control import CronControl
        from .queue_manager import manage_queue

        key = (owner, scope, call.call_id)
        signature = (call.task, instruction, allow_answers, reply_language)
        existing = self._queue_requests.get(key)
        if existing is not None and existing[0] != signature:
            raise ValueError("Queue command ID reused with different instructions")
        if existing is None:
            if len(self._queue_requests) >= 128:
                expired = next((k for k, v in self._queue_requests.items() if v[1].done()), None)
                if expired is None:
                    raise ValueError("Too many queue management requests")
                del self._queue_requests[expired]
            task = asyncio.create_task(manage_queue(
                self, owner, scope, call, instruction,
                allow_answers=allow_answers, reply_language=reply_language,
                cron=CronControl(owner),
            ))
            self._queue_requests[key] = (signature, task)
        return await asyncio.shield(self._queue_requests[key][1])

    async def operate(self, owner, scope, call, *, ws=None, approvals_only=False, user_instruction=""):
        args = call.arguments
        if args.get("job_id"):
            try:
                self.service.get(owner, scope, args["job_id"])
            except TaskNotFound:
                logger.info(
                    "Voice task target not found: operation=%s job_id=%s scope=%s",
                    call.name, args["job_id"], scope,
                )
                return {
                    "state": "rejected", "applied": False, "code": "TASK_NOT_FOUND",
                    "attempted_job_id": args["job_id"], "retryable": True,
                    "message": (
                        "No operation was performed. This ID does not identify a Core invocation in the current conversation. "
                        "Do not infer that the resulting resource was deleted or that the user request is impossible. "
                        "Query jiuwen_task_query without job_id, locate the intended invocation from its requirement "
                        "and result, and copy its exact job_id and revision before trying a corrected operation. "
                        "IDs inside result text identify external resources, not Core invocations; send such IDs "
                        "to Core in the follow-up instruction, never as job_id. If the target remains ambiguous, ask the user. "
                        "Never repeat this rejected ID, operate on another conversation, or recreate the resource."
                    ),
                }
        if call.name == "jiuwen_task_query":
            snapshot = self.snapshot(owner, scope)
            jobs = snapshot["jobs"]
            if args.get("job_id"):
                self.service.get(owner, scope, args["job_id"])  # Keep ownership validation.
                jobs = [t for t in jobs if t["id"] == args["job_id"]]
            else:
                query = args.get("query", "").casefold()
                jobs = [t for t in jobs if query in t["question"].casefold() or query in t.get("title", "").casefold()]
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
                "operation": "query", "applied": False,
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
            if not args.get("job_id"):
                result["scheduled_jobs"] = await self.scheduled_jobs(owner)
                card = await self.chat_card(owner, scope)
                if card is not None:
                    result["chat_approval"] = card
        elif call.name == "jiuwen_task_cancel":
            result = await self.service.cancel(
                owner, scope, args["job_id"], call.call_id,
                user_instruction=user_instruction if user_instruction != call.name else "",
            )
        elif call.name == "jiuwen_task_reorder":
            if args["action"] == "before" and not args.get("before_job_id"):
                raise ValueError("before requires before_job_id")
            try:
                queue_version = args.get("queue_version")
                if queue_version is None:
                    queue_version = self.snapshot(owner, scope)["queue_version"]
                receipt = self.service.reorder(
                    owner, scope, args["job_id"], queue_version,
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
            if not args.get("job_id"):
                # A typed chat's card has no job; it is named by its interaction_id.
                return await self.answer_chat_card(
                    ws, owner, scope, args["interaction_id"], args["answers"]
                )
            interaction = self.service.get(owner, scope, args["job_id"]).get("interaction")
            if approvals_only and approval_cards(interaction) is None:
                raise ValueError("JoyAI voice form answers are disabled; complete the on-screen form")
            try:
                result = await self.service.answer(
                    owner,
                    scope,
                    args["job_id"],
                    call.call_id,
                    args["interaction_id"],
                    answers=args["answers"],
                )
            except ValueError as exc:
                # Explicit receipt so the model cannot mistake a refusal for consent.
                current = self.service.get(owner, scope, args["job_id"])
                return dict(
                    task_id=args["job_id"], state="rejected", applied=False, error=str(exc),
                    current_task=self.voice_job(self.public(current)),
                    message=(
                        "The answer was not applied. Report the current task state from this receipt; "
                        "do not claim approval, continued execution or completion without evidence. "
                        "Query the current card before another answer. Do not recreate the task or "
                        "transfer consent to a different card automatically."
                    ),
                )
        else:
            try:
                result = self.service.modify(
                    owner, scope, args["job_id"], call.call_id,
                    TaskModification(args["revision"], args["instruction"], args.get("title", "")),
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
            for task in tasks:
                await self._sync_approval_card(ws, task, (owner, scope, id(ws)))
            return {
                "jobs": [self.public(t) for t in tasks],
                "next_offset": cursor,
                "replay": True,
            }

        await self._respond(ws, req_id, run)

    async def handle_answer(self, ws, req_id, params, session_id):
        async def run():
            if not isinstance(params, dict):
                raise ValueError("Invalid confirmation")
            visible_session = params.get("session_id")
            if not isinstance(visible_session, str) or not visible_session:
                raise ValueError("Confirmation session is required")
            owner, scope = await self.scope(ws, {
                "search_session_id": "task-duplex:" + visible_session,
            })
            task = self.service.get(owner, scope, params.get("job_id"))
            interaction = task.get("interaction") or {}
            if interaction.get("request_id") != params.get("request_id"):
                raise ValueError("Confirmation is no longer pending")
            return await self.service.answer(
                owner, scope, task["id"], str(req_id), interaction.get("id"),
                answers=params.get("answers"), native_ui=True,
            )

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
                    TaskModification(params.get("revision"), params.get("instruction"), params.get("title", "")),
                )
            else:
                raise ValueError(
                    "Unsupported control; stop the active task before reordering"
                )
            snapshot = self.snapshot(owner, scope)
            await self.channel.send_event(ws, "video.search.queue", snapshot)
            return snapshot

        await self._respond(ws, req_id, run, trace={
            "source": "task_controls", "job_id": params.get("job_id"),
            "action": params.get("action"), "before_job_id": params.get("before_job_id"),
            "queue_version": params.get("queue_version"),
            "search_session_id": params.get("search_session_id"),
        })

    async def close(self):
        pending = [entry[1] for entry in self._queue_requests.values()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._queue_requests.clear()
        if self._service is not None:
            await self._service.close()
