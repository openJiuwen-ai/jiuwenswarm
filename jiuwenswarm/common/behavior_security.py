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
from typing import Any, NamedTuple

import httpx

from openjiuwen.harness.security import PermissionLevel, PermissionResult, SkillInstallContext, before_skill_install
from openjiuwen.harness.security.permission_engine.toolguard.tool_policy import evaluate_tiered_policy
from jiuwenswarm.agents.harness.common.channel_runtime_context import CURRENT_SESSION_ID
from jiuwenswarm.common.np_transport import named_pipe_transport_for
from jiuwenswarm.common.permission_profile import (
    PERMISSION_PROFILE_DEFAULT,
    PERMISSION_PROFILE_FULL_ACCESS,
    current_permission_profile,
    resolve_permission_profile_rules,
)
from jiuwenswarm.common.utils import logger

ACTIVE_BEHAVIOR: contextvars.ContextVar[tuple | None] = contextvars.ContextVar("active_behavior", default=None)
PENDING_SKILL = "behavior_security.pending_skill"
# toolOutput CLARIFY 待确认标记（session state）：key = f"{PENDING_OUTPUT}:{tool_call_id}"，
# value = 行为事件 eventId；resume 时凭用户答复从 bridge.output_stash 取回/屏蔽输出
PENDING_OUTPUT = "behavior_security.pending_output"
_OUTPUT_CHUNK_CHARS = 5500
_OUTPUT_CHUNK_OVERLAP = 256
_MAX_OUTPUT_INSPECTION_CHARS = 64 * 1024

# 风控三态 UI（2026-10-18，设计 claw_desktop docs/risk-notice-ui-design-2026-10-18.md）：
# 安全提示内置工具名——该工具不上报不送审（防递归），权限评估本地直接放行。
SECURITY_NOTICE_TOOL = "security_notice"


# ---------------------------------------------------------------------------
# 三态 UI 指令模板：deny/clarify 时给模型的引导文案，经 agent-core
# reject(tool_result=...) / tool_result 替换原样进入模型视野——横幅本体由桌面宿主
# 在裁决当下直发（risk-notice 事件），此处只引导模型调 security_notice 补写
# 第二行融合话术（notice_id=行为事件 eventId，与桌面登记簿同键）并完成后续动作。
#
# 关键纪律（2026-10-18 二次迭代）：指令里【只带风险类别 labels，绝不带云端 remind
# 原文】——remind 可能含命中特征词（如恶意签名），进入模型上下文后会被桌面 answer
# 滑窗二次送审并自我命中 REJECT，把整轮误清屏（用户看到「失败 + 风控原文」）。
# 横幅第二行初值=remind 由宿主直发（不经模型、不再送审），模型仅按类别融合话术。
# ---------------------------------------------------------------------------

def _risk_category(labels: Any) -> str:
    cats = [str(x).strip() for x in labels or [] if str(x or "").strip()]
    return "、".join(cats) if cats else "安全风险"


def tool_input_reject_instruction(tool_name: str, category: str, notice_id: str) -> str:
    return (
        f"[安全风控] 工具 {tool_name} 的输入未通过安全审核，本次调用已被拦截，工具未执行。\n"
        f"风险类别：{category}（审核详情已通过界面安全提示直接展示给用户，你看不到也无需追问）。\n"
        f"请调用 security_notice 工具向用户补写安全提示（notice_id=\"{notice_id}\"，message 用一句"
        "自然语言向用户说明安全审计结果与建议；不要带「安全审计结果：」前缀）；然后用一段话向用户"
        "说明情况并调整后续计划。不要重试相同或相似的调用。"
    )


def skill_install_reject_instruction(name: str, category: str, notice_id: str) -> str:
    return (
        f"[安全风控] 技能「{name}」未通过安全审核，已拒绝安装。\n"
        f"风险类别：{category}（审核详情已通过界面安全提示直接展示给用户，你看不到也无需追问）。\n"
        f"请调用 security_notice 工具向用户补写安全提示（notice_id=\"{notice_id}\"，message 要求同上）；"
        "然后向用户说明情况。不要再尝试安装该技能或内容相同的技能包。"
    )


def tool_output_reject_instruction(tool_name: str, category: str, notice_id: str) -> str:
    return (
        f"[安全风控] 工具 {tool_name} 的输出未通过安全审核，已被屏蔽（内容不会提供给你）。\n"
        f"风险类别：{category}（审核详情已通过界面安全提示直接展示给用户，你看不到也无需追问）。\n"
        f"请调用 security_notice 工具向用户补写安全提示（notice_id=\"{notice_id}\"，message 向用户"
        "说明「该工具结果存在安全风险、已屏蔽」及你的应对，要求同上）；然后向用户说明情况并基于"
        "已有信息重新规划方案。不要重试该工具，也不要尝试以其他方式获取同内容。"
    )


