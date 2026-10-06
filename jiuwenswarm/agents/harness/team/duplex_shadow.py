# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native A2A/U2A routing adapter at the SDK's pre-delivery boundary.

The SDK owns DB ordering, broadcasts, ACKs, and all delivery/lifecycle actions.
Only this adapter knows its MessageHandler and NativeHarness accessors.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import uuid
from functools import wraps
from typing import Any

from jiuwenswarm.common.duplex_router import (
    ControlSnapshot, InboundMessage, SYSTEM_PROMPT, decision_schema, observe, prompt_for,
)

logger = logging.getLogger(__name__)


class RoutedInput(str):
    """Original SDK rendering plus DB identity; not a second message history."""

    def __new__(cls, text, message):
        obj = super().__new__(cls, text)
        obj.message = message
        return obj


def native_from_runtime(runtime):
    from openjiuwen.agent_teams.harness.native_harness import NativeHarness
    from openjiuwen.agent_teams.harness.team_harness import TeamHarness

    if isinstance(runtime, TeamHarness):
        runtime = runtime.inner_agent
    return runtime if isinstance(runtime, NativeHarness) else None


def snapshot_from_native(harness: Any) -> ControlSnapshot | None:
    """Read the existing task/plan and execution position without asking the model."""
    active = harness.active_round
    if active is None:
        return None
    checkpoint = active.last_iter_snapshot or active.pre_round_snapshot
    if checkpoint is None:
        return None
    plan = checkpoint.deep_agent_state.get("task_plan") or {}
    if getattr(harness, "_session", None) is not None:
        plan = harness.load_state(harness._session).to_session_dict().get("task_plan") or {}
    current = next((task for task in plan.get("tasks", [])
                    if task.get("id") == plan.get("current_task_id")), {})
    provider = getattr(harness, "_duplex_goal_provider", None)
    query = provider() if provider else active.original_query
    if isinstance(query, list):
        query = "\n".join(str(item) for item in query)
    goal = str(plan.get("goal") or (query if isinstance(query, str) else ""))
    next_action = str(current.get("description") or current.get("content") or "")
    action_provider = getattr(harness, "_duplex_action_provider", None)
    if action_provider is not None:
        actual_action = action_provider()
        if isinstance(actual_action, str) and actual_action:
            next_action = (next_action + "\nActual execution: " + actual_action[:2400]).strip()
    phase = str(getattr(active.iter_phase, "value", active.iter_phase))
    last_provider = getattr(harness, "_duplex_last_action_provider", None)
    last_action = str(last_provider() or "")[:2400] if last_provider is not None else ""
    version = hashlib.sha256(json.dumps([
        id(active), id(checkpoint), phase, active.pause_requested, goal, next_action, last_action,
        getattr(getattr(harness, "_st", None), "seq_counter", 0),
    ]).encode()).hexdigest()[:20]
    return ControlSnapshot(context_version=version, round_id=str(active.round_id),
        checkpoint_id=f"{active.round_id}:{checkpoint.iteration_index}",
        phase=phase, goal=goal, next_action=next_action, last_action=last_action)


async def classify_input(host, model_name, snapshot, messages):
    """One isolated fast-model request, with no worker or private state."""
    from openjiuwen.agent_teams import create_tiny_agent

    async with create_tiny_agent(
        system_prompt=SYSTEM_PROMPT, model_name=model_name,
        model_resolver=host.tiny_agent_model_resolver,
        default_schema=decision_schema(), name=f"duplex-{uuid.uuid4().hex}",
        language="en", max_iterations=1,
    ) as agent:
        return await agent.run(prompt_for(snapshot, messages))


def _decision_timeout(host, config, policy, backend, model_name):
    timeout = config.get("timeout_seconds")
    if timeout is None and policy != "always_interrupt":
        if backend == "mindshub":
            from jiuwenswarm.common.duplex_mindshub import DEFAULT_TIMEOUT_SECONDS

            timeout = DEFAULT_TIMEOUT_SECONDS
        elif backend == "jev":
            from jiuwenswarm.common.duplex_jev import DEFAULT_TIMEOUT_SECONDS

            timeout = DEFAULT_TIMEOUT_SECONDS
        elif backend == "clef":
            from jiuwenswarm.common.duplex_clef import clef_timeout

            timeout = clef_timeout(config.get("clef"))
        elif backend == "sdk":
            timeout = host.tiny_agent_model_resolver(model_name).model_client_config.timeout
    return float(timeout) if timeout is not None else None


