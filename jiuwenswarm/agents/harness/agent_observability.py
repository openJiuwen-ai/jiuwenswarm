# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Config-gated lifecycle for single-agent / coding-agent observability.

The non-team counterpart of ``sync_team_observability`` /
``shutdown_team_observability`` in
``jiuwenswarm.agents.harness.team.team_manager``, and symmetric with it: this
module only reads this platform's config and toggles the runtime, while the
tracing mechanics — run root span, the session-keyed fallback that keeps it
reachable from supervisor tasks, agent-tier rail wiring and the sub-agent
dispatch hook — live in the SDK under ``openjiuwen.harness.observability``.

It is kept in a **separate file with its own state and config section** on
purpose, so the existing team scenario is not affected.

Shared-provider caveat (important):
    OpenTelemetry allows exactly ONE global ``TracerProvider`` per process, and
    initialization is a no-op if one already exists. In a process where BOTH
    team and agent observability are enabled, whichever runs first wins; the
    other silently reuses it (its exporter/endpoint/service_name are ignored).
    Provider demands are coordinated inside the SDK, so agent shutdown never
    tears down a provider the team subsystem depends on.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from openjiuwen.core.common.logging import server_logger
from openjiuwen.harness.observability import (
    acquire_observability,
    release_observability,
)

from jiuwenswarm.agents.harness.observability_runtime import build_observability_config

from jiuwenswarm.common.config import (
    get_config,
    get_skill_evolution_enabled,
)
from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.observability.config import load_trajectory_store_settings
from jiuwenswarm.observability.runtime import (
    shutdown_trajectory_runtime,
    sync_trajectory_runtime,
)

logger = logging.getLogger(__name__)

# Tracks whether observability is currently active so we can detect config
# toggles (enabled -> disabled or vice-versa) and init / shutdown accordingly
# on each single-agent request.
_agent_observability_active: bool = False

# Sticky flag: once any single-agent request has force-enabled observability
# (e.g. a ``/debug`` run with ``debug_trace.<mode>.otel_enabled``), we never
# auto-teardown the provider for the rest of the process. OTel allows only one
# global TracerProvider and re-init after shutdown is fragile, so a /debug
# toggle must not churn init/shutdown across alternating requests. The normal
# config-gated path (agent_observability.enabled hot-reload) is unaffected
# unless force was ever used.
_force_ever_enabled: bool = False

# Session-id → open root span fallback table (see ``open_agent_run_span``). The
# agent and its sub-agents run in a different asyncio task than the request
# handler, so ContextVars do not reach them; this table lets a sub-agent's
# ``create_subagent``/LLM callback resolve the parent run's span by session id.
# A session's entry is dropped when its run closes, and sessions overlap, so
# closing never clears another still-running session's entry.
_ROOT_SPANS: dict[str, Any] = {}


def sync_agent_observability(*, force: bool = False) -> None:
    """Synchronize single-agent observability state with current config.

    Called before each ``Runner.run_agent_streaming`` / ``Runner.run_agent`` so
    that hot-reloading the ``agent_observability.enabled`` flag takes effect
    immediately:

    * disabled -> enabled : acquire the provider (or reuse if already up)
    * enabled -> disabled : ``shutdown_agent_observability()``
    * unchanged           : no-op

    Evolution also requests the provider when the explicit switch is disabled.

    ``force=True`` (set by a ``/debug`` run when ``debug_trace.<mode>.otel_enabled``
    is true) treats ``want_enabled`` as true regardless of config, so a debug
    request can pull up OTel even when ``agent_observability.enabled`` is false.
    Once force is ever used, the provider stays up for the process (sticky — see
    ``_force_ever_enabled``) to avoid init/shutdown churn across alternating
    requests; the normal config hot-reload teardown is unchanged when evolution
    is disabled.
    """
    global _agent_observability_active, _force_ever_enabled

    config = get_config()
    cfg = config.get("agent_observability", {}) or {}
    trajectory_settings = load_trajectory_store_settings(config)
    evolution_requested = get_skill_evolution_enabled(config)
    want_enabled = (
        bool(cfg.get("enabled", False))
        or trajectory_settings.enabled
        or evolution_requested
        or force
        or _force_ever_enabled
    )
    if force:
        _force_ever_enabled = True

    if not want_enabled:
        try:
            sync_trajectory_runtime(trajectory_settings, demand="agent")
        except Exception as exc:
            logger.warning("[AgentObservability] trajectory runtime stop failed: %s", exc)
        if _agent_observability_active:
            shutdown_agent_observability()
        return

    try:
        traces_dir = str(cfg.get("traces_dir") or get_user_workspace_dir() / ".trace")
        obs_cfg = build_observability_config(
            cfg,
            service_name="jiuwenswarm-agent",
            default_backend="otlp",
            traces_dir=traces_dir,
        )
        provider_existed = acquire_observability(obs_cfg)
        was_active = _agent_observability_active
        _agent_observability_active = True
        try:
            sync_trajectory_runtime(trajectory_settings, demand="agent")
        except Exception as exc:
            # The trajectory read store is an optional fan-out. Existing file,
            # OTLP and Langfuse exporters must keep the Agent path available.
            logger.warning("[AgentObservability] trajectory runtime init failed: %s", exc)
        if not was_active:
            if provider_existed:
                logger.info(
                    "[AgentObservability] reusing existing observability provider "
                    "(owned by another subsystem)"
                )
            elif cfg.get("exporter", "otlp_grpc") == "file":
                logger.info(
                    "[AgentObservability] enabled: exporter=%s traces_dir=%s",
                    cfg.get("exporter", "otlp_grpc"),
                    traces_dir,
                )
            else:
                logger.info(
                    "[AgentObservability] enabled: exporter=%s endpoint=%s",
                    cfg.get("exporter", "otlp_grpc"),
                    cfg.get("endpoint", "http://localhost:4317"),
                )
    except Exception as exc:
        _agent_observability_active = False
        if evolution_requested:
            raise RuntimeError(
                "Agent evolution observability initialization failed"
            ) from exc
        logger.warning("[AgentObservability] init failed: %s", exc)


