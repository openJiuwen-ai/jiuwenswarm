"""Native benchmark execution, with real model clients and measured events."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from openjiuwen.agent_teams.harness.native_harness import NativeHarness
from openjiuwen.agent_teams.harness.state import HarnessState
from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.factory import DeepAgentParts
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

from jiuwenswarm.agents.harness.team.duplex_native import DuplexNativeHarness
from jiuwenswarm.agents.harness.team.duplex_shadow import RoutedInput, deliver_routed
from jiuwenswarm.common.duplex_router import InboundMessage
from jiuwenswarm.benchmarks.duplex_metrics import Events as Events


def request_fingerprint(args, kwargs, settings):
    """Hash SDK inputs and their API-shaped meaning without logging credentials.

    The raw hash includes SDK bookkeeping. The semantic hash uses the SDK's
    message/tool conversion (which drops BaseMessage metadata), then normalizes
    tool-call identities. Neither hash claims to capture the final HTTP body.
    """
    from openjiuwen.core.foundation.llm.model_clients.base_model_client import BaseModelClient

    def serializable(value):
        if hasattr(value, "model_dump"):
            return serializable(value.model_dump(mode="json", exclude_none=True))
        if isinstance(value, dict):
            return {key: serializable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [serializable(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise TypeError(f"Unsupported request value: {type(value).__name__}")

    payload = serializable({"args": args, "kwargs": {k: v for k, v in kwargs.items()
        if k in ("messages", "tools", "temperature", "top_p", "max_tokens", "model",
                 "stop", "seed", "tool_choice", "response_format", "extra_body")}, "settings": settings})
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":")).encode()).hexdigest()
    exact = digest(payload)
    semantic_kwargs = dict(kwargs)
    semantic_args = list(args)
    if semantic_args:
        semantic_kwargs["messages"] = semantic_args.pop(0)
    if "messages" in semantic_kwargs:
        semantic_kwargs["messages"] = BaseModelClient._convert_messages_to_dict(semantic_kwargs["messages"])
    if "tools" in semantic_kwargs:
        semantic_kwargs["tools"] = BaseModelClient._convert_tools_to_dict(semantic_kwargs["tools"])
    semantic_payload = serializable({"args": semantic_args, "kwargs": {
        k: v for k, v in semantic_kwargs.items() if k in payload["kwargs"] or k == "messages"},
        "settings": settings})
    identities = {}
    def collect(value):
        if isinstance(value, dict):
            for call in value.get("tool_calls", []) or []:
                if isinstance(call, dict) and call.get("id"):
                    identities.setdefault(call["id"], f"call-{len(identities)}")
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    def normalize(value):
        if isinstance(value, dict):
            return {key: identities.get(item, item) if key in ("id", "tool_call_id") and isinstance(item, str)
                    else normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value
    collect(semantic_payload)
    return {"request_sha256": exact, "semantic_request_sha256": digest(normalize(semantic_payload))}


class MeasuredModel(Model):
    """Count calls including cancellations; missing provider usage stays null."""
    def __init__(self, config, events, *, lane, prompt=None, member=None, max_calls=None):
        super().__init__(model_client_config=config.model_client_config,
                         model_config=config.model_request_config)
        self.events = events
        self.lane = lane
        self.prompt = prompt
        self.member, self.max_calls, self.calls = member, max_calls, 0
        config_values = config.model_request_config.model_dump(mode="json", exclude_none=True)
        self.request_settings = {k: v for k, v in config_values.items() if k in (
            "model_name", "model", "temperature", "top_p", "max_tokens", "stop", "seed", "extra_body")}
        self.entered = asyncio.Event()

    def _arguments(self, kwargs):
        if self.prompt is not None:
            kwargs["messages"] = self.prompt()
            kwargs["tools"] = None  # upstream browser prompt/action protocol stays unchanged
        return kwargs

    async def stream(self, *args, **kwargs):
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise RuntimeError("Benchmark model call budget exhausted")
        self.calls += 1
        call_id = uuid.uuid4().hex
        started = time.monotonic()
        usage, status, chars = None, "complete", 0
        output_text = []
        arguments = self._arguments(kwargs)
        try:
            fingerprint = request_fingerprint(args, arguments, self.request_settings)
        except (TypeError, ValueError):
            fingerprint = {"request_fingerprint_unavailable": True}
        first_chunk_seconds = first_output_seconds = None
        self.events.add("model_start", lane=self.lane, member=self.member, call_id=call_id, **fingerprint)
        self.entered.set()
        try:
            async for chunk in super().stream(*args, **arguments):
                if first_chunk_seconds is None:
                    first_chunk_seconds = time.monotonic() - started
                value = getattr(chunk, "usage_metadata", None)
                if value:
                    usage = value.model_dump() if hasattr(value, "model_dump") else value
                content = getattr(chunk, "content", "")
                if first_output_seconds is None and (content or getattr(chunk, "tool_calls", None)):
                    first_output_seconds = time.monotonic() - started
                if isinstance(content, str):
                    chars += len(content)
                    output_text.append(content)
                yield chunk
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception:
            status = "error"
            raise
        finally:
            self.events.add("model_end", lane=self.lane, member=self.member, call_id=call_id, status=status,
                            seconds=time.monotonic() - started, usage=usage, output_chars=chars,
                            response_text="".join(output_text), first_chunk_seconds=first_chunk_seconds,
                            first_output_seconds=first_output_seconds)


def load_models(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return {name: TeamModelConfig.model_validate(data[name]) for name in ("slow", "fast")}


class NativePeer:
    """A persistent native worker; Runner's lifetime belongs to the entry point."""
    def __init__(self, *, name, models, policy, system_prompt, tools, events, prompt=None,
                 max_iterations=1000, goal=None, max_model_calls=None):
        if policy not in ("serial", "steer", "abort_restart", "model", "always_interrupt"):
            raise ValueError("unsupported benchmark policy")
        self.name, self.policy, self.events = name, policy, events
        self.models = models
        self.goal = goal
        self.blueprint = SimpleNamespace(member_name=name)
        self.tiny_agent_model_resolver = lambda name: models["fast"] if name == "fast" else None
        self.model = MeasuredModel(models["slow"], events, lane="slow", prompt=prompt,
                                   member=name, max_calls=max_model_calls)
        self._system_prompt, self._tools = system_prompt, tools
        self._max_iterations = max_iterations
        self._collector = None
        self._last_result = None
        self._failure = None
        self._inputs = []
        self._lock = asyncio.Lock()
        self._received = set()
        self.harness = self._build()

    def _build(self):
        peer = self

        class Spec:
            def resolve_parts(self, context=None):
                return DeepAgentParts(config=DeepAgentConfig(
                    card=AgentCard(id=uuid.uuid4().hex, name=peer.name), model=peer.model,
                    system_prompt=peer._system_prompt, enable_task_loop=True,
                    max_iterations=peer._max_iterations), rails=[],
                    tool_cards=[tool.card for tool in peer._tools], tool_instances=peer._tools)

        cls = DuplexNativeHarness if self.policy in ("model", "always_interrupt") else NativeHarness
        harness = cls(Spec())
        if self.model.prompt is not None:
            harness._duplex_context_provider = self.model.prompt
        if self.goal is not None:
            harness._duplex_goal_provider = self.goal
        return harness

    async def start(self):
        await self.harness.subscribe(on_round=self._on_round)
        await self.harness.start()
        self._collector = asyncio.create_task(self._drain())

    async def _drain(self):
        async for _ in self.harness.outputs():
            pass

    async def _on_round(self, kind, round_id, result=None):
        self.events.add("round", member=self.name, kind=kind, round_id=round_id, result=result)
        if kind == "finished":
            self._last_result = result
        elif kind == "failed":
            self._failure = result or {"error": "native round failed"}

    async def send(self, query):
        self._inputs.append(str(query))
        self._last_result, self._failure = None, None
        await self.harness.send(query)

    @staticmethod
    async def _original(host, content, *, use_steer):
        return await host.harness.send(content, immediate=use_steer)

    def record_duplex_input(self, snapshot, messages):
        self.events.add("route_input", member=self.name, goal=snapshot.goal,
                        next_action=snapshot.next_action, last_action=snapshot.last_action, phase=snapshot.phase,
                        message_ids=[message.message_id for message in messages])

    def record_duplex_observation(self, observation):
        self.events.add("route_decision", member=self.name, attempts=observation.attempts,
                        message_ids=observation.message_ids,
                        latency_ms=observation.latency_ms, status=observation.status,
                        action=observation.proposed_action, usage=None)

    async def receive(self, content, *, message_id, sender="peer"):
        async with self._lock if self.policy == "abort_restart" else nullcontext():
            if message_id in self._received:
                return
            self.events.add("message_arrived", member=self.name, message_id=message_id)
            self._inputs.append(content)
            if self.policy == "abort_restart":
                # Deliberately naive comparison: cancel and rebuild the entire
                # execution context, retaining inputs only. Do not label this
                # safe checkpoint recovery; external effects cannot be undone.
                await self.harness.abort(immediate=True)
                await self.harness.stop()
                await self._collector
                self.harness = self._build()
                await self.start()
                await self.harness.send("\n\n".join(self._inputs))
            else:
                routed = RoutedInput(content, InboundMessage(message_id, sender, content))
                if self.policy in ("model", "always_interrupt"):
                    await deliver_routed(self, routed, use_steer=True, original=self._original,
                        settings={"mode": "active", "policy": self.policy, "model_name": "fast"})
                else:
                    await self.harness.send(content, immediate=self.policy != "serial")
            self._received.add(message_id)
            self.events.add("message_accepted", member=self.name, message_id=message_id)

    async def wait(self, *, timeout=7200):
        async with asyncio.timeout(timeout):
            while True:
                await asyncio.sleep(0.01)
                if self._failure is not None:
                    raise RuntimeError(f"native benchmark worker failed: {self._failure}")
                if self.harness.state is HarnessState.IDLE and self._last_result is not None:
                    return self._last_result

    async def close(self):
        await self.harness.stop()
        if self._collector is not None:
            await self._collector