def _classifier(host, config, policy, backend, model_name, timeout):
    async def classify(state, messages):
        record_input = getattr(host, "record_duplex_input", None)
        if record_input is not None:
            record_input(state, messages)
        if policy == "always_interrupt":
            return {"action": "INTERRUPT"}
        if backend == "mindshub":
            from jiuwenswarm.common.duplex_mindshub import DEFAULT_MODEL, classify_mindshub

            return await classify_mindshub(state, messages, model_name=model_name or DEFAULT_MODEL,
                                           settings=config.get("mindshub"), timeout_seconds=timeout)
        if backend == "jev":
            from jiuwenswarm.common.duplex_jev import DEFAULT_MODEL, classify_jev

            return await classify_jev(state, messages, model_name=model_name or DEFAULT_MODEL,
                                      settings=config.get("jev"), timeout_seconds=timeout)
        if backend == "clef":
            from jiuwenswarm.common.duplex_clef import classify_clef

            return await classify_clef(state, messages, settings=config.get("clef"),
                                       timeout_seconds=timeout)
        if backend != "sdk":
            raise ValueError("unsupported duplex router backend")
        return await classify_input(host, model_name, state, messages)

    return classify


async def deliver_routed(host, content, *, use_steer, original, settings=None):
    from jiuwenswarm.common.config import get_config
    from .duplex_native import DuplexNativeHarness

    config = settings if settings is not None else getattr(host, "duplex_settings", None)
    if config is None:
        config = get_config().get("duplex_router", {}) or {}
    native = native_from_runtime(host.harness)
    message = content.message
    backend = config.get("backend", "sdk")
    logger.info("duplex route entry backend=%s mode=%s recipient=%s message_id=%s sender=%s "
                "use_steer=%s harness_type=%s",
                backend, config.get("mode", "off"),
                getattr(getattr(host, "blueprint", None), "member_name", "unknown"),
                message.message_id, message.sender, use_steer, type(native).__name__)
    if config.get("mode") != "active" or not isinstance(native, DuplexNativeHarness):
        logger.info("duplex route bypass backend=%s message_id=%s reason=%s",
                    backend, message.message_id,
                    "mode_not_active" if config.get("mode") != "active" else "unsupported_harness")
        return await original(host, str(content), use_steer=use_steer)
    if message.message_id in native._duplex_received:
        logger.info("duplex route bypass backend=%s message_id=%s reason=duplicate", backend, message.message_id)
        return

    async def steer():
        result = await original(host, str(content),
                                use_steer=use_steer and config.get("policy") != "serial")
        native._duplex_received.add(message.message_id)
        return result

    policy = config.get("policy", "model")
    snapshot = snapshot_from_native(native)
    if not use_steer or policy in ("serial", "steer") or snapshot is None:
        reason = "use_steer_false" if not use_steer else "policy_bypass" if policy in ("serial", "steer") else "no_snapshot"
        logger.info("duplex route bypass backend=%s message_id=%s reason=%s policy=%s",
                    backend, message.message_id, reason, policy)
        return await steer()
    model_name = str(config.get("model_name") or "")
    logger.info("duplex route start backend=%s message_id=%s round_id=%s checkpoint_id=%s phase=%s",
                backend, message.message_id, snapshot.round_id, snapshot.checkpoint_id, snapshot.phase)

    try:
        timeout = _decision_timeout(host, config, policy, backend, model_name)
        classify = _classifier(host, config, policy, backend, model_name, timeout)
        observation = await observe(snapshot, (message,), classify=classify,
             timeout_seconds=timeout)
    except Exception as exc:
        logger.warning("duplex route fallback backend=%s message_id=%s reason=classification_exception "
                       "error_type=%s effective=APPEND", backend, message.message_id, type(exc).__name__)
        return await steer()
    recorder = getattr(host, "record_duplex_observation", None)
    if recorder is not None:
        recorder(observation)
    logger.info("duplex route observation backend=%s message_id=%s status=%s proposed=%s latency_ms=%.1f attempts=%d",
                backend, message.message_id, observation.status, observation.proposed_action,
                observation.latency_ms, observation.attempts)
    if observation.status != "ok" or observation.proposed_action == "APPEND":
        result = await steer()
        logger.info("duplex route effective backend=%s message_id=%s action=APPEND reason=%s",
                    backend, message.message_id,
                    observation.status if observation.status != "ok" else "model_append")
        return result
    effective = await native.interrupt(str(content), version=observation.context_version,
                                       message_id=message.message_id)
    if effective == "UNAVAILABLE":
        result = await steer()
        logger.info("duplex route effective backend=%s message_id=%s action=APPEND reason=inactive_or_pending",
                    backend, message.message_id)
        return result
    logger.info("duplex route effective backend=%s message_id=%s action=%s",
                backend, message.message_id, effective)
    return effective