def shutdown_agent_observability() -> None:
    """Shutdown single-agent observability (on disable or process exit)."""
    global _agent_observability_active
    try:
        if not shutdown_trajectory_runtime(demand="agent"):
            logger.warning("[AgentObservability] trajectory runtime did not drain cleanly")
    except Exception as exc:
        logger.warning("[AgentObservability] trajectory runtime shutdown failed: %s", exc)
    if not _agent_observability_active:
        return
    try:
        release_observability()
        _agent_observability_active = False
        logger.info("[AgentObservability] disabled")
    except Exception as exc:
        logger.warning("[AgentObservability] shutdown failed: %s", exc)


# ── Per-run root span ───────────────────────────────────────────
# openjiuwen's OtelCallbackHandler skips LLM/tool span creation when no parent
# span exists (``get_team_span`` / ``get_current_agent_span`` both None — see
# callback_handler._get_parent_context_for_llm_tool). Single-agent runs set
# neither, so without a root span zero spans are produced even after a clean
# ``init_observability``. These helpers open a root span and register it via
# ``set_team_span`` — the exact mechanism team mode uses internally
# (team_runner._maybe_attach_observability → get_or_create_team_span). LLM/tool
# spans then nest under it and are exported.
#
# Usage (must be paired, in the same coroutine so the ContextVar propagates
# into the runner's LLM calls):
#     handle = open_agent_run_span(session_id=sid)
#     try:
#         ... Runner.run_agent_streaming / Runner.run_agent ...
#     finally:
#         close_agent_run_span(handle)
# Synthetic team name for the non-team run paths. Registered with
# ``set_team_span`` for the root span, and stamped on the agents themselves by
# :func:`mark_single_agent_team` — the observability rail keys its agent-tier
# spans off ``agent.team_name``.
SINGLE_AGENT_TEAM_NAME = "single-agent"


def mark_single_agent_team(agent: Any) -> None:
    """Stamp the synthetic team marker the observability rail keys off.

    ``ObservabilityRail.before_invoke`` returns early for an agent with no
    ``team_name``, and a single-round agent (``enable_task_loop=False``) gets
    its span from that hook alone — ``before_task_iteration`` never fires. A
    single agent has no team, so without this marker it produces **no
    agent-tier span at all**: its llm/tool spans and any sub-agent's
    ``agent.<type>.invoke`` span both attach straight to the run's root span,
    which is what flattens a task-tool sub-agent into the agent layer instead
    of nesting it under the dispatching agent.

    ``team_name`` is a plain attribute on DeepAgent. An agent that already
    carries one is a real team member and is left alone. Best-effort: tracing
    setup must never break a run.

    Args:
        agent: The DeepAgent instance about to run (main agent or sub-agent).
    """
    if agent is None:
        return
    if getattr(agent, "team_name", ""):
        return
    try:
        agent.team_name = SINGLE_AGENT_TEAM_NAME
    except Exception as exc:
        logger.debug("[AgentObservability] set team_name on agent failed: %s", exc)


# Idempotency marker so the patch below is applied at most once per process.
_RAIL_TEAMATTR_PATCH_ATTR = "jiuwenswarm_single_agent_attr_patch"

# Private rail method this module rebinds via getattr/setattr.
_RAIL_STAMP_METHOD = "_stamp_agent_attributes"


