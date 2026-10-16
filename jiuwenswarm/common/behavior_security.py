"""Desktop behavior bridge: raw events only; cloud contracts live in claw_desktop."""
from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import json
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx

from openjiuwen.harness.security import PermissionLevel, PermissionResult, SkillInstallContext, before_skill_install
from openjiuwen.harness.security.permission_engine.toolguard.tool_policy import evaluate_tiered_policy
from jiuwenswarm.agents.harness.common.channel_runtime_context import CURRENT_SESSION_ID
from jiuwenswarm.common.np_transport import named_pipe_transport_for
from jiuwenswarm.common.permission_profile import resolve_permission_profile_rules
from jiuwenswarm.common.utils import logger

ACTIVE_BEHAVIOR: contextvars.ContextVar[tuple | None] = contextvars.ContextVar("active_behavior", default=None)
PENDING_SKILL = "behavior_security.pending_skill"
_OUTPUT_CHUNK_CHARS = 5500
_OUTPUT_CHUNK_OVERLAP = 256
_MAX_OUTPUT_INSPECTION_CHARS = 64 * 1024


def desktop_security_active() -> bool:
    # Explicit host capability. Other hosts, including old desktops, keep their behavior.
    return os.environ.get("CLAW_BEHAVIOR_SECURITY") == "1" and bool(os.environ.get("CLAW_SKILL_TOKEN"))


def cloud_authorization_enabled(config: dict) -> bool:
    """Only an explicit YAML boolean enables cloud decisions."""
    return config.get("xiaoyi_work_security", {}).get("cloud_authorization", {}).get("enabled") is True


def runtime_permission_config(config: dict) -> dict:
    """Keep desktop policy checks active even in the full-access UI mode.

    Only the runtime copy is changed; the stored permission mode and other
    hosts retain their existing semantics.
    """
    permissions = resolve_permission_profile_rules(config.get("permissions", {}))
    # Host-owned policy: persisted settings and session overlays cannot opt in.
    permissions.pop("strict_tool_command_merge", None)
    if desktop_security_active():
        permissions["enabled"] = True
        cloud_enabled = cloud_authorization_enabled(config)
        permissions["defer_unmatched"] = cloud_enabled
        permissions["strict_tool_command_merge"] = cloud_enabled
    return permissions


def _session_id(ctx: Any) -> str:
    session = getattr(ctx, "session", None)
    getter = getattr(session, "get_session_id", None)
    return str(CURRENT_SESSION_ID.get() or (getter() if callable(getter) else getattr(session, "session_id", "")) or "")


def _event_id(ctx: Any, stage: str, suffix: str = "") -> str:
    call = ctx.inputs.tool_call
    turn = ctx.extra.get("behavior.turn_id", "")
    args = json.dumps(ctx.inputs.tool_args, sort_keys=True, ensure_ascii=False, default=str)
    revision = ctx.extra.get("behavior.policy_revision", "")
    raw = f"{revision}:{_session_id(ctx)}:{turn}:{getattr(call, 'id', '')}:{ctx.inputs.tool_name}:{args}:{stage}:{suffix}"
    return hashlib.sha256(raw.encode()).hexdigest()


def raw_event(ctx: Any, stage: str, suffix: str = "") -> dict:
    call = ctx.inputs.tool_call
    tool = {"id": str(getattr(call, "id", "")), "name": ctx.inputs.tool_name,
            "arguments": copy.deepcopy(ctx.inputs.tool_args)}
    if stage == "tool.after":
        tool["result"] = copy.deepcopy(ctx.inputs.tool_result)
        if ctx.exception:
            tool["error"] = type(ctx.exception).__name__
    event = {"version": 1, "eventId": _event_id(ctx, stage, suffix), "stage": stage,
             "sessionId": _session_id(ctx), "tool": tool}
    if "behavior.dialogue_context" in ctx.extra:
        event["dialogueContext"] = copy.deepcopy(ctx.extra["behavior.dialogue_context"])
    # Pydantic tool results must be normalized before scheduling, while the
    # original context and data are still alive. No cloud body is built here.
    return json.loads(json.dumps(event, ensure_ascii=False, default=lambda value:
        value.model_dump(mode="json") if hasattr(value, "model_dump") else str(value)))