def tool_output_clarify_confirm_message(tool_name: str, category: str) -> str:
    """toolOutput CLARIFY 用户确认卡话术（用户可见；只带类别不带 remind 原文）。

    末尾 call_goal 行供宿主审批卡提取标题（approval extractCallGoal 契约），
    让标题读作「确认是否采纳疑似风险输出」而非工具命令本身。
    """
    return (
        f"安全确认：工具 {tool_name} 的输出经安全审核为疑似风险（{category}）。\n"
        "采纳该结果将继续基于它完成任务；拒绝则屏蔽该结果并重新规划。\n"
        'call_goal: "确认是否采纳该疑似风险输出"'
    )


def tool_output_clarify_approved_note() -> str:
    """用户确认采纳后拼回工具输出的尾注（模型可见，解释刚才的确认停顿）。"""
    return "\n\n[安全风控] 该输出经安全审核为疑似风险，已由用户确认采纳。"


def tool_output_clarify_rejected_instruction(tool_name: str, category: str) -> str:
    """用户拒绝采纳疑似风险输出后的模型可见指令（原输出不进上下文）。"""
    return (
        f"[安全风控] 工具 {tool_name} 的输出经安全审核为疑似风险（{category}），"
        "用户已拒绝采纳，内容已被屏蔽（不会提供给你）。\n"
        "请向用户说明情况并基于已有信息重新规划方案。不要重试该工具，"
        "也不要尝试以其他方式获取同内容。"
    )


class OutputStashEntry(NamedTuple):
    """toolOutput CLARIFY 暂存：待用户确认的工具输出（确认前不进模型上下文）。"""

    tool_call_id: str
    tool_name: str
    category: str
    tool_result: Any
    tool_msg: Any


class OutputInspection(NamedTuple):
    """tool.after 送审结论（三态）：deny 屏蔽 / clarify 放行+确认指令 / allow 放行。"""

    result: PermissionResult
    verdict: str  # "deny" | "clarify" | "allow"
    reason: str   # deny/clarify 时的云端 remind（仅日志用，不进模型上下文）
    labels: tuple  # 云端风险类别标签（指令模板用，替代 remind 原文）


def desktop_security_active() -> bool:
    # Explicit host capability. Other hosts, including old desktops, keep their behavior.
    return os.environ.get("CLAW_BEHAVIOR_SECURITY") == "1" and bool(os.environ.get("CLAW_SKILL_TOKEN"))


def cloud_authorization_enabled(config: dict) -> bool:
    """Only an explicit YAML boolean enables cloud decisions."""
    return config.get("xiaoyi_work_security", {}).get("cloud_authorization", {}).get("enabled") is True


