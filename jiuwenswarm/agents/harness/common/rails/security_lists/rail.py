# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""UnifiedSecurityListRail — 统一安全名单护栏（priority=95）。

座次：BehaviorSecurityRail(100) → 本 Rail(95) → PermissionInterruptRail(90)
→ CsplSentinelRail(78)。先于权限引擎执行，因此内置基线 deny 在各档位
（含 auto_approve/full_access）一致生效。

判定流程（before_tool_call）：
1. 复活分支：本 Rail 的确认应答经独立 resume 键
   （``SECURITY_LISTS_RESUME_USER_INPUT_KEY``，对齐 evolution rail 的隔离
   模式）取回，避免被 PermissionInterruptRail 吞掉或误吞它的应答；
2. 首检：extract_targets → 逐目标 evaluate → 任一 deny 直接拒绝
   （sentinel 同款 _skip_tool）→ 命中 ask 时**先向权限引擎预检一次裁决并
   归一化**（引擎 deny → 直接拒绝、不弹窗；引擎 ask → 合成一次弹窗；引擎
   不可用 → 退化为既有串行行为）→ 取最严 ask 发起 HITL 确认 →
   全 allow/None 交权限引擎管线；
3. 异常兜底（fail-closed）：名单损坏/IO 异常 → DENY + 审计 +
   桌面健康告警（behavior bridge 通道，best-effort）。

deny 时额外在 ``ctx.extra[SECURITY_LIST_DENY_KEY]`` 留标记：
PermissionInterruptRail(90) 仍会跑（_skip_tool 不中断 rail 链），
jiuwenswarm 侧 permission_scene_hook 见标记直接 reject，避免二次弹窗。

用户批准时在 ``ctx.extra[SECURITY_LIST_APPROVED_KEY]`` 留标记（值为 tool_call_id）：
scene hook 据此对本调用免二次弹窗，但仍先查一次引擎裁决——引擎 deny 不放行。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.interrupt.state import INTERRUPT_AUTO_CONFIRM_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    BaseInterruptRail,
    InterruptDecision,
)
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.common.permission_profile import current_permission_profile
from jiuwenswarm.common.utils import logger

from . import audit
from .composer import SecurityListComposer
from .evaluate import Verdict, evaluate
from .matcher import extract_targets
from .models import new_record_id, utc_now_iso

#: 本 Rail 确认应答的独立 resume 键（写入 InterruptRequest.metadata，
#: handler 据此把应答放到该键而非通用 RESUME_USER_INPUT_KEY）
SECURITY_LISTS_RESUME_USER_INPUT_KEY = "_security_lists_resume_user_input"

#: deny 标记键：permission_scene_hook 见到直接 reject（防二次弹窗）
SECURITY_LIST_DENY_KEY = "security_lists.denied"

#: 已批准标记键：值为本次 tool_call_id。permission_scene_hook 据此对本调用
#: 免二次弹窗（但仍需引擎自身裁决非 deny 才放行）
SECURITY_LIST_APPROVED_KEY = "security_lists.approved"

_AUTO_CONFIRM_PREFIX = "security_list:"

_SOURCE_LABELS = {
    "user": "用户名单",
    "user_approval": "审批记住",
    "builtin": "内置基线",
    "cloud": "云侧下发",
}
_TYPE_LABELS = {"file_path": "文件路径", "domain": "网络域名", "command": "命令"}


@dataclass
class _Hit:
    """单个目标的命中结果（target 三元组 + 裁决）。"""

    list_type: str
    target: str
    op: str
    verdict: Verdict


def _get_resume_user_input(ctx: AgentCallbackContext, tool_call_id: str) -> Any | None:
    """从独立 resume 键取本 Rail 的确认应答（对齐 evolution rail 模式）。"""
    raw_input = ctx.extra.get(SECURITY_LISTS_RESUME_USER_INPUT_KEY)
    if raw_input is None:
        return None
    if isinstance(raw_input, InteractiveInput):
        return raw_input.user_inputs.get(tool_call_id)
    if isinstance(raw_input, dict):
        if tool_call_id in raw_input:
            return raw_input[tool_call_id]
        return raw_input
    return raw_input


