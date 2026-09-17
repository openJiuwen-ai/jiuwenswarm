# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Single-agent / coding-agent observability lifecycle.

This is the non-team counterpart of the team observability adapter in
``jiuwenswarm.agents.harness.team.team_manager`` (``sync_team_observability``
/ ``shutdown_team_observability``). It is kept in a **separate file with its
own state and config section** on purpose, so the existing team scenario is
not affected.

Once ``openjiuwen.agent_teams.observability.init_observability`` has run, the
generic ``OtelCallbackHandler`` is registered against the **global**
``Runner.callback_framework``. LLM and tool events are emitted from the shared
foundation layer (``core/foundation/llm/model.py`` /
``core/foundation/tool/base.py``) for *every* agent, team or not — so simply
ensuring the provider is initialized before ``Runner.run_agent_streaming`` /
``Runner.run_agent`` gives single-agent and coding-agent runs automatic
LLM/tool span tracing. The team-only ``OtelTeamMonitorHandler`` (team/member/
task/message spans) is intentionally never attached here.

Shared-provider caveat (important):
    OpenTelemetry allows exactly ONE global ``TracerProvider`` per process,
    and ``init_observability`` is a no-op if already initialized. In a process
    where BOTH team and agent observability are enabled, whichever runs first
    wins; the other silently reuses it (its exporter/endpoint/service_name are
    ignored). Provider demands are coordinated by ``observability_runtime`` so
    agent shutdown never tears down a provider the team subsystem depends on.
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.config import (
    get_config,
    get_skill_evolution_enabled,
)
from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.agents.harness.observability_runtime import (
    acquire_observability_demand,
    build_observability_config,
    release_observability_demand,
)

logger = logging.getLogger(__name__)

# ── Single-Agent Observability ─────────────────────────────────
# Tracks whether observability is currently active so we can detect config
# toggles (enabled -> disabled or vice-versa) and init / shutdown accordingly
# on each single-agent request.
_agent_observability_active: bool = False

# Root spans of the runs currently in flight, keyed by session id.
#
# The per-request root ContextVar can't reach the round tasks (agent execution
# runs in a session-setup supervisor task), so SDK lookups that only see the
# ContextVar return None there. The wrappers installed below fall back to this
# registry, which works regardless of task/context boundary.
#
# Keyed rather than a single "current run" slot because sessions overlap: a
# process serves several chats at once, and a single slot made them fight over
# it. Whoever finished first cleared it, so a run still in progress silently
# lost its agent-tier spans from that moment on (its sub-agents landed flat
# under the dispatching agent) — and before that, whoever opened last owned the
# slot, so the other run's spans would have joined the wrong trace.
_ROOT_SPANS: dict[str, Any] = {}

# Marker set on the wrapped ``get_root_span`` / ``get_team_span`` callables so
# install stays idempotent and tests can assert the fallback is in place.
_SDK_ROOT_SPAN_FALLBACK_ATTR = "_jiuwenswarm_root_span_fallback"


def _is_recording(span: Any) -> bool:
    """Report whether *span* is still open, tolerating stubs without the API."""
    try:
        return bool(span is not None and span.is_recording())
    except Exception:
        return False


def _resolve_root_span() -> Any:
    """Return the root span of the run the calling task belongs to, or None.

    Resolution is by session id first: ``get_session_id`` is set by the SDK
    around agent execution, so it is readable from the tasks the ContextVar
    cannot reach — which is exactly where this fallback is needed.

    When no session id is in reach, a single run in flight is unambiguous and
    answers. Several in flight with no way to tell them apart returns None
    rather than a guess: attaching one run's spans to another run's trace is
    worse than the span being missing.
    """
    session_id = ""
    try:
        from openjiuwen.agent_teams.context import get_session_id

        session_id = get_session_id() or ""
    except Exception as exc:
        logger.debug("[AgentObservability] session id lookup failed: %s", exc)

    span = _ROOT_SPANS.get(session_id)
    if _is_recording(span):
        return span

    live = [candidate for candidate in list(_ROOT_SPANS.values()) if _is_recording(candidate)]
    if len(live) == 1:
        return live[0]
    return None