def _effective_permission_profile(config: dict) -> str:
    profile = current_permission_profile()
    if profile is not None:
        return profile
    permissions = config.get("permissions", {})
    if isinstance(permissions, dict) and permissions.get("enabled") is False:
        return PERMISSION_PROFILE_FULL_ACCESS
    return PERMISSION_PROFILE_DEFAULT


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
        # Let the host resolve unmatched tools through one fallback policy,
        # whether cloud authorization is disabled or temporarily unavailable.
        permissions["defer_unmatched"] = True
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
        # toolOutput CLARIFY 暂存（eventId → 输出快照）：用户确认前输出不进模型上下文，
        # resume 批准后原样拼回（工具不重执行）；拒绝则替换为屏蔽指令
        self.output_stash: OrderedDict[str, OutputStashEntry] = OrderedDict()

    def stash_output(self, event_id: str, entry: OutputStashEntry) -> None:
        self.output_stash[event_id] = entry
        if len(self.output_stash) > 128:
            self.output_stash.popitem(last=False)

    def pop_output_stash(self, event_id: str) -> OutputStashEntry | None:
        return self.output_stash.pop(event_id, None)

    def policy_revision(self) -> str:
        permissions = self.config.get("permissions", {})
        composition = permissions.get("composition", {}) if isinstance(permissions, dict) else {}
        identity = (
            composition.get("revision") if isinstance(composition, dict) else None,
            cloud_authorization_enabled(self.config),
            self.config.get("security", {}).get("fail_closed", {}).get("enabled") is True,
            _effective_permission_profile(self.config),
        )
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def update_config(self, config: dict) -> None:
        self.config = config
        self.decisions.clear()
        self.local_decisions.clear()

    def _unmatched_tool_fallback(self, *, cloud_unavailable: bool) -> PermissionResult:
        fail_closed = self.config.get("security", {}).get("fail_closed", {}).get("enabled") is True
        if fail_closed and _effective_permission_profile(self.config) != PERMISSION_PROFILE_FULL_ACCESS:
            reason = "云端不可用，请授权本次操作" if cloud_unavailable else "本地规则未命中，请授权本次操作"
            return PermissionResult(PermissionLevel.ASK, reason=reason)
        reason = "cloud:unavailable" if cloud_unavailable else "cloud:disabled"
        return PermissionResult(PermissionLevel.ALLOW, reason=reason)

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

    async def inspect(self, event: dict) -> OutputInspection:
        """Synchronously decide whether a tool result may be exposed to the model.

        deny：只有明确云端 REJECT 屏蔽整个输出（工具结果替换为屏蔽指令）；
        clarify：输出暂存待用户确认卡定夺（三态 UI 2026-10-18 二次迭代：
        确定性确认卡替代模型自觉 ask_user，批准原样拼回、拒绝屏蔽）；
        超时/传输错误/未确认结论/超大载荷 fail-open 放行原样。
        """
        events = self._output_inspection_events(event)
        if events is None:
            logger.warning("[behavior-security] tool.after inspect skipped: output exceeds limit")
            return OutputInspection(PermissionResult(PermissionLevel.ALLOW, reason="cloud:allow"), "allow", "", ())
        replies = await asyncio.gather(*(
            self._send({**item, "mode": "inspect"}) for item in events
        ))
        clarified = False
        clarify_reason = ""
        clarify_labels: tuple = ()
        for reply in replies:
            if reply.get("decision") == "deny":
                reason = str(reply.get("reason") or "工具输出安全检测未通过")
                labels = tuple(reply.get("labels") or ())
                return OutputInspection(
                    PermissionResult(PermissionLevel.DENY, reason=reason), "deny", reason, labels,
                )
            if reply.get("decision") == "clarify" and not clarified:
                clarified = True
                clarify_reason = str(reply.get("reason") or "").strip()
                clarify_labels = tuple(reply.get("labels") or ())
        if clarified:
            return OutputInspection(
                PermissionResult(PermissionLevel.ALLOW, reason="cloud:clarify"), "clarify",
                clarify_reason, clarify_labels,
            )
        return OutputInspection(PermissionResult(PermissionLevel.ALLOW, reason="cloud:allow"), "allow", "", ())

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
            if event.get("stage") == "skill.before_install":
                # Skill installation retains its separate confirmation policy.
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
                result = self._unmatched_tool_fallback(cloud_unavailable=False)
        else:
            reply = await self._send({**event, "mode": "authorize"})
            decision = reply.get("decision")
            if decision == "allow":
                result = PermissionResult(PermissionLevel.ALLOW, reason="cloud:allow")
            elif decision == "clarify":
                # 三态 UI（2026-10-18）：toolInput CLARIFY 转用户审批卡确认（ASK 打断点）。
                # 授权记忆由权限系统承担——resume 批准后本地已 ALLOW，下次评估走
                # 本地优先分支，不会重入云端 clarify，无环路。
                remind = str(reply.get("reason") or "").strip() or "该操作存在安全风险，请确认是否继续"
                result = PermissionResult(PermissionLevel.ASK, reason=remind)
            elif decision == "deny":
                # reason = 给模型的指令模板（经 reject(tool_result=...) 原样送达）：
                # 只带风险类别 labels、绝不带 remind 原文（防 answer 滑窗二次命中清屏），
                # 引导调 security_notice 补写横幅第二行（notice_id=eventId 与桌面登记簿同键）
                result = PermissionResult(
                    PermissionLevel.DENY,
                    reason=tool_input_reject_instruction(
                        str(event["tool"].get("name") or ""),
                        _risk_category(reply.get("labels")),
                        event["eventId"]),
                )
            else:
                if event.get("stage") == "skill.before_install":
                    # Installation still asks; tool authorize fail-opens like inspect.
                    result = PermissionResult(PermissionLevel.ASK, reason="云端不可用，请授权本次操作")
                else:
                    result = self._unmatched_tool_fallback(cloud_unavailable=True)
        if revision != self.policy_revision():
            return PermissionResult(PermissionLevel.DENY, reason="安全策略已变更，请重新发起操作")
        self.decisions[key] = result
        if len(self.decisions) > 512:
            self.decisions.popitem(last=False)
        return result

    async def decide_authoritative(self, event: dict) -> PermissionResult:
        """Cloud-authoritative decision: the local verdict is deliberately NOT consulted.

        Skill installation is a low-frequency, high-risk action: when cloud
        authorization is enabled, a cloud REJECT must block the install even when
        the initiating tool call was locally allowed or confirmed by the user
        (decide() would otherwise degrade the cloud verdict to a report nobody
        consumes). Used by check_agent_skill_install.
        """
        key = event["eventId"]
        if key in self.decisions:
            return self.decisions[key]
        reply = await self._send({**event, "mode": "authorize"})
        decision = reply.get("decision")
        if decision == "allow":
            result = PermissionResult(PermissionLevel.ALLOW, reason="cloud:allow")
        elif decision == "clarify":
            # 三态 UI（2026-10-18）：skill 安装 CLARIFY 复用 ASK 打断点（install_confirmation
            # 状态机 + 指纹复核现成），审批卡话术带云端 remind；用户确认后继续安装。
            remind = str(reply.get("reason") or "").strip()
            result = PermissionResult(
                PermissionLevel.ASK,
                reason=remind or "云端提示该技能存在安全风险，请确认是否继续安装",
            )
        elif decision == "deny":
            skill_name = str((event.get("skill") or {}).get("name") or "")
            result = PermissionResult(
                PermissionLevel.DENY,
                reason=skill_install_reject_instruction(
                    skill_name, _risk_category(reply.get("labels")), event["eventId"]),
            )
        else:
            # 云端不可用：默认转用户确认（复用闸口的 ASK 打断点），
            # skill_install_fail_open: true 时才静默放行
            section = self.config.get("xiaoyi_work_security", {}).get("cloud_authorization", {})
            if bool(section.get("skill_install_fail_open", False)):
                result = PermissionResult(PermissionLevel.ALLOW, reason="cloud:unavailable-fail-open")
            else:
                result = PermissionResult(PermissionLevel.ASK, reason="云端不可用，请确认是否安装")
        self.decisions[key] = result
        if len(self.decisions) > 512:
            self.decisions.popitem(last=False)
        return result

    def output_force_finish_enabled(self) -> bool:
        """toolOutput REJECT 后是否强制收尾（2026-10-18 三态 UI 起默认 False：
        屏蔽输出后让模型重新规划；true 回退旧的 request_force_finish 收尾语义）。"""
        section = self.config.get("xiaoyi_work_security", {}).get("cloud_authorization", {})
        return bool(section.get("tool_output_force_finish", False))

    async def on_permission_evaluated(self, request):
        ctx = request.ctx
        ctx.extra["behavior.local_permission"] = request.result
        ctx.extra["behavior.input_observed"] = True
        # 安全提示工具：不上报不送审、本地直接放行（防递归；其价值即通知宿主，
        # 阻断/审批它没有意义，反而会让「补写安全横幅」本身弹审批）
        if str(getattr(ctx.inputs, "tool_name", "") or "") == SECURITY_NOTICE_TOOL:
            return PermissionResult(PermissionLevel.ALLOW, reason="builtin_security_notice")
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