def _hit_metadata(hit: _Hit) -> dict[str, Any]:
    rec = hit.verdict.record
    return {
        "record_id": rec.id,
        "type": hit.list_type,
        "pattern": rec.pattern,
        "match": rec.match,
        "op": hit.op,
        "action": hit.verdict.action,
        "source": hit.verdict.source,
        "target": hit.target[:500],
    }


class UnifiedSecurityListRail(DeepAgentRail):
    """统一安全名单护栏：名单 deny/ask 先于权限引擎生效。"""

    priority: int = 95

    def __init__(
        self,
        composer: SecurityListComposer | None = None,
        engine_provider: Any | None = None,
    ) -> None:
        super().__init__()
        self._composer = composer or SecurityListComposer()
        self._helper = BaseInterruptRail()
        #: 惰性取权限引擎（装配侧注入 callable；取不到/异常即退化为串行判定）
        self._engine_provider = engine_provider

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "")
        if not tool_name:
            return
        if ctx.extra.get("_skip_tool"):
            # 更高优先级 rail 已跳过本次调用（防御；当前 100 座次不跳过工具）
            return

        tool_call = getattr(ctx.inputs, "tool_call", None)
        tool_call_id = self._helper._resolve_tool_call_id(tool_call)

        user_input = _get_resume_user_input(ctx, tool_call_id)
        if user_input is not None:
            decision = await self._resolve_resume(ctx, tool_call, tool_name, user_input)
            if decision is not None:
                ctx.extra["_interrupt_decision"] = decision
                self._helper._apply_decision(ctx, tool_call, tool_name, decision)
            return

        try:
            mode, session_id, hits, tool_args = self._collect_hits(ctx, tool_call)
        except Exception as exc:
            self._fallback_deny(ctx, tool_call, tool_name, exc)
            return

        if not hits:
            return

        deny_hit = next((h for h in hits if h.verdict.action == "deny"), None)
        if deny_hit is not None:
            self._deny(ctx, tool_call, tool_name, deny_hit, mode=mode, session_id=session_id)
            return

        ask_hits = [h for h in hits if h.verdict.action == "ask"]
        if not ask_hits:
            return  # 全 allow → 交权限引擎管线

        # 归一化：弹窗前向权限引擎预检一次裁决，避免"先问用户、再被引擎拒绝"
        engine_level, engine_rule = self._engine_precheck(tool_name, tool_args)
        if engine_level == "deny":
            self._deny_by_engine(
                ctx, tool_call, tool_name, engine_rule, mode=mode, session_id=session_id
            )
            return
        engine_ask_rule = engine_rule if engine_level == "ask" else None

        auto_config = None
        if ctx.session is not None:
            raw = ctx.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY)
            auto_config = raw if isinstance(raw, dict) else {}
        pending = [h for h in ask_hits if not self._is_auto_confirmed(auto_config, h)]
        if not pending:
            logger.info(
                "[security_lists] 会话内免重弹 tool=%s records=%s",
                tool_name,
                [h.verdict.record.id for h in ask_hits],
            )
            return

        first = pending[0]
        audit.log_event(
            audit.AUDIT_HIT,
            tool=tool_name,
            mode=mode,
            session_id=session_id or "",
            resolution="ask",
            hits=[_hit_metadata(h) for h in pending],
        )
        decision = self._helper.interrupt(
            self._build_ask_request(tool_name, pending, engine_rule=engine_ask_rule)
        )
        ctx.extra["_interrupt_decision"] = decision
        self._helper._apply_decision(ctx, tool_call, tool_name, decision)

    # ------------------------------------------------------------------
    # 求值
    # ------------------------------------------------------------------

    def _collect_hits(
        self,
        ctx: AgentCallbackContext,
        tool_call: Any,
    ) -> tuple[str, str | None, list[_Hit], dict[str, Any]]:
        """extract_targets → 逐目标 evaluate。异常原样上抛（调用方兜底）。

        返回 ``(mode, session_id, hits, tool_args)``；``tool_args`` 供引擎预检复用。
        """
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "")
        tool_args = PermissionInterruptRail.parse_tool_args(tool_call)

        from jiuwenswarm.common.config import get_config

        cfg = get_config()
        perms = cfg.get("permissions") if isinstance(cfg, dict) else {}
        if not isinstance(perms, Mapping):
            perms = {}

        workspace = None
        try:
            from jiuwenswarm.common.utils import get_workspace_dir

            workspace = get_workspace_dir()
        except Exception:
            workspace = None

        targets = extract_targets(
            tool_name,
            tool_args,
            workspace=workspace,
            permission_config=perms,
        )
        mode = current_permission_profile(perms)
        session_id = PermissionInterruptRail._resolve_session_id(ctx)

        hits: list[_Hit] = []
        seen: set[tuple[str, str]] = set()
        for list_type, target, op in targets:
            verdict = evaluate(
                list_type,
                target,
                op=op,
                mode=mode,
                composer=self._composer,
                session_id=session_id,
            )
            if verdict is None:
                continue
            # 同一记录被多个目标命中（整行/子命令/exe 名）只记首个 hit：
            # 弹窗/审计/记住均按记录维度处置一次
            key = (verdict.record.id, verdict.action)
            if key in seen:
                continue
            seen.add(key)
            hits.append(_Hit(list_type, target, op, verdict))
        return mode, session_id, hits, tool_args

    # ------------------------------------------------------------------
    # 引擎预检（名单结果 × 引擎结果的轻量归一化）
    # ------------------------------------------------------------------

    def _engine_precheck(
        self, tool_name: str, tool_args: Mapping[str, Any] | None
    ) -> tuple[str | None, str | None]:
        """向权限引擎查询一次裁决（不弹窗），用于与名单结果归一化。

        返回 ``(level, matched_rule)``，``level ∈ {"deny","ask","allow", None}``。
        未注入引擎 / 查询异常 → ``(None, None)``：退化为既有串行判定（引擎随后
        仍会自行判定），保证装配差异或版本差异下功能可用性不受影响。
        """
        provider = self._engine_provider
        if provider is None:
            return None, None
        try:
            engine = provider() if callable(provider) else provider
            if engine is None:
                return None, None
            level, rule = engine.check_tool_permission_directly(
                tool_name, dict(tool_args or {})
            )
        except Exception:
            logger.debug(
                "[security_lists] 权限引擎预检不可用，退化为串行判定", exc_info=True
            )
            return None, None
        name = str(getattr(level, "value", level) or "").lower()
        if name not in ("deny", "ask", "allow"):
            return None, rule
        return name, rule

    def _deny_by_engine(
        self,
        ctx: AgentCallbackContext,
        tool_call: Any,
        tool_name: str,
        rule: str | None,
        *,
        mode: str,
        session_id: str | None,
    ) -> None:
        """引擎预检为拒绝：直接拒绝且不弹窗。

        消除"名单 ask 先问用户、用户批准后又被引擎 deny"的白问一次。
        """
        detail = f"（引擎规则：{rule}）" if rule else ""
        message = (
            f"[SECURITY_LIST_DENIED] 权限引擎判定为拒绝{detail}，"
            f"已拒绝执行 {tool_name}。"
        )
        logger.warning(
            "[security_lists] 引擎预检 deny tool=%s rule=%s mode=%s",
            tool_name,
            rule,
            mode,
        )
        audit.log_event(
            audit.AUDIT_HIT,
            tool=tool_name,
            mode=mode,
            session_id=session_id or "",
            resolution="engine_deny_precheck",
            hits=[],
        )
        # 与名单 deny 同款：标记给 permission_scene_hook，引擎层直接 reject 不二次弹窗
        ctx.extra[SECURITY_LIST_DENY_KEY] = message
        decision = self._helper.reject(tool_result=message)
        ctx.extra["_interrupt_decision"] = decision
        self._helper._apply_decision(ctx, tool_call, tool_name, decision)

    # ------------------------------------------------------------------
    # deny / ask
    # ------------------------------------------------------------------

    def _deny(
        self,
        ctx: AgentCallbackContext,
        tool_call: Any,
        tool_name: str,
        hit: _Hit,
        *,
        mode: str,
        session_id: str | None,
    ) -> None:
        rec = hit.verdict.record
        message = (
            f"[SECURITY_LIST_DENIED] 命中安全名单规则"
            f"（{_TYPE_LABELS.get(hit.list_type, hit.list_type)}: {rec.pattern}，"
            f"来源：{_SOURCE_LABELS.get(hit.verdict.source, hit.verdict.source)}），"
            f"已拒绝执行 {tool_name}。"
        )
        logger.warning(
            "[security_lists] deny tool=%s record=%s pattern=%r source=%s mode=%s",
            tool_name,
            rec.id,
            rec.pattern,
            hit.verdict.source,
            mode,
        )
        audit.log_event(
            audit.AUDIT_HIT,
            tool=tool_name,
            mode=mode,
            session_id=session_id or "",
            resolution="deny",
            hits=[_hit_metadata(hit)],
        )
        # 标记给 permission_scene_hook：权限引擎层直接 reject，避免二次弹窗
        ctx.extra[SECURITY_LIST_DENY_KEY] = message
        decision = self._helper.reject(tool_result=message)
        ctx.extra["_interrupt_decision"] = decision
        self._helper._apply_decision(ctx, tool_call, tool_name, decision)

    def _build_ask_request(
        self,
        tool_name: str,
        pending: list[_Hit],
        *,
        engine_rule: str | None = None,
    ) -> InterruptRequest:
        lines = [f"工具 {tool_name} 命中安全名单规则，是否放行？"]
        for hit in pending[:3]:
            rec = hit.verdict.record
            lines.append(
                f"- [{_SOURCE_LABELS.get(hit.verdict.source, hit.verdict.source)}] "
                f"{_TYPE_LABELS.get(hit.list_type, hit.list_type)}: {rec.pattern}"
                + (f"（{rec.note}）" if rec.note else "")
            )
        if len(pending) > 3:
            lines.append(f"- …等共 {len(pending)} 条")
        if engine_rule:
            # 归一化：引擎同样要求确认，合并为一次弹窗（避免引擎层二次弹窗）
            lines.append(f"- [权限引擎] 同时要求确认：{engine_rule}")
        return InterruptRequest(
            message="\n".join(lines),
            payload_schema=ConfirmPayload.to_schema(),
            metadata={
                "source": "security_lists",
                "resume_user_input_key": SECURITY_LISTS_RESUME_USER_INPUT_KEY,
                "hits": [_hit_metadata(h) for h in pending],
            },
        )

    @staticmethod
    def _is_auto_confirmed(auto_config: Optional[dict], hit: _Hit) -> bool:
        record_id = hit.verdict.record.id
        if not auto_config or not record_id:
            return False
        return bool(auto_config.get(_AUTO_CONFIRM_PREFIX + record_id))

    # ------------------------------------------------------------------
    # 复活分支（用户确认应答）
    # ------------------------------------------------------------------

    async def _resolve_resume(
        self,
        ctx: AgentCallbackContext,
        tool_call: Any,
        tool_name: str,
        user_input: Any,
    ) -> InterruptDecision | None:
        payload = PermissionInterruptRail.parse_confirm_payload(user_input)
        if payload is None:
            logger.warning(
                "[security_lists] 无法解析确认应答 tool=%s input_type=%s，重弹确认",
                tool_name,
                type(user_input).__name__,
            )
            # 应答畸形：重弹一次（不带 hits 明细，由首检链路重建）
            return self._helper.interrupt(
                InterruptRequest(
                    message=f"工具 {tool_name} 命中安全名单规则，是否放行？",
                    payload_schema=ConfirmPayload.to_schema(),
                    metadata={
                        "source": "security_lists",
                        "resume_user_input_key": SECURITY_LISTS_RESUME_USER_INPUT_KEY,
                    },
                )
            )

        # 重新求值（确认期间配置可能已变更；deny 永远优先于用户批准）
        try:
            mode, session_id, hits, tool_args = self._collect_hits(ctx, tool_call)
        except Exception as exc:
            self._fallback_deny(ctx, tool_call, tool_name, exc)
            return None

        deny_hit = next((h for h in hits if h.verdict.action == "deny"), None)
        if deny_hit is not None:
            self._deny(ctx, tool_call, tool_name, deny_hit, mode=mode, session_id=session_id)
            return None

        # 引擎预检：确认期间引擎规则也可能变严，deny 同样优先于用户批准
        engine_level, engine_rule = self._engine_precheck(tool_name, tool_args)
        if engine_level == "deny":
            self._deny_by_engine(
                ctx, tool_call, tool_name, engine_rule, mode=mode, session_id=session_id
            )
            return None

        if not payload.approved:
            feedback = payload.feedback or "[SECURITY_LIST_REJECTED] 用户拒绝了本次操作。"
            audit.log_event(
                audit.AUDIT_HIT,
                tool=tool_name,
                mode=mode,
                session_id=session_id or "",
                resolution="user_reject",
                hits=[_hit_metadata(h) for h in hits if h.verdict.action == "ask"],
            )
            return self._helper.reject(tool_result=feedback)

        ask_hits = [h for h in hits if h.verdict.action == "ask"]
        resolution = "approve_once"
        persisted = False
        permanent = payload.wants_permanent_persist()
        if permanent:
            persisted = self._persist_remember(ask_hits, mode=mode, session_id=session_id, permanent=True)
            resolution = "permanent_allow" if persisted else "approve_once"
        elif payload.wants_session_persist():
            persisted = self._persist_remember(ask_hits, mode=mode, session_id=session_id, permanent=False)
            resolution = "session_allow" if persisted else "approve_once"

        # 会话内免重弹（永久记住落盘成功的不必再存——下次求值直接命中审批格）
        if payload.auto_confirm and ctx.session is not None and not (permanent and persisted):
            self._store_auto_confirm(ctx, ask_hits)

        audit.log_event(
            audit.AUDIT_HIT,
            tool=tool_name,
            mode=mode,
            session_id=session_id or "",
            resolution=resolution,
            hits=[_hit_metadata(h) for h in ask_hits],
        )
        # 归一化：标记本调用已获用户批准（值为 tool_call_id），权限引擎层据此
        # 免二次弹窗；引擎自身 deny 不受影响（scene hook 会再查一次引擎裁决）
        ctx.extra[SECURITY_LIST_APPROVED_KEY] = self._helper._resolve_tool_call_id(tool_call)
        return self._helper.approve()

    def _store_auto_confirm(self, ctx: AgentCallbackContext, ask_hits: list[_Hit]) -> None:
        if ctx.session is None:
            return
        config = ctx.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY)
        if not isinstance(config, dict):
            config = {}
        changed = False
        for hit in ask_hits:
            record_id = hit.verdict.record.id
            if record_id and not config.get(_AUTO_CONFIRM_PREFIX + record_id):
                config[_AUTO_CONFIRM_PREFIX + record_id] = True
                changed = True
        if changed:
            ctx.session.update_state({INTERRUPT_AUTO_CONFIRM_KEY: config})

    # ------------------------------------------------------------------
    # 记住（审批条目：command→approval_overrides，file_path→file_guard.paths）
    # ------------------------------------------------------------------

    def _persist_remember(
        self,
        ask_hits: list[_Hit],
        *,
        mode: str,
        session_id: str | None,
        permanent: bool,
    ) -> bool:
        """把 ask 命中记录写成与名单格子语义一致的审批条目（带 mode/created_at）。

        domain 类无审批存储通道（引擎 NetGuard 无 ask 语义），跳过并告警——
        该命中按"仅本次放行"处理。
        """
        overrides: list[dict[str, Any]] = []
        fg_paths: list[dict[str, Any]] = []
        now = utc_now_iso()
        for hit in ask_hits:
            rec = hit.verdict.record
            entry_id = f"sl_{rec.id or new_record_id()}_{mode}"
            if rec.type == "command":
                # glob 记录沿用原 pattern（同等语义）；exact/regex 落具体目标
                # （审批投影恒为 glob，regex 原文当 glob 会改变语义）
                pattern = rec.pattern if rec.match == "glob" else hit.target
                overrides.append({
                    "id": entry_id,
                    "match_type": "command",
                    "pattern": pattern,
                    "action": "allow",
                    "mode": mode,
                    "created_at": now,
                })
            elif rec.type == "file_path":
                entry: dict[str, Any] = {
                    "id": entry_id,
                    "path": rec.pattern,
                    "match": rec.match if rec.match in ("prefix", "glob") else "prefix",
                    "mode": mode,
                    "created_at": now,
                }
                if hit.op in ("read", "write", "exec"):
                    entry[hit.op] = "allow"
                else:
                    entry.update({"read": "allow", "write": "allow", "exec": "allow"})
                fg_paths.append(entry)
            else:
                logger.warning(
                    "[security_lists] domain 记录不支持审批记住（按仅本次放行）record=%s",
                    rec.id,
                )
        if not overrides and not fg_paths:
            return False

        scope = "permanent" if permanent else "session"
        if permanent:
            from jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist import (
                persist_merged_allow_rule_snapshot,
            )

            snapshot: dict[str, Any] = {}
            if overrides:
                snapshot["approval_overrides"] = overrides
            if fg_paths:
                snapshot["file_guard"] = {"paths": fg_paths}
            ok = persist_merged_allow_rule_snapshot(snapshot)
        else:
            if not session_id:
                logger.warning("[security_lists] 会话记住缺少 session_id，按仅本次放行")
                return False
            from jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist import (
                get_permissions_with_session_overlay,
                persist_session_allow_rule,
            )

            merged = deepcopy(get_permissions_with_session_overlay(session_id=session_id))
            if not isinstance(merged, dict):
                return False
            if overrides:
                existing = merged.setdefault("approval_overrides", [])
                if isinstance(existing, list):
                    known = {e.get("id") for e in existing if isinstance(e, dict)}
                    existing.extend(e for e in overrides if e["id"] not in known)
            if fg_paths:
                fg = merged.setdefault("file_guard", {})
                if isinstance(fg, dict):
                    paths = fg.setdefault("paths", [])
                    if isinstance(paths, list):
                        known = {e.get("id") for e in paths if isinstance(e, dict)}
                        paths.extend(e for e in fg_paths if e["id"] not in known)
            ok = persist_session_allow_rule(merged, session_id=session_id)

        logger.info(
            "[security_lists] 审批记住 scope=%s ok=%s overrides=%d fg_paths=%d mode=%s",
            scope,
            ok,
            len(overrides),
            len(fg_paths),
            mode,
        )
        if ok:
            audit.log_event(
                audit.AUDIT_CHANGE,
                scope=scope,
                mode=mode,
                approval_overrides=overrides,
                file_guard_paths=fg_paths,
            )
        return ok

    # ------------------------------------------------------------------
    # fail-closed 兜底
    # ------------------------------------------------------------------

    def _fallback_deny(
        self,
        ctx: AgentCallbackContext,
        tool_call: Any,
        tool_name: str,
        exc: Exception,
    ) -> None:
        message = (
            f"[SECURITY_LIST_DENIED] 安全名单读取异常（{type(exc).__name__}），"
            f"按 fail-closed 策略拒绝执行 {tool_name}。"
        )
        logger.error(
            "[security_lists] 名单求值异常，fail-closed 拒绝 tool=%s: %s",
            tool_name,
            exc,
            exc_info=True,
        )
        audit.log_event(
            audit.AUDIT_FALLBACK,
            tool=tool_name,
            reason=f"{type(exc).__name__}: {exc}",
        )
        self._notify_desktop_fallback(ctx, message)
        ctx.extra[SECURITY_LIST_DENY_KEY] = message
        decision = self._helper.reject(tool_result=message)
        ctx.extra["_interrupt_decision"] = decision
        self._helper._apply_decision(ctx, tool_call, tool_name, decision)

    @staticmethod
    def _notify_desktop_fallback(ctx: AgentCallbackContext, message: str) -> None:
        """桌面健康告警（behavior bridge 通道，best-effort，不阻断拒绝）。"""
        from jiuwenswarm.agents.harness.common.rails.security_lists.notify import (
            report_security_event,
        )

        report_security_event(ctx, stage="security.fallback", detail=message)
