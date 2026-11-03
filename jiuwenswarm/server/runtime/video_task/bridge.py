"""AgentServer-side handle for the managed-task checkpoint protocol.

The Gateway writer lives in ``extensions/video_duplex/backend/tasks/bridge.py``;
both halves talk only over the existing E2A push/ACK channel.
"""

import asyncio
import uuid

from jiuwenswarm.runtime.host_services import send_runtime_push

_pending = {}
ACK_TIMEOUT = 15
# Wire event shared with the Gateway writer (see the gateway half's ``EVENT``).
EVENT = "voice.task.command"


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