def _apply_single_agent_team_attr_suppression() -> None:
    """Drop the ``agentteam.*`` block from single-agent spans.

    Patches ``ObservabilityRail._stamp_agent_attributes`` to rebind a
    single-agent span's ``set_attribute`` so any ``agentteam.*`` key (incl. the
    inline input/output) is discarded; real team members use the original.
    """
    try:
        from openjiuwen.agent_teams.observability import rail as _rail
        from openjiuwen.extensions.observability.semconv import (
            LANGFUSE_OBSERVATION_TYPE,
            LANGFUSE_SESSION_ID,
        )
    except Exception as exc:  # pragma: no cover - openjiuwen unavailable
        logger.debug("[AgentObservability] rail patch import failed: %s", exc)
        return

    rail_cls = _rail.ObservabilityRail
    if getattr(rail_cls, _RAIL_TEAMATTR_PATCH_ATTR, False):
        return  # already patched

    _orig_stamp = getattr(rail_cls, _RAIL_STAMP_METHOD)
    _team_attr_prefix = "agentteam."

    @staticmethod
    def _stamped(span, *, agent, member_name, team_name, session_id, is_leader):
        if team_name != SINGLE_AGENT_TEAM_NAME:
            # Real team member: original stamping.
            _orig_stamp(
                span, agent=agent, member_name=member_name, team_name=team_name,
                session_id=session_id, is_leader=is_leader,
            )
            return

        # Rebind this span's set_attribute to drop agentteam.* keys. The rail's
        # later inline input/output stamps hit the same span, so they're caught too.
        try:
            orig_set_attribute = span.set_attribute

            def _filter_attribute(key, value):
                if isinstance(key, str) and key.startswith(_team_attr_prefix):
                    return
                orig_set_attribute(key, value)

            span.set_attribute = _filter_attribute  # type: ignore[method-assign]
        except Exception as exc:
            logger.debug(
                "[AgentObservability] set_attribute rebind failed: %s", exc
            )
            _orig_stamp(
                span, agent=agent, member_name=member_name, team_name=team_name,
                session_id=session_id, is_leader=is_leader,
            )
            return

        # Keep the two non-agentteam attrs; everything else the original would
        # set is agentteam.* and gets dropped by the filter above.
        span.set_attribute(LANGFUSE_OBSERVATION_TYPE, "agent")
        if session_id:
            span.set_attribute(LANGFUSE_SESSION_ID, session_id)

    setattr(rail_cls, _RAIL_STAMP_METHOD, _stamped)
    setattr(rail_cls, _RAIL_TEAMATTR_PATCH_ATTR, True)


def attach_subagent_observability(subagent: Any) -> None:
    """Give *subagent* its own agent-tier span for the run that dispatches it.

    Without a rail of its own a sub-agent produces no ``agent.<type>.invoke``
    span, so its llm/tool spans attach to the **dispatching** agent's span —
    the sub-agent's whole run then reads as if the parent had made those calls,
    with nothing under the ``task_tool`` span it actually ran inside.

    Attaching at build time is unreliable: the parent agent is constructed
    once, typically before observability is initialized, so
    ``maybe_observability_rail()`` would return None. By dispatch time
    observability is up, and ``add_rail`` still lands before the sub-agent's
    first ``_ensure_initialized()`` registers its hooks.

    Idempotent, and a no-op when observability is off or *subagent* lacks the
    DeepAgent rail API. Best-effort: tracing must never break a run.

    Args:
        subagent: The freshly created sub-agent DeepAgent.
    """
    if subagent is None:
        return
    try:
        from openjiuwen.agent_teams.observability.rail import (
            ObservabilityRail,
            maybe_observability_rail,
        )

        rail = maybe_observability_rail()
        if rail is not None:
            configured = subagent.configured_rails() if hasattr(subagent, "configured_rails") else []
            if not any(isinstance(r, ObservabilityRail) for r in configured):
                if hasattr(subagent, "add_rail"):
                    subagent.add_rail(rail)
    except Exception as exc:
        logger.debug("[AgentObservability] attach subagent rail failed: %s", exc)

    # Released openjiuwen guards ObservabilityRail.before_invoke with
    # ``if not team_name: return``, which no sub-agent can satisfy on its own.
    # Harmless on newer versions, where that guard is gone.
    mark_single_agent_team(subagent)

    # Pin the sub-session id in the session contextvar for the duration of the
    # sub-agent's run, so the sub-agent LLM history forwarder can attribute its
    # LLM calls to the parent session (id format "<parent>_sub_<type>_<uuid>").
    _wrap_subagent_session_context(subagent)


# Marker stamped on the wrapper below so a second install recognizes its own
# work and leaves it alone. The ``jiuwenswarm`` prefix is what keeps it from
# colliding with anything the SDK puts on the same function object, so the name
# carries no leading underscore: it is read from outside the wrapper.
_SUBAGENT_HOOK_MARKER_ATTR = "jiuwenswarm_observability_hooked"