class UserInputPeer(NativePeer):
    """Use the production USER_INPUT handler and TeamAgent delivery boundary."""

    async def start(self):
        from jiuwenswarm.agents.harness.team.duplex_shadow import install_shadow_observer
        install_shadow_observer()
        self.duplex_settings = {"mode": "active", "policy": self.policy,
                                "model_name": "fast"}
        await super().start()

    async def deliver_input(self, content, *, use_steer=True):
        if self.policy == "abort_restart":
            message = content.message
            return await super().receive(str(content), message_id=message.message_id, sender="user")
        from openjiuwen.agent_teams.agent.team_agent import TeamAgent
        return await TeamAgent.deliver_input(self, content, use_steer=self.policy != "serial")

    async def receive_user(self, content, *, message_id):
        from openjiuwen.agent_teams.agent.coordination.event_bus import InnerEventMessage, InnerEventType
        from openjiuwen.agent_teams.agent.coordination.handlers.agent_lifecycle import AgentLifecycleHandler
        handler = AgentLifecycleHandler(self, self.blueprint, None, None)
        self.events.add("message_arrived", member=self.name, message_id=message_id)
        self.events.add("user_input_entry", member=self.name, message_id=message_id,
                        handler="AgentLifecycleHandler.on_user_input")
        await handler.on_user_input(InnerEventMessage(event_type=InnerEventType.USER_INPUT,
            payload={"content": content, "message_id": message_id}))
        self._received.add(message_id)
        self.events.add("message_accepted", member=self.name, message_id=message_id)