async def check_agent_skill_install(staged: Path, destination: Path, name: str, source: str,
                                    download_url: str | None = None) -> None:
    """Barrier immediately before mutation. Non-Agent installation is outside this phase.

    download_url：来源市场的包下载地址（clawhub/teamskillshub/skillnet），透传进事件
    skill.downloadUrl——桌面端存在时覆盖送审 file.url，云端可回拉全量包审核（不止
    hash+SKILL.md 文本）；无地址来源（builtin）保持缺省，走 hash+SKILL.md 降级形态。
    """
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
    if download_url:
        event["skill"]["downloadUrl"] = download_url
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
        # 技能安装是低频高危动作：云端启用时不看本地裁决，一律权威送审——
        # 否则发起工具本地已裁决（ALLOW / 用户确认后 ASK→ALLOW）时云端 REJECT
        # 只落得一条 report，无人消费（decide() 本地优先遮蔽）。
        if cloud_authorization_enabled(bridge.config):
            return await bridge.decide_authoritative(event)
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


def check_agent_skill_install_sync(staged: Path, destination: Path, name: str, source: str,
                                   download_url: str | None = None) -> None:
    """Worker-thread adapter; contextvars are propagated by asyncio.to_thread."""
    active = ACTIVE_BEHAVIOR.get()
    if active is not None:
        future = asyncio.run_coroutine_threadsafe(
            check_agent_skill_install(staged, destination, name, source, download_url), active[2])
        try:
            future.result(timeout=35)
        except BaseException:
            future.cancel()
            raise
