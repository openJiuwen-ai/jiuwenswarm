"""Bind a voice task to an existing Design leader round, not to canvas renders."""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import asyncio
import json

from .bridge import RemoteTaskEndpoint


@dataclass
class DesignVoiceRound:
    request_id: str
    endpoint: RemoteTaskEndpoint
    settled: bool = False
    cancelled: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def settle(self, *, cancelled=False):
        async with self.lock:
            if self.cancelled or (self.settled and not cancelled):
                return
            await self.endpoint.call("close")
            await self.endpoint.call("settle", cancelled=cancelled)
            self.settled, self.cancelled = True, cancelled


_rounds = {}


async def add_voice_context(request, inputs):
    """Supply recent speech to a warm Team without rewriting its native context."""
    if request.params.get("answers"):
        return
    from jiuwenswarm.server.runtime.agent_adapter.user_turn import TEAM_USER_TURN_KEY, UserTurn
    from ..conversation_context import load_voice_history
    from ..realtime_context import RESTORE_INSTRUCTIONS

    turn = inputs.get(TEAM_USER_TURN_KEY)
    if not isinstance(turn, UserTurn) or not isinstance(turn.text, str):
        return
    records = await load_voice_history(request.session_id)
    recent = [{"role": item["role"], "text": str(item.get("content") or "")[:1000]}
              for item in records][-6:]
    if recent:
        background = RESTORE_INSTRUCTIONS + "\n" + json.dumps({"recent_voice_turns": recent}, ensure_ascii=False)
        inputs[TEAM_USER_TURN_KEY] = turn.with_text(background + "\n\nCurrent request:\n" + turn.text)


def control_target(request):
    """Reject stale voice cancellation before the standard Team cancel executes."""
    params = request.params or {}
    if params.get("mode") != "team.design":
        return None
    current = _rounds.get((request.user_id or "", request.session_id))
    expected = params.get("managed_task_request")
    if expected and (current is None or current.request_id != expected):
        raise ValueError("TASK_EXECUTION_BINDING_STALE")
    return current


async def answer_design_task(request, build_inputs):
    """Deliver an observed information answer to the existing Team input channel."""
    binding = control_target(request)
    if binding is None or binding.settled:
        raise ValueError("Design answer channel is no longer active")
    from jiuwenswarm.agents.harness.team import get_team_manager
    from jiuwenswarm.common.schema.agent import AgentResponse

    inputs, _, _ = build_inputs(request)
    accepted, error = await get_team_manager(request.channel_id).interact(request.session_id, inputs["query"])
    return AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                         ok=accepted, payload={"success": accepted, **({"error": error} if error else {})})


@asynccontextmanager
async def bind_design_voice_task(request, inputs=None):
    params = request.params or {}
    if params.get("mode") != "team.design":
        yield
        return
    key = (request.user_id or "", request.session_id)
    current = _rounds.get(key)
    if current and not current.settled:
        raise RuntimeError("A voice Design task still owns this conversation; answer or cancel it first")
    managed = request.channel_id == "video_tool" and params.get("managed_task_binding")
    if not managed:
        _rounds.pop(key, None)
        if inputs is not None:
            await add_voice_context(request, inputs)
        yield
        return
    from jiuwenswarm.agents.harness.team import get_team_manager

    if not params.get("answers") and get_team_manager(request.channel_id).is_round_active(request.session_id):
        raise RuntimeError("Design assistant is busy; wait for its current turn")
    binding = DesignVoiceRound(request.request_id, RemoteTaskEndpoint(request))
    _rounds[key] = binding
    try:
        await binding.endpoint.call("bind")
    except BaseException:
        _rounds.pop(key, None)
        raise
    if inputs is not None:
        try:
            await add_voice_context(request, inputs)
        except BaseException:
            await binding.settle()
            _rounds.pop(key, None)
            raise
    try:
        yield
    except BaseException:
        # A detached output is not proof that Team execution has stopped.
        await binding.endpoint.call("close")
        raise
    else:
        await binding.endpoint.call("close")
        status = await binding.endpoint.call("status")
        if status["status"] != "cancelling":
            await binding.settle()
            if status["status"] != "waiting_user" and _rounds.get(key) is binding:
                _rounds.pop(key, None)
