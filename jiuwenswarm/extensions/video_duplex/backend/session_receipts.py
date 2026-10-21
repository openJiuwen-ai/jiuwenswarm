"""Session-scoped Core receipts; model text carries no routing identities."""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ReceiptSubscription:
    owner: str
    token: str
    expires: float
    seen: set = field(default_factory=set)
    managed: set = field(default_factory=set)
    managed_outputs: set = field(default_factory=set)
    reply_requests: set = field(default_factory=set)


_subscriptions = {}
LEASE_SECONDS = 180


def subscription(session_id):
    current = _subscriptions.get(session_id)
    if current and current.expires > time.monotonic():
        return current
    _subscriptions.pop(session_id, None)
    return None


async def configure(request):
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    params = request.params or {}
    token, action = params.get("token"), params.get("action")
    if not isinstance(token, str) or not token or len(token) > 128 or action not in {"start", "renew", "stop"}:
        raise ValueError("Invalid Full-duplex subscription")
    metadata = await asyncio.to_thread(get_session_metadata, request.session_id,
                                       cache_bust=True, enable_writeback=False)
    owner = request.user_id or ""
    if not metadata or str(metadata.get("user_id") or "").strip() != owner:
        raise ValueError("Conversation is unavailable to this user")
    current = subscription(request.session_id)
    if action == "start":
        for sid, item in list(_subscriptions.items()):
            if item.owner == owner:
                _subscriptions.pop(sid, None)
        _subscriptions[request.session_id] = ReceiptSubscription(owner, token, time.monotonic() + LEASE_SECONDS)
    elif current and current.owner == owner and current.token == token:
        if action == "stop":
            _subscriptions.pop(request.session_id, None)
        else:
            current.expires = time.monotonic() + LEASE_SECONDS
    elif action == "renew":
        raise ValueError("Full-duplex subscription was replaced or expired")
    return {"active": action != "stop"}


def prepare_request(request):
    """Use the session model policy, with a request fallback for standalone tools."""
    current = subscription(request.session_id)
    if current is not None and request.user_id is not None and current.owner != request.user_id:
        return
    params = request.params or {}
    if current and params.get("managed_task_binding"):
        current.managed.add(request.request_id)
    if params.get("attach_only"):
        return
    query = params.get("query") or params.get("content")
    if not isinstance(query, str) or not query:
        return
    if current:
        params.setdefault("duplex_original_question", query)
    elif request.channel_id == "video_tool" and "Voice receipt protocol" not in query:
        from .core_receipts import core_agent_brief_protocol

        params["query"] = params["content"] = query + core_agent_brief_protocol()


def apply_summary_policy(builder, session_id, ctx=None):
    """Refresh the root model's policy even on native interaction resumes."""
    name = "video_duplex.voice_receipt"
    builder.remove_section(name)
    if not session_id:
        session = getattr(ctx, "session", None)
        getter = getattr(session, "get_session_id", None)
        session_id = getter() if callable(getter) else None
    if subscription(session_id) is None:
        return
    from openjiuwen.harness.prompts import PromptSection
    from .core_receipts import core_agent_brief_protocol

    prompt = core_agent_brief_protocol()
    builder.add_section(PromptSection(name=name, content={"cn": prompt, "en": prompt}, priority=65))


async def publish_result(request, event):
    """Observe root final answers without turning progress into spoken results."""
    current = subscription(event.session_id or request.session_id)
    payload = event.payload or {}
    from jiuwenswarm.runtime.request import CHAT_TURN_METHODS

    final_answer = event.event_type == "chat.final" or (
        not event.event_type and event.is_complete and request.req_method in CHAT_TURN_METHODS
    )
    if current is None or (request.user_id is not None and current.owner != request.user_id) or not final_answer:
        return
    role = str(payload.get("role") or "").lower()
    if role not in {"", "assistant", "leader"} or payload.get("is_subagent"):
        return
    content = str(payload.get("content") or "").strip()
    if not content:
        return
    from .core_receipts import receipt_payload

    presented = receipt_payload({"answer": content})
    digest = hashlib.sha256(presented["display_result"].encode()).hexdigest()
    managed = bool((request.params or {}).get("managed_task_binding"))
    if managed:
        current.managed_outputs.add(digest)
        return  # The existing TaskService delivers this receipt exactly once.
    observer = (request.params or {}).get("attach_only") or request.request_id in current.reply_requests
    if current.managed or (observer and digest in current.managed_outputs):
        return
    identity = (event.request_id, payload.get("execution_id"), digest)
    if identity in current.seen:
        return
    from jiuwenswarm.runtime.host_services import send_runtime_push

    try:
        sent = await send_runtime_push({
            "request_id": event.request_id, "channel_id": "web", "session_id": event.session_id,
            "payload": {"event_type": "video.core.result", "session_id": event.session_id,
                        "search_session_id": "task-duplex:" + str(event.session_id),
                        "job_id": "core-reply:" + event.request_id + ":" + digest[:16],
                        "subscription_token": current.token,
                        "question": ((request.params or {}).get("video_question")
                                     or (request.params or {}).get("duplex_original_question") or "Core Agent reply"),
                        **presented},
        })
        if sent:
            current.seen.add(identity)
            current.reply_requests.add(request.request_id)
    except Exception:
        logger.warning("Full-duplex Core receipt delivery failed", exc_info=True)


def finish_request(request):
    current = subscription(request.session_id)
    if current:
        current.managed.discard(request.request_id)