def install_shadow_observer() -> bool:
    """Install the minimal routing shim; unsupported SDKs keep their old behavior.

    Rendering is the narrow synchronous point after DB read/expansion and before
    deliver_input. Inheriting the drain avoids duplicating ACK/watermark logic.
    """
    try:
        from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
        from openjiuwen.agent_teams.agent.team_agent import TeamAgent
    except ImportError:
        logger.warning("duplex shadow unavailable: unsupported SDK")
        return False

    original = getattr(MessageHandler, "_format_message", None)
    if original is None:
        logger.warning("duplex shadow unavailable: SDK hooks missing")
        return False
    if getattr(original, "_duplex_shadow", False):
        return True
    try:
        inspect.signature(original).bind(None, None, expanded=None,
                                         is_human_agent=False, now_ms=0)
    except TypeError:
        logger.warning("duplex shadow unavailable: SDK rendering signature changed")
        return False

    @wraps(original)
    def render(self, msg, *, expanded, is_human_agent, now_ms):
        text = original(self, msg, expanded=expanded, is_human_agent=is_human_agent, now_ms=now_ms)
        try:
            if not is_human_agent and not expanded.is_template and msg.protocol in (None, "", "text", "plain"):
                text = RoutedInput(text, InboundMessage(str(msg.message_id),
                                   str(msg.from_member_name), expanded.body))
        except Exception:
            logger.warning("duplex shadow skipped; delivery unchanged", exc_info=True)
        return text

    render._duplex_shadow = True
    MessageHandler._format_message = render

    deliver = TeamAgent.deliver_input

    @wraps(deliver)
    async def deliver_with_route(self, content, *, use_steer=True):
        if isinstance(content, RoutedInput):
            return await deliver_routed(self, content, use_steer=use_steer, original=deliver)
        logger.info("duplex delivery bypass recipient=%s reason=unwrapped_input use_steer=%s input_type=%s",
                    getattr(getattr(self, "blueprint", None), "member_name", "unknown"),
                    use_steer, type(content).__name__)
        return await deliver(self, content, use_steer=use_steer)

    TeamAgent.deliver_input = deliver_with_route
    from openjiuwen.agent_teams.agent.coordination.handlers.agent_lifecycle import AgentLifecycleHandler
    user_input = AgentLifecycleHandler.on_user_input

    @wraps(user_input)
    async def route_user_input(self, event):
        content = event.payload.get("content", "")
        logger.info("duplex user input entry recipient=%s input_type=%s already_wrapped=%s",
                    getattr(getattr(self, "_blueprint", None), "member_name", "unknown"),
                    type(content).__name__, isinstance(content, RoutedInput))
        if isinstance(content, str) and not isinstance(content, RoutedInput):
            message = InboundMessage(str(event.payload.get("message_id") or f"u2a-{uuid.uuid4().hex}"),
                                     "user", content)
            event = event.model_copy(update={"payload": {**event.payload,
                                                          "content": RoutedInput(content, message)}})
            logger.info("duplex user input wrapped message_id=%s sender=user", message.message_id)
        return await user_input(self, event)

    AgentLifecycleHandler.on_user_input = route_user_input
    from openjiuwen.agent_teams.harness import team_harness
    base_native = team_harness.NativeHarness

    def native_factory(*args, **kwargs):
        from jiuwenswarm.common.config import get_config
        config = get_config().get("duplex_router", {}) or {}
        if config.get("mode") == "active":
            from jiuwenswarm.agents.harness.team.duplex_native import DuplexNativeHarness
            return DuplexNativeHarness(*args, **kwargs)
        return base_native(*args, **kwargs)

    team_harness.NativeHarness = native_factory
    return True