def install_subagent_observability_hook() -> None:
    """Trace every sub-agent, whichever tool dispatched it.

    ``DeepAgent.create_subagent`` is the one point all dispatch paths share —
    the SDK's builtin ``task_tool``, this platform's custom agent tool, and
    background sub-agents. Wrapping it there is what makes tracing independent
    of the dispatcher; hooking a single tool covers only that tool (the
    ``/debug`` capture wrapper used to be the only place a rail was attached,
    so a normal run produced no sub-agent spans at all).

    Idempotent — a second call sees the wrapper already installed. Best-effort:
    never raises, and a failure only costs sub-agent spans.
    """
    try:
        from openjiuwen.harness.deep_agent import DeepAgent
    except Exception as exc:
        logger.debug("[AgentObservability] subagent hook install skipped: %s", exc)
        return

    original = getattr(DeepAgent, "create_subagent", None)
    if original is None or getattr(original, _SUBAGENT_HOOK_MARKER_ATTR, False):
        return

    def create_subagent_with_observability(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Create the sub-agent, then give it its own observability rail."""
        subagent = original(self, *args, **kwargs)
        attach_subagent_observability(subagent)
        return subagent

    setattr(create_subagent_with_observability, _SUBAGENT_HOOK_MARKER_ATTR, True)
    DeepAgent.create_subagent = create_subagent_with_observability


def _wrap_subagent_session_context(subagent: Any) -> None:
    """Make the sub-session id the active session context during a sub-agent run.

    Sub-agent invokes pass ``conversation_id=<sub_session_id>`` (see task_tool).
    Wrapping invoke pins that id into the session contextvar so LLM callbacks
    running inside the sub-agent can be attributed to the parent session.
    Idempotent and best-effort.
    """
    try:
        original = getattr(subagent, "invoke", None)
        if original is None or getattr(original, "_jiuwenswarm_sub_ctx_wrapped", False):
            return
        from openjiuwen.agent_teams.context import reset_session_id, set_session_id

        async def invoke_with_sub_session(inputs: Any, **kwargs: Any) -> Any:
            conv_id = ""
            if isinstance(inputs, dict):
                conv_id = str(inputs.get("conversation_id") or "")
            token = set_session_id(conv_id) if conv_id else None
            try:
                return await original(inputs, **kwargs)
            finally:
                if token is not None:
                    try:
                        reset_session_id(token)
                    except Exception as exc:
                        logger.debug(
                            "[AgentObservability] failed to reset sub-session id %s: %s",
                            token, exc,
                        )

        setattr(invoke_with_sub_session, "_jiuwenswarm_sub_ctx_wrapped", True)
        subagent.invoke = invoke_with_sub_session
        server_logger.info(
            "[AgentObservability] subagent session ctx wrapped: subagent=%s",
            getattr(subagent, "agent_type", type(subagent).__name__),
        )
    except Exception as exc:
        logger.debug("[AgentObservability] wrap subagent session context failed: %s", exc)


# ── Sub-agent LLM calls → parent session history ────────────────
# Each sub-agent runs under a sub-session id "<parent>_sub_<type>_<uuid>". The
# LLM callbacks below detect those calls and append chat.llm_call_start /
# chat.usage_metadata / chat.llm_call_end into the PARENT session's history so
# Logger shows the sub-agent's own LLM calls nested under the task_tool.
#
# NOTE: the agent (and its sub-agents) execute in a different asyncio task than
# the request handler, so a ContextVar is NOT visible there. We therefore keep
# the parent request context in a process-global dict keyed by session id.

_PARENT_REQ_BY_SESSION: dict[str, tuple[str, str, str]] = {}

# Prompt of the most recent sub-agent LLM call per session. ``_on_input`` sees
# the model messages; ``_on_output`` needs the same prompt for the
# usage_metadata record (Logger renders ``um.prompt``). Calls inside a
# sub-agent run are sequential, so a single slot per sub-session is enough.
_LAST_PROMPT_BY_SESSION: dict[str, str] = {}

# Wall-clock start of the most recent sub-agent LLM call per session, so
# ``_on_output`` can stamp the real call duration on the usage_metadata record
# (Logger's Latency / LLM-call latency) instead of leaving it empty.
_LAST_CALL_START_BY_SESSION: dict[str, float] = {}


def set_parent_request_context(*, session_id: str, request_id: str, channel_id: str, mode: str) -> None:
    """Record the parent request context so sub-agent LLM events can be written
    into the parent session's history. Call at request start.
    """
    _PARENT_REQ_BY_SESSION[session_id or "default"] = (request_id, channel_id, mode)
    server_logger.info(
        "[AgentObservability] set_parent_request_context: session=%s rid=%s cid=%s mode=%s",
        session_id, request_id, channel_id, mode,
    )


def _subagent_from_session_id(session_id: str) -> tuple[str, str] | None:
    """If *session_id* is a sub-session id, return (parent_id, subagent_type)."""
    sid = str(session_id or "").strip()
    if "_sub_" not in sid:
        return None
    parent, _, rest = sid.partition("_sub_")
    if not parent or not rest:
        return None
    return parent, rest.split("_")[0]


def _subagent_prompt_preview(messages: Any, max_chars: int = 4000) -> str | None:
    """Minimal serialization of the messages sent to the model (system included).

    The actual query is the FIRST user message — a huge system prompt must never
    push it out of the preview, and the harness may append a runtime-state
    reminder as a trailing user message, so the last one is not the task.
    """
    if not messages:
        return None
    segs: list[tuple[str, str]] = []
    for msg in messages:
        if isinstance(msg, dict):
            role = str(msg.get("role") or "?")
            content = msg.get("content")
        else:
            role = str(getattr(msg, "role", None) or "?")
            content = getattr(msg, "content", None)
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(p.get("text", "")) for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        else:
            text = str(content) if content else ""
        if "data:image" in text:
            text = "[image]"
        segs.append((role, text))

    first_user = -1
    for i, (role, _) in enumerate(segs):
        if role.lower() == "user":
            first_user = i
            break

    def _trunc(seg: str, limit: int) -> str:
        if len(seg) <= limit:
            return seg
        return seg[: max(0, limit)] + "\n… (truncated)"

    user_seg = ""
    if first_user >= 0:
        user_budget = min(max_chars // 2, 1500)
        user_seg = _trunc(f"<user>\n{segs[first_user][1]}", user_budget)

    head = ""
    remaining = max_chars - len(user_seg)
    if remaining > 0:
        head_parts: list[str] = []
        head_total = 0
        for role, text in segs[:first_user]:
            seg = f"<{role}>\n{text}"
            if head_total + len(seg) > remaining:
                head_parts.append(_trunc(seg, max(0, remaining - head_total)))
                head_total = remaining
                break
            head_parts.append(seg)
            head_total += len(seg)
        head = "\n\n".join(head_parts)

    if head and user_seg:
        return head + "\n\n" + user_seg
    return head or user_seg or None


def _is_image_probe_call(messages: Any) -> bool:
    """Return True when *messages* is the harness's image-modality probe payload.

    The probe (``schedule_image_support_probe``) sends a single user message
    whose content is a list containing an ``image_url``/``image`` block plus a
    color-naming text block. It runs inside the sub-session context, so the
    history forwarder would otherwise attribute it to the parent as if it were
    a real sub-agent LLM call — polluting the trace with llm_call_start records
    (and, on a text-only model, a chat.error bubble from the probe's 400).
    """
    if not isinstance(messages, list) or len(messages) != 1:
        return False
    msg = messages[0]
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    if not isinstance(content, list):
        return False
    role = str(msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "") or "")
    if role.lower() != "user":
        return False
    return any(
        isinstance(part, dict) and (
            part.get("type") in ("image_url", "image") or "image_url" in part
        )
        for part in content
    )


def _tool_call_arguments(inputs: Any) -> str:
    """Serialize a tool invoke's positional/keyword arguments into a JSON string."""
    if isinstance(inputs, tuple) and len(inputs) >= 1:
        a, kw = inputs[0], inputs[1] if len(inputs) > 1 else {}
    else:
        a, kw = inputs, {}
    combined: dict[str, Any] = {}
    if isinstance(a, dict):
        combined.update(a)
    elif a is not None:
        combined["input"] = a
    if isinstance(kw, dict):
        combined.update(kw)
    try:
        return json.dumps(combined, ensure_ascii=False, default=str)
    except Exception:
        return str(combined)


def _subagent_active() -> tuple[str, str, tuple[str, str, str]] | None:
    """Return (parent_id, subagent_type, parent_ctx) when the active session is
    a sub-agent run with a known parent request context."""
    from openjiuwen.agent_teams.context import get_session_id as _ctx_session_id

    sub = _subagent_from_session_id(_ctx_session_id())
    if sub is None:
        return None
    ctx = _PARENT_REQ_BY_SESSION.get(sub[0])
    if not ctx:
        return None
    return sub[0], sub[1], ctx


def _append_subagent_llm_record(
    *, parent_id: str, subagent_type: str, ctx: tuple[str, str, str],
    event_type: str, content: str = "", extra: dict[str, Any] | None = None,
) -> None:
    """Write one history record into the parent session for a sub-agent LLM call."""
    try:
        from jiuwenswarm.server.runtime.session.session_history import append_history_record

        rid, cid, mode = ctx
        record_extra = dict(extra or {})
        record_extra["subagent_type"] = subagent_type
        # The sub-session id distinguishes parallel sub-agents of the same type
        # (Logger groups sub-agent sections by it).
        from openjiuwen.agent_teams.context import get_session_id as _ctx_session_id

        record_extra["sub_session_id"] = _ctx_session_id() or parent_id
        append_history_record(
            session_id=parent_id,
            request_id=rid,
            channel_id=cid,
            role="assistant",
            event_type=event_type,
            content=content,
            timestamp=time.time(),
            extra=record_extra or None,
            mode=mode,
        )
        logger.info(
            "[AgentObservability] forwarded subagent llm record: session=%s subagent=%s event=%s",
            parent_id, subagent_type, event_type,
        )
    except Exception as exc:
        server_logger.warning(
            "[AgentObservability] subagent history record failed: session=%s subagent=%s event=%s err=%s",
            parent_id, subagent_type, event_type, exc,
        )


def install_subagent_llm_history_forwarder() -> None:
    """Forward sub-agent LLM calls into the parent session's history.

    Registers LLM callbacks on the global Runner callback framework. A call is
    attributed to a sub-agent when the active session contextvar is a sub-session
    id ("<parent>_sub_<type>_<uuid>"). Best-effort: never raises.
    """
    try:
        from openjiuwen.core.runner import Runner
        from openjiuwen.core.runner.callback.events import LLMCallEvents, ToolCallEvents
        from openjiuwen.agent_teams.context import get_session_id as _ctx_session_id
    except Exception as exc:
        logger.debug("[AgentObservability] subagent history forwarder skipped: %s", exc)
        return

    framework = getattr(Runner, "callback_framework", None)
    if framework is None:
        server_logger.info(
            "[AgentObservability] subagent history forwarder: "
            "Runner.callback_framework unavailable — not installed"
        )
        return

    server_logger.info("[AgentObservability] subagent history forwarder installing handlers on %r", framework)

    ns = "jiuwenswarm-subagent-history"

    async def _on_input(*args: Any, **kwargs: Any) -> None:
        try:
            from openjiuwen.agent_teams.context import get_session_id as _ctx_session_id
            sid = _ctx_session_id()
            sub = _subagent_from_session_id(sid)
            server_logger.info(
                "[AgentObservability] subagent llm input cb fired: session=%s sub=%s parent_ctx=%s",
                sid, sub, _PARENT_REQ_BY_SESSION.get((sub or ("", ""))[0]) if sub else None,
            )
            if sub:
                try:
                    msgs = kwargs.get("messages") or []
                    parts = []
                    for m in msgs:
                        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "?")
                        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
                        img_in = 0
                        if isinstance(content, list):
                            for p in content:
                                if (
                                    isinstance(p, dict)
                                    and (
                                        p.get("type") in ("image_url", "image")
                                        or "image_url" in p
                                    )
                                ):
                                    img_in += 1
                        parts.append(f"{role}[img={img_in}]")
                    server_logger.info(
                        "[AgentObservability] subagent msgs: n=%d %s",
                        len(msgs), " ".join(parts[:12]),
                    )
                except Exception as exc:
                    server_logger.warning("[AgentObservability] subagent msgs dump failed: %s", exc)
            # The image-modality probe is a background diagnostic, not a real
            # sub-agent LLM call: skip it so its records (and, on text-only
            # models, its 400 chat.error bubble) do not pollute the trace.
            if _is_image_probe_call(kwargs.get("messages")):
                return
            active = _subagent_active()
            if not active:
                return
            parent_id, sub_type, ctx = active
            prompt = _subagent_prompt_preview(kwargs.get("messages"))
            if prompt:
                _LAST_PROMPT_BY_SESSION[sid] = prompt
            _LAST_CALL_START_BY_SESSION[sid] = time.time()
            _append_subagent_llm_record(
                parent_id=parent_id, subagent_type=sub_type, ctx=ctx,
                event_type="chat.llm_call_start",
                extra={"prompt": prompt} if prompt else {},
            )
        except Exception as exc:
            server_logger.warning("[AgentObservability] subagent llm_start forward failed: %s", exc)

    async def _on_output(*args: Any, **kwargs: Any) -> None:
        try:
            active = _subagent_active()
            if not active:
                return
            parent_id, sub_type, ctx = active
            response = kwargs.get("response") or kwargs.get("result")
            content = ""
            if isinstance(response, str):
                content = response
            elif response is not None:
                content = str(getattr(response, "content", "") or "")

            usage = kwargs.get("usage") or getattr(response, "usage_metadata", None)
            model = (
                kwargs.get("model")
                or kwargs.get("model_name")
                or getattr(response, "model", None)
                or ""
            )
            usage_meta: dict[str, Any] = {"model_name": str(model or "")}
            usage_meta["code"] = 0
            usage_meta["err_msg"] = ""
            from openjiuwen.agent_teams.context import get_session_id as _ctx_session_id2

            usage_meta["prompt"] = _LAST_PROMPT_BY_SESSION.get(_ctx_session_id2(), "")
            if usage is not None:
                um = usage.model_dump() if hasattr(usage, "model_dump") else (
                    usage if isinstance(usage, dict) else {}
                )
                for key in ("input_tokens", "output_tokens", "total_tokens", "cache_tokens"):
                    val = um.get(key) if isinstance(um, dict) else None
                    if val is not None:
                        usage_meta[key] = val

            usage_metadata = {"usage_metadata": usage_meta}
            call_start = _LAST_CALL_START_BY_SESSION.get(_ctx_session_id2(), 0.0)
            if call_start:
                usage_metadata["total_latency_ms"] = (time.time() - call_start) * 1000.0

            _append_subagent_llm_record(
                parent_id=parent_id, subagent_type=sub_type, ctx=ctx,
                event_type="chat.usage_metadata",
                extra={"metadata": usage_metadata},
            )
            _append_subagent_llm_record(
                parent_id=parent_id, subagent_type=sub_type, ctx=ctx,
                event_type="chat.llm_call_end",
                content=content[:2000],
            )
        except Exception as exc:
            server_logger.warning("[AgentObservability] subagent llm_end forward failed: %s", exc)

    async def _on_error(*args: Any, **kwargs: Any) -> None:
        # Real sub-agent failures surface through the task_tool result (the
        # parent reports them in its own output). Forwarding the raw model
        # error here also catches background probes, producing misleading
        # chat.error bubbles in the parent history — so errors are not copied.
        pass

    async def _on_tool_start(*args: Any, **kwargs: Any) -> None:
        try:
            active = _subagent_active()
            if not active:
                return
            parent_id, sub_type, ctx = active
            tool_name = kwargs.get("tool_name") or ""
            tool_id = kwargs.get("tool_id") or ""
            tool_call = {"name": tool_name, "arguments": _tool_call_arguments(kwargs.get("inputs"))}
            if tool_id:
                tool_call["tool_call_id"] = tool_id
            _append_subagent_llm_record(
                parent_id=parent_id, subagent_type=sub_type, ctx=ctx,
                event_type="chat.tool_call",
                extra={"tool_call": tool_call},
            )
        except Exception as exc:
            server_logger.warning("[AgentObservability] subagent tool_start forward failed: %s", exc)

    async def _on_tool_end(*args: Any, **kwargs: Any) -> None:
        try:
            active = _subagent_active()
            if not active:
                return
            parent_id, sub_type, ctx = active
            result = kwargs.get("result")
            _append_subagent_llm_record(
                parent_id=parent_id, subagent_type=sub_type, ctx=ctx,
                event_type="chat.tool_result",
                extra={
                    "result": str(result) if result is not None else "",
                    "tool_name": kwargs.get("tool_name") or "",
                    "tool_call_id": kwargs.get("tool_id") or "",
                },
            )
        except Exception as exc:
            server_logger.warning("[AgentObservability] subagent tool_end forward failed: %s", exc)

    async def _on_tool_error(*args: Any, **kwargs: Any) -> None:
        try:
            active = _subagent_active()
            if not active:
                return
            parent_id, sub_type, ctx = active
            error = kwargs.get("error")
            _append_subagent_llm_record(
                parent_id=parent_id, subagent_type=sub_type, ctx=ctx,
                event_type="chat.tool_result",
                extra={
                    "result": "",
                    "tool_name": kwargs.get("tool_name") or "",
                    "tool_call_id": kwargs.get("tool_id") or "",
                    "error": str(error) if error is not None else "tool call error",
                    "error_type": "subagent_tool_error",
                },
            )
        except Exception as exc:
            server_logger.warning("[AgentObservability] subagent tool_error forward failed: %s", exc)

    pairs: list[tuple[str, Any]] = [
        (LLMCallEvents.LLM_INVOKE_INPUT, _on_input),
        (LLMCallEvents.LLM_STREAM_INPUT, _on_input),
        (LLMCallEvents.LLM_OUTPUT, _on_output),
        (ToolCallEvents.TOOL_CALL_STARTED, _on_tool_start),
        (ToolCallEvents.TOOL_CALL_FINISHED, _on_tool_end),
        (ToolCallEvents.TOOL_CALL_ERROR, _on_tool_error),
    ]
    for event, cb in pairs:
        try:
            framework.register_sync(event, cb, namespace=ns)
        except Exception as exc:
            logger.debug("[AgentObservability] subagent history forwarder register failed: %s", exc)


def _build_run_span_name(*, mode: str, session_id: str) -> str:
    """Build a hierarchical OTel span name: ``agent.<mode>.<session_id>``.

    ``mode`` is the JiuwenSwarm request mode, shaped ``<category>.<submode>``
    (e.g. ``agent.plan`` / ``agent.fast`` / ``code.normal`` / ``code.plan``),
    so it yields the hierarchy directly:

        agent.plan  -> agent.agent.plan.<session_id>
        code.normal -> agent.code.normal.<session_id>

    Falls back gracefully when either component is empty.
    """
    m = (mode or "").strip()
    sid = (session_id or "").strip()
    if not m:
        return f"agent.run.{sid}" if sid else "agent.run"
    if not sid:
        return f"agent.{m}.run"
    return f"agent.{m}.{sid}"


def open_agent_run_span(*, session_id: str = "", mode: str = "") -> Any:
    """Open a root team span around a single-agent run.

    Returns an opaque handle to pass to :func:`close_agent_run_span`, or
    ``None`` when observability is not initialized (in which case closing is
    a no-op).
    """
    try:
        from opentelemetry.trace import SpanKind

        from openjiuwen.extensions.observability.setup import (
            get_tracer,
            is_initialized,
        )
        from openjiuwen.extensions.observability.semconv import LANGFUSE_SESSION_ID
        from openjiuwen.agent_teams.observability.span_context import (
            set_current_session_id,
            set_root_span,
            set_team_span,
        )

        if not is_initialized():
            return None
        if not _agent_observability_active:
            return None

        tracer = get_tracer("jiuwenswarm.agent")
        name = _build_run_span_name(mode=mode, session_id=session_id)
        span = tracer.start_span(name=name, kind=SpanKind.SERVER)
        span.set_attribute(LANGFUSE_SESSION_ID, session_id or "")
        # Tag the mode so traces can be filtered in Langfuse without parsing
        # the span name.
        span.set_attribute("jiuwenswarm.mode", mode or "")
        # Register as the team/root span so parent lookup finds it for LLM/tool
        # span creation. Pass session_id into the SDK registry as well as our
        # local fallback table — supervisor tasks may not inherit ContextVars.
        sid = session_id or ""
        set_team_span(span, team_name=SINGLE_AGENT_TEAM_NAME)
        set_root_span(span, session_id=sid)
        set_current_session_id(sid)
        _ROOT_SPANS[sid] = span
        logger.info("[AgentObservability] root span opened: name=%s", name)
        return span
    except Exception as exc:
        logger.warning("[AgentObservability] open root span failed: %s", exc)
        return None


def _stamp_run_output(handle: Any, output: str) -> None:
    """Write the run's final answer onto the root span as the trace output.

    Team mode fills the equivalent attribute on its ``team.<name>`` span from
    the leader's iteration result (``ObservabilityRail.after_task_iteration``),
    which keys off ``TeamRole.LEADER`` and therefore never fires for a single
    agent — leaving the Langfuse trace with an empty top-level output. The
    single-agent counterpart is the run's final answer, stamped here.

    Redaction follows the active ``ObservabilityConfig`` so ``redact_completions``
    covers this attribute exactly as it covers llm/agent span outputs.

    Args:
        handle: The still-recording root span.
        output: Final answer text; empty means nothing to stamp.
    """
    if not output:
        return
    from openjiuwen.extensions.observability.redaction import redact_completion
    from openjiuwen.extensions.observability.semconv import LANGFUSE_OBSERVATION_OUTPUT
    # Aliased: the module-level ``get_config`` is JiuwenSwarm's own settings
    # reader, and this SDK-side one returns the active ObservabilityConfig.
    from openjiuwen.agent_teams.observability.setup import get_config as get_observability_config

    config = get_observability_config()
    text = redact_completion(output, config) if config else output
    handle.set_attribute(LANGFUSE_OBSERVATION_OUTPUT, text)


def close_agent_run_span(handle: Any, *, session_id: str = "", output: str = "") -> None:
    """End the root span opened by :func:`open_agent_run_span` and clear it.

    Args:
        handle: Opaque handle from :func:`open_agent_run_span`; None is a no-op.
        session_id: Session the run belonged to; its registry entry is dropped.
        output: The run's final answer, stamped as the trace-level output.
            Empty (aborted / errored run) leaves the attribute unset.
    """
    # Drop this run's fallback entry — and only this run's. Sessions overlap,
    # so clearing whatever happens to be registered would blind a run that is
    # still going (its sub-agents would lose their spans mid-run).
    if _ROOT_SPANS.get(session_id or "") is handle:
        _ROOT_SPANS.pop(session_id or "", None)
    if handle is None:
        return
    try:
        from openjiuwen.agent_teams.observability.span_context import (
            cascade_close_children,
            clear_root_span,
            clear_team_span,
            flush_child_spans,
        )

        try:
            _stamp_run_output(handle, output)
        except Exception as exc:
            logger.debug("[AgentObservability] stamp run output failed: %s", exc)

        # End any still-open child LLM/tool spans (e.g. run aborted mid-call).
        # Two nets are needed for the single-agent path:
        #   1. cascade_close_children — closes spans whose state was pushed on
        #      the _llm_span_stack / _tool_span_map ContextVars in THIS context.
        #   2. flush_child_spans — the SpanProcessor-backed safety net Team mode
        #      relies on (finalize_trace -> flush_child_spans via
        #      ActiveSpanTracker). The single-agent runner opens LLM spans inside
        #      its own child context, so their ContextVar state is not visible
        #      here; the tracker closes them by trace_id regardless of context.
        # Both must run BEFORE clear_team_span(): flush_child_spans reads the
        # team span ContextVar to resolve this trace's id, and scopes the close
        # to our trace only (flush_spans_for_trace), so concurrent runs are not
        # affected.
        #
        # Ordering note — the root span is ended BETWEEN the two nets, not after
        # them: ``flush_spans_for_trace`` spares only spans whose name starts
        # with ``team.`` (Team mode's root), so our ``agent.<mode>.<sid>`` root
        # would otherwise be swept up as a leaked child — reported as an ORPHAN
        # warning, force-ended by the tracker, and then re-ended here ("Calling
        # end() on an ended span"). Ending it first makes it non-recording, which
        # the tracker skips, so the root keeps its own end time and status while
        # the net still catches genuinely leaked children.
        try:
            cascade_close_children()
        except Exception as exc:
            logger.debug("[AgentObservability] cascade_close_children failed: %s", exc)
        try:
            handle.end()
        except Exception as exc:
            logger.debug("[AgentObservability] end root span failed: %s", exc)
        try:
            flush_child_spans()
        except Exception as exc:
            logger.debug("[AgentObservability] flush_child_spans failed: %s", exc)
        try:
            clear_root_span(session_id=session_id or "", expected_span=handle)
        except Exception as exc:
            logger.debug("[AgentObservability] clear_root_span failed: %s", exc)
        clear_team_span()
    except Exception as exc:
        logger.warning("[AgentObservability] close root span failed: %s", exc)
