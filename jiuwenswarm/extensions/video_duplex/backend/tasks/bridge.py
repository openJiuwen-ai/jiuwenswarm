"""Gateway-owned checkpoint commands over the existing E2A push/ACK channel."""

import asyncio
import logging
import uuid
import weakref

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.host_services import send_runtime_push

_endpoints = weakref.WeakValueDictionary()
_pending = {}
EVENT = "voice.task.command"
ACK_TIMEOUT = 15
logger = logging.getLogger(__name__)


class GatewayTaskEndpoint:
    """The only checkpoint writer; all validation and mutation is atomic here."""

    def __init__(self, store):
        self.store = store
        self.id = uuid.uuid4().hex
        _endpoints[self.id] = self

    def binding(self, task):
        return {"endpoint": self.id, "task_id": task["id"]}

    def close(self):
        _endpoints.pop(self.id, None)

    def execute(self, command):
        action = command.get("action")
        with self.store.transaction() as db:
            task = self.store.get(db, command.get("task_id"))
            if (command.get("owner"), command.get("session_id"), command.get("request_id")) != (
                task["owner"], task["core_session_id"], task["request_id"]
            ):
                raise ValueError("TASK_EXECUTION_BINDING_STALE")
            if action == "status":
                return {"status": task["status"]}
            if action not in {"bind", "claim", "context_written", "observed", "close", "settle"}:
                raise ValueError("Unknown task checkpoint command")
            replay, fingerprint = self.store.replay(
                db, task["owner"], task["session"], command.get("command_id"), command
            )
            if replay is not None:
                return replay
            if action in {"bind", "claim", "context_written", "observed"}:
                if task["status"] not in {"running", "waiting_user"}:
                    raise RuntimeError("TASK_EXECUTION_NOT_RUNNING")
            result = {}
            if action == "bind":
                if task.get("execution_bound") or not task.get("checkpoint_open"):
                    raise RuntimeError("Task execution is not available for a new invocation")
                task["execution_bound"] = True
            elif not task.get("execution_bound"):
                raise RuntimeError("Task execution is not bound")
            elif action == "claim":
                if not task.get("checkpoint_open"):
                    raise RuntimeError("TASK_EXECUTION_BINDING_CLOSED")
                changes = [c for c in task["changes"] if c["state"] == "pending"]
                for change in changes:
                    change["state"] = "claimed"
                result = {"changes": changes}
            elif action in {"context_written", "observed"}:
                if not task.get("checkpoint_open"):
                    raise RuntimeError("TASK_EXECUTION_BINDING_CLOSED")
                before, after = ("claimed", "context_written") if action == "context_written" else (
                    "context_written", "model_input_observed"
                )
                for change in task["changes"]:
                    if change["id"] in command.get("change_ids", []) and change["state"] == before:
                        change["state"] = after
            elif action == "close":
                task["checkpoint_open"] = False
            elif action == "settle":
                if task.get("checkpoint_open"):
                    raise RuntimeError("Task invocation is still open")
                task["execution_settled"] = True
                task["execution_cancelled"] = bool(command.get("cancelled"))
            task["sequence"] += 1
            self.store.put(db, task)
            self.store.command(db, task, command["command_id"], fingerprint, result)
            return result


class RemoteTaskEndpoint:
    """AgentServer-side handle; contains no task database or cached task state."""

    def __init__(self, request):
        binding = (request.params or {}).get("managed_task_binding") or {}
        if not binding.get("endpoint") or not binding.get("task_id"):
            raise RuntimeError("Managed task execution binding is unavailable")
        self.identity = {
            **binding, "owner": request.user_id or "", "session_id": request.session_id,
            "request_id": request.request_id,
        }

    async def call(self, action, **data):
        command_id = "task-checkpoint-" + uuid.uuid4().hex
        command = {**self.identity, **data, "command_id": command_id, "action": action}
        future = asyncio.get_running_loop().create_future()
        _pending[command_id] = (self.identity, future)
        try:
            delivered = await send_runtime_push({
                "request_id": self.identity["request_id"], "channel_id": "video_tool",
                "session_id": self.identity["session_id"],
                "payload": {"event_type": EVENT, **command},
            })
            if delivered is False:
                raise RuntimeError("Task checkpoint could not reach Gateway")
            result = await asyncio.wait_for(asyncio.shield(future), ACK_TIMEOUT)
            if result.get("error"):
                raise RuntimeError(result["error"])
            return result
        finally:
            _pending.pop(command_id, None)
            if not future.done():
                future.cancel()


def resolve_checkpoint_ack(request):
    params = request.params or {}
    pending = _pending.get(params.get("command_id"))
    if pending is None:
        return False
    identity, future = pending
    if (request.user_id or "", request.session_id, params.get("execution_request_id")) != (
        identity["owner"], identity["session_id"], identity["request_id"]
    ):
        raise ValueError("Task checkpoint acknowledgement identity mismatch")
    if not future.done():
        future.set_result(params.get("result") or {})
    return True


async def handle_checkpoint_push(client, chunk, session_id):
    """Consume internal task frames before normal user-message delivery."""
    command = chunk.payload
    if not isinstance(command, dict) or command.get("event_type") != EVENT:
        return False
    endpoint = _endpoints.get(command.get("endpoint"))
    try:
        if endpoint is None:
            raise ValueError("Task Gateway endpoint is unavailable")
        if chunk.channel_id != "video_tool" or (
            chunk.request_id, session_id
        ) != (command.get("request_id"), command.get("session_id")):
            raise ValueError("Task checkpoint transport identity mismatch")
        result = endpoint.execute(command)
    except (ValueError, RuntimeError) as exc:
        result = {"error": str(exc)}
    acknowledgement = e2a_from_agent_fields(
        request_id="ack-" + str(command.get("command_id") or ""),
        channel_id="video_tool", session_id=session_id,
        req_method=ReqMethod.VOICE_TASK_CHECKPOINT_ACK,
        user_id=command.get("owner") or None, is_stream=False,
        params={"command_id": command.get("command_id"), "result": result,
                "execution_request_id": chunk.request_id},
    )
    try:
        await asyncio.wait_for(client.send_request(acknowledgement), ACK_TIMEOUT)
    except Exception:
        # The AgentServer waiter will time out. Do not repeat the operation.
        logger.warning("Task checkpoint acknowledgement delivery failed", exc_info=True)
    return True