class BehaviorSecurityBridge:
    """Bounded reporting tasks and one decision per behavior/checkpoint."""
    def __init__(self, config: dict, *, transport=None):
        self.config = config
        self.transport = transport
        self.tasks: set[asyncio.Task] = set()
        self.decisions: OrderedDict[str, PermissionResult] = OrderedDict()
        self.local_decisions: OrderedDict[str, PermissionResult] = OrderedDict()

    def policy_revision(self) -> str:
        permissions = self.config.get("permissions", {})
        composition = permissions.get("composition", {}) if isinstance(permissions, dict) else {}
        identity = (
            composition.get("revision") if isinstance(composition, dict) else None,
            cloud_authorization_enabled(self.config),
            self.config.get("security", {}).get("fail_closed", {}).get("enabled") is True,
        )
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def update_config(self, config: dict) -> None:
        self.config = config
        self.decisions.clear()
        self.local_decisions.clear()

    async def _send(self, event: dict) -> dict:
        section = self.config.get("xiaoyi_work_security", {}).get("cloud_authorization", {})
        timeout = max(0.1, min(float(section.get("timeout_ms", 4000)) / 1000, 30))
        try:
            transport = self.transport or named_pipe_transport_for("np://claw-skill")
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
                    response = await client.post("http://claw-skill/internal/behavior-security/v1",
                        headers={"Authorization": f"Bearer {os.environ.get('CLAW_SKILL_TOKEN', '')}"},
                        json=event)
                    response.raise_for_status()
                    body = response.json()
                    if not isinstance(body, dict):
                        return {"decision": "unavailable", "reason": "invalid_response"}
                    return body
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            logger.warning("[behavior-security] %s %s timeout", event["stage"], event["mode"])
            return {"decision": "unavailable", "reason": "timeout"}
        except Exception:
            logger.warning("[behavior-security] %s %s unavailable", event["stage"], event["mode"])
            return {"decision": "unavailable", "reason": "request_failed"}

    def report(self, event: dict) -> None:
        if len(self.tasks) >= 64:
            logger.warning("[behavior-security] local report queue full")
            return
        task = asyncio.create_task(self._send({**event, "mode": "report"}))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    @staticmethod
    def _output_inspection_events(event: dict) -> list[dict] | None:
        """Bound output requests without dropping an unscanned middle section."""
        tool = event.get("tool")
        result = tool.get("result") if isinstance(tool, dict) else None
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(text) <= _OUTPUT_CHUNK_CHARS:
            return [event]
        if len(text) > _MAX_OUTPUT_INSPECTION_CHARS:
            return None
        step = _OUTPUT_CHUNK_CHARS - _OUTPUT_CHUNK_OVERLAP
        chunks = [text[start:start + _OUTPUT_CHUNK_CHARS] for start in range(0, len(text), step)]
        output = []
        for index, chunk in enumerate(chunks):
            item = copy.deepcopy(event)
            item["eventId"] = hashlib.sha256(
                f"{event['eventId']}:{index}:{len(chunks)}".encode(),
            ).hexdigest()
            item["tool"]["arguments"] = {}
            item["tool"]["result"] = {
                "chunkIndex": index,
                "chunkCount": len(chunks),
                "content": chunk,
            }
            output.append(item)
        return output

    async def inspect(self, event: dict) -> PermissionResult:
        """Synchronously decide whether a tool result may be exposed to the model.

        Only an explicit cloud REJECT blocks the whole output. Timeout, transport
        errors, unconfirmed results and oversized payloads fail-open so the
        original payload continues.
        """
        events = self._output_inspection_events(event)
        if events is None:
            logger.warning("[behavior-security] tool.after inspect skipped: output exceeds limit")
            return PermissionResult(PermissionLevel.ALLOW, reason="cloud:allow")
        replies = await asyncio.gather(*(
            self._send({**item, "mode": "inspect"}) for item in events
        ))
        for reply in replies:
            if reply.get("decision") == "deny":
                return PermissionResult(
                    PermissionLevel.DENY,
                    reason=str(reply.get("reason") or "工具输出安全检测未通过"),
                )
        return PermissionResult(PermissionLevel.ALLOW, reason="cloud:allow")

    async def decide(self, event: dict, local: PermissionResult) -> PermissionResult:
        revision = self.policy_revision()
        key = event["eventId"]
        if key in self.decisions:
            return self.decisions[key]
        cloud_enabled = cloud_authorization_enabled(self.config)
        if not local.is_undetermined:
            self.report(event)
            result = local
        elif not cloud_enabled:
            self.report(event)
            # Normal tool checks already use Core defaults when cloud is off.
            # Installation hooks may still arrive without a prior local result.
            tool = event["tool"]
            args = tool.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            permissions = {**self.config.get("permissions", {}), "defer_unmatched": False}
            level, matched_rule = evaluate_tiered_policy(
                permissions, tool["name"], args if isinstance(args, dict) else {},
            )
            result = PermissionResult(level, matched_rule=matched_rule)
        else:
            reply = await self._send({**event, "mode": "authorize"})
            decision = reply.get("decision")
            if decision in {"allow", "clarify"}:
                # Keep compatibility with older desktops returning "clarify";
                # current desktops normalize all non-REJECT results to "allow".
                result = PermissionResult(PermissionLevel.ALLOW, reason="cloud:" + decision)
            elif decision == "deny":
                result = PermissionResult(PermissionLevel.DENY, reason=str(reply.get("reason") or "云端拒绝本次操作"))
            else:
                fail_closed = self.config.get("security", {}).get("fail_closed", {}).get("enabled") is True
                if fail_closed:
                    result = PermissionResult(PermissionLevel.DENY, reason="云端不可用，拒绝本次操作")
                elif event.get("stage") == "skill.before_install":
                    # Installation still asks; tool authorize fail-opens like inspect.
                    result = PermissionResult(PermissionLevel.ASK, reason="云端不可用，请授权本次操作")
                else:
                    result = PermissionResult(PermissionLevel.ALLOW, reason="cloud:unavailable")
        if revision != self.policy_revision():
            return PermissionResult(PermissionLevel.DENY, reason="安全策略已变更，请重新发起操作")
        self.decisions[key] = result
        if len(self.decisions) > 512:
            self.decisions.popitem(last=False)
        return result

    async def on_permission_evaluated(self, request):
        ctx = request.ctx
        ctx.extra["behavior.local_permission"] = request.result
        ctx.extra["behavior.input_observed"] = True
        self.local_decisions[_event_id(ctx, "tool.before")] = request.result
        if len(self.local_decisions) > 512:
            self.local_decisions.popitem(last=False)
        return await self.decide(raw_event(ctx, "tool.before"), request.result)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()