def _install_team_span_global_fallback() -> None:
    """Wrap SDK root/team span lookups with the session-keyed ``_ROOT_SPANS`` fallback.

    Newer openjiuwen moved team-span state into
    ``extensions.observability.span_context.get_root_span`` (session registry +
    ContextVar). ``get_team_span`` is a thin facade over that. Wrapping both
    the extension accessor and the team facade keeps:

    * ``get_team_span()`` (rail / callback parent lookup), and
    * ``ActiveSpanTracker`` parent resolution (via ``get_root_span``),

    able to see the single-agent root even when the ContextVar is invisible to
    the supervisor task.

    Best-effort, idempotent, never raises — observability must never break a run.
    """
    try:
        from openjiuwen.agent_teams.observability import span_context as team_sc
        from openjiuwen.extensions.observability import span_context as ext_sc
    except Exception as exc:
        logger.debug("[AgentObservability] skip team-span fallback install: %s", exc)
        return

    original = getattr(ext_sc, "get_root_span", None)
    if original is None or getattr(original, _SDK_ROOT_SPAN_FALLBACK_ATTR, False):
        return

    def get_root_span_with_fallback(*, session_id: str | None = None):
        try:
            span = original(session_id=session_id)
        except TypeError:
            span = original()
        if _is_recording(span):
            return span
        return _resolve_root_span()

    setattr(get_root_span_with_fallback, _SDK_ROOT_SPAN_FALLBACK_ATTR, True)
    ext_sc.get_root_span = get_root_span_with_fallback
    team_sc.get_root_span = get_root_span_with_fallback
    # callback_handler imports get_root_span by name at module load; rebind that
    # early binding too, otherwise LLM/tool parent lookup still sees the unwrapped
    # accessor when the handler was imported before this install ran.
    try:
        from openjiuwen.extensions.observability import callback_handler as ch

        ch.get_root_span = get_root_span_with_fallback
    except Exception as exc:
        logger.debug("[AgentObservability] callback_handler rebind skipped: %s", exc)

    def get_team_span_with_fallback(team_name: str | None = None):
        del team_name
        return get_root_span_with_fallback()

    setattr(get_team_span_with_fallback, _SDK_ROOT_SPAN_FALLBACK_ATTR, True)
    team_sc.get_team_span = get_team_span_with_fallback


_install_team_span_global_fallback()
# Sticky flag: once any single-agent request has force-enabled observability
# (e.g. a ``/debug`` run with ``debug_trace.<mode>.otel_enabled``), we never
# auto-teardown the provider for the rest of the process. OTel allows only one
# global TracerProvider and re-init after shutdown is fragile, so a /debug
# toggle must not churn init/shutdown across alternating requests. The normal
# config-gated path (agent_observability.enabled hot-reload) is unaffected
# unless force was ever used.
_force_ever_enabled: bool = False


def sync_agent_observability(*, force: bool = False) -> None:
    """Synchronize single-agent observability state with current config.

    Called before each ``Runner.run_agent_streaming`` / ``Runner.run_agent`` so
    that hot-reloading the ``agent_observability.enabled`` flag takes effect
    immediately:

    * disabled -> enabled : ``init_observability()`` (or reuse if already up)
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
    evolution_requested = get_skill_evolution_enabled(config)
    want_enabled = (
        bool(cfg.get("enabled", False))
        or evolution_requested
        or force
        or _force_ever_enabled
    )
    if force:
        _force_ever_enabled = True

    # Single-agent spans carry a redundant agentteam.* block; drop it. Scoped
    # to single-agent runs — real team members keep their team attrs.
    if want_enabled:
        _apply_single_agent_team_attr_suppression()

    if not want_enabled:
        if _agent_observability_active:
            shutdown_agent_observability()
        return

    try:
        traces_dir = str(cfg.get("traces_dir") or get_user_workspace_dir() / ".trace")
        obs_cfg = build_observability_config(
            cfg,
            service_name="jiuwenswarm-agent",
            traces_dir=traces_dir,
        )
        provider_existed = acquire_observability_demand(
            "agent",
            observability_config=obs_cfg,
        )
        was_active = _agent_observability_active
        _agent_observability_active = True
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
    if not _agent_observability_active:
        return
    try:
        release_observability_demand("agent")
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
        if rail is None:
            return  # observability not initialized -> nothing to trace
        configured = subagent.configured_rails() if hasattr(subagent, "configured_rails") else []
        if any(isinstance(r, ObservabilityRail) for r in configured):
            return  # already attached — never add a second one
        if hasattr(subagent, "add_rail"):
            subagent.add_rail(rail)
    except Exception as exc:
        logger.debug("[AgentObservability] attach subagent rail failed: %s", exc)

    # Released openjiuwen guards ObservabilityRail.before_invoke with
    # ``if not team_name: return``, which no sub-agent can satisfy on its own.
    # Harmless on newer versions, where that guard is gone.
    mark_single_agent_team(subagent)


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