def skill_confirmation_pending(ctx: Any) -> bool:
    return bool(ctx.session and ctx.session.get_state(PENDING_SKILL + ":" + _event_id(ctx, "tool.before")))


async def check_agent_skill_install(staged: Path, destination: Path, name: str, source: str) -> None:
    """Barrier immediately before mutation. Non-Agent installation is outside this phase."""
    active = ACTIVE_BEHAVIOR.get()
    if active is None:
        return
    bridge, ctx, _loop = active
    if ctx.extra.get("behavior.finished"):
        raise PermissionError("安装调用已经取消或结束")
    # Internal snapshot identity only; NOT the still-undecided cloud file.hash.
    def fingerprint():
        digest = hashlib.sha256()
        files = sorted(staged.rglob("*")) if staged.is_dir() else [staged]
        for path in files:
            if path.is_file():
                digest.update(str(path.relative_to(staged) if staged.is_dir() else path.name).encode())
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
        return digest.hexdigest()
    snapshot = await asyncio.to_thread(fingerprint)
    event = raw_event(ctx, "skill.before_install", f"{destination}:{snapshot}")
    event["skill"] = {"name": name, "source": source, "stagedPath": str(staged), "destination": str(destination)}
    context = SkillInstallContext(event["eventId"], name, source, staged, destination)
    state_key = PENDING_SKILL + ":" + _event_id(ctx, "tool.before")
    pending = ctx.session.get_state(state_key) if ctx.session else None
    from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
    from openjiuwen.harness.rails.interrupt.interrupt_base import BaseInterruptRail
    helper = BaseInterruptRail()
    response = helper._get_user_input(ctx, ctx.inputs.tool_call.id)
    if pending == event["eventId"] and response is not None:
        answer = PermissionInterruptRail.parse_confirm_payload(response)
        if answer is not None:
            ctx.session.update_state({state_key: None})
            if answer.approved:
                return
            raise PermissionError("用户拒绝安装技能")

    async def evaluate(_context):
        local = ctx.extra.get("behavior.local_permission")
        if local is None:
            local = PermissionResult(PermissionLevel.UNDETERMINED)
        # Explicit ASK was already confirmed for this install tool invocation.
        if local.needs_approval:
            local = PermissionResult(PermissionLevel.ALLOW, reason="local_install_confirmation")
        return await bridge.decide(event, local)

    result = await before_skill_install(context, evaluate)
    if ctx.extra.get("behavior.finished"):
        raise PermissionError("安装调用已经取消或结束")
    if result.is_allowed:
        if await asyncio.to_thread(fingerprint) != snapshot:
            raise PermissionError("技能内容在授权期间发生变化，请重新安装")
        if ctx.extra.get("behavior.finished"):
            raise PermissionError("安装调用已经取消或结束")
        return
    if result.needs_approval:
        if ctx.session:
            ctx.session.update_state({state_key: event["eventId"]})
        ctx.extra["behavior.install_confirmation"] = f"权限确认：是否安装技能 {name}？{result.reason or ''}"
    raise PermissionError(result.reason or "技能安装未获授权")


def check_agent_skill_install_sync(staged: Path, destination: Path, name: str, source: str) -> None:
    """Worker-thread adapter; contextvars are propagated by asyncio.to_thread."""
    active = ACTIVE_BEHAVIOR.get()
    if active is not None:
        future = asyncio.run_coroutine_threadsafe(check_agent_skill_install(staged, destination, name, source), active[2])
        try:
            future.result(timeout=35)
        except BaseException:
            future.cancel()
            raise
