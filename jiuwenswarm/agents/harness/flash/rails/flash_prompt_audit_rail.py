# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.flash.prompt.snapshot import (
    PromptSnapshot,
    build_prompt_snapshot,
)
from jiuwenswarm.agents.harness.flash.sections import (
    FLASH_SECTION_REGISTRY,
    resolve_carrier,
)

logger = logging.getLogger(__name__)

# 反引号内 snake_case 标识符（工具名候选）。
_TOOL_TOKEN_RE = re.compile(r"`([a-z][a-z0-9_]{2,40})`")

# 510 输入消息约定段里的 JSON schema 示例占位符——合法保留，不算未渲染变量。
_KNOWN_SAFE_PLACEHOLDERS = frozenset({
    "channel",
    "preferred_response_language",
    "content",
    "supplementary_info",
    "source",
    "type",
    "memory_id",
    "id",
    "handle",
    "path",
    "tool_name",
    "arguments",
    "call_goal",
    "description",
    "available_agents",
    "target_channels",
})

# 未渲染变量：``{...}`` 且花括号内是「标识符形态」的模板变量。收紧为标识符语法
# （首字符字母/下划线）而非任意非 ``{}:`` 字符——否则 ``^[0-9]{6}$`` 这类正则量词
# 会被误判为未渲染变量（评审 #4）。JSON 对象字面量（含 ``:`` 或嵌套 ``{``）本就不命中。
_UNRENDERED_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _is_static_section(section: Any) -> bool:
    """段是否属于静态区（carrier=system）——供 _static_hash / _prefix_metric 复用。"""
    return resolve_carrier(
        int(getattr(section, "priority", 0) or 0),
        str(getattr(section, "name", "") or ""),
    ) == "system"


@dataclass
class AuditReport:
    """一次合规检查的结构化结果。"""

    sections: list[tuple[int, str]] = field(default_factory=list)
    duplicate_priorities: list[tuple[int, list[str]]] = field(default_factory=list)
    duplicate_names: list[str] = field(default_factory=list)
    unrendered_vars: list[tuple[str, str]] = field(default_factory=list)
    unregistered_priorities: list[int] = field(default_factory=list)
    ghost_tools: list[str] = field(default_factory=list)
    static_hash: str = ""
    static_hash_drift: bool = False
    prefix_hash: str = ""
    prefix_chars: int = 0
    snapshot_full_hash: str = ""
    snapshot_static_hash: str = ""
    snapshot_prefix_hash: str = ""
    snapshot_prefix_chars: int = 0
    section_hashes: list[tuple[int, str, str]] = field(default_factory=list)
    band_hashes: dict[str, str] = field(default_factory=dict)
    skipped_checks: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """硬违规清零才算通过（advisory 项不计入）。

        ``duplicate_priorities`` 是 advisory（与 ``unregistered_priorities`` 口径一致）：
        迁移期 ``_SAME_ORDER_GROUPS`` 刻意保留同号组（offload+compression_recall@450、
        workspace+tool_navigation@70 等），撞号裁决靠 ``summary()`` 打点，不计入硬失败——
        否则 ``fail_closed=True`` 会因这些预期同号组直接中断全部模型调用。
        """
        return not (
            self.duplicate_names
            or self.unrendered_vars
            or self.static_hash_drift
        )

    def summary(self) -> str:
        parts = [f"sections={len(self.sections)}"]
        if self.duplicate_priorities:
            parts.append(f"撞号={self.duplicate_priorities}")
        if self.duplicate_names:
            parts.append(f"重复段={self.duplicate_names}")
        if self.unrendered_vars:
            parts.append(f"未渲染变量={self.unrendered_vars}")
        if self.unregistered_priorities:
            parts.append(f"未登记段号={self.unregistered_priorities}")
        if self.ghost_tools:
            parts.append(f"幽灵工具={self.ghost_tools}")
        if self.skipped_checks:
            parts.append(f"跳过={self.skipped_checks}")
        return " | ".join(parts)


class AuditViolation(AbortError):
    """CI 期 fail_closed 时抛出的硬违规。

    必须继承 :class:`AbortError` 而非 ``RuntimeError``：普通回调异常会被 agent-core
    的 callback manager 记录后继续执行，导致 ``fail_closed`` 形同虚设；``AbortError``
    才会中断回调执行并从 ``trigger()`` 边界传播出去。
    """


class FlashPromptAuditRail(DeepAgentRail):
    """Last-resort prompt auditor; runs after every other prompt-injecting rail."""

    # 所有段注入之后执行（数值最小 → 最后执行，见 07 §六 priority 语义）。
    # DisabledToolsRail priority=1（最后注销禁用工具）；audit 需在其之后（取 0）才能
    # 看到最终工具面，故这里用 0，保证 audit 是真正最后一道检查。
    priority = 0
    inherit_to_subagents = False

    def __init__(
        self,
        fail_closed: bool = False,
        fail_closed_provider: Callable[[], bool] | None = None,
        config_fingerprint_provider: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._agent: Any = None
        self._ability_manager: Any = None
        self.fail_closed = fail_closed
        # 每轮从 provider 现读 fail_closed（热重载后下一轮即生效，评审 #6）；无 provider
        # 则用构造期值（离线/单测路径）。
        self._fail_closed_provider = fail_closed_provider
        # 配置指纹 provider：返回影响静态提示词内容的配置指纹（可 hash）。指纹变化 = 已生效
        # 的合法配置变更，应重置静态 hash 基线、re-seed 到新内容（评审 #2）；无 provider 则
        # 退化为「首轮 seed、永不更新」的旧行为。
        self._config_fingerprint_provider = config_fingerprint_provider
        self._last_config_fingerprint: Any = None
        # 会话内静态区 hash 基线：首次 before_model_call 记为 seed，后续比对。
        self._static_hash_seed: str | None = None

    def init(self, agent) -> None:
        self._agent = agent
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._ability_manager = getattr(agent, "ability_manager", None)

    def uninit(self, agent) -> None:
        self._static_hash_seed = None
        self.system_prompt_builder = None
        self._agent = None
        self._ability_manager = None

    async def before_model_call(self, ctx) -> None:
        # 热重载后每轮从 provider 现读 fail_closed（评审 #6），再取活 builder 审计。
        if self._fail_closed_provider is not None:
            self.fail_closed = self._fail_closed_provider()
        # 与其他 flash rail 口径一致：每轮从 ctx 取活 builder（热重载会重建
        # system_prompt_builder），而不是只用 init 时抓到的旧实例——否则审计对象
        # 停留在旧 builder，新 prompt 从未被审计、旧 builder 变化时 static_hash_drift
        # 持续误报，fail_closed=True 时还会基于 stale 数据打断模型调用。
        builder = getattr(getattr(ctx, "agent", None), "system_prompt_builder", None)
        if builder is not None:
            self.system_prompt_builder = builder
        # 每轮从 ctx.agent 刷新工具面（评审 D2）：ghost 工具检查用当前工具面，而非 init 时
        # 的旧值（内层 ReActAgent 的工具面可能每轮/热重载变化）。
        ability_manager = getattr(getattr(ctx, "agent", None), "ability_manager", None)
        if ability_manager is not None:
            self._ability_manager = ability_manager
        # 配置指纹变化 = 已生效的合法配置变更 → 重置静态 hash 基线（评审 #2）：否则
        # persona 关/开等正常配置变更会改写静态区、被当漂移，fail_closed 持续中断后续轮次。
        # 语言也算提示词配置（热重载改语言同样触发重基线）。
        if self._config_fingerprint_provider is not None:
            fingerprint = self._config_fingerprint_provider()
            if fingerprint is not None:
                lang = getattr(self.system_prompt_builder, "language", None) or "cn"
                fingerprint = f"{fingerprint}\0{lang}"
                if (
                    self._last_config_fingerprint is not None
                    and fingerprint != self._last_config_fingerprint
                ):
                    self._static_hash_seed = None
                self._last_config_fingerprint = fingerprint
        report = self.check_compliance()
        if self._static_hash_seed is None and report.static_hash:
            self._static_hash_seed = report.static_hash
        if report.prefix_hash:
            # 前缀度量打点（点 7 数据源）：字符长度 + 前缀 hash 短值，每轮输出。
            logger.info(
                "[FlashPromptAudit] prefix chars=%d hash=%s",
                report.prefix_chars,
                report.prefix_hash[:16],
            )
        if report.static_hash_drift:
            logger.warning(
                "[FlashPromptAudit] 静态区 hash 漂移: %s -> %s",
                self._static_hash_seed,
                report.static_hash,
            )
        if not report.ok:
            logger.warning("[FlashPromptAudit] %s", report.summary())
        elif report.duplicate_priorities or report.unregistered_priorities or report.ghost_tools:
            logger.info("[FlashPromptAudit] advisory: %s", report.summary())
        if self.fail_closed and not report.ok:
            raise AuditViolation(f"prompt audit failed: {report.summary()}")

    def check_compliance(self) -> AuditReport:
        """对当前 system_prompt_builder 的段做合规检查（纯函数，不改状态）。"""
        report = AuditReport()
        builder = self.system_prompt_builder
        if builder is None:
            report.skipped_checks.append("no_system_prompt_builder")
            return report

        sections = self._collect_sections(builder)
        report.sections = [(int(s.priority), str(s.name)) for s in sections]

        self._check_duplicates(sections, report)
        self._check_unrendered_vars(sections, report)
        self._check_unregistered_priorities(sections, report)
        self._check_ghost_tools(sections, report)
        report.static_hash = self._static_hash(sections)
        snapshot = self.build_snapshot(sections)
        if snapshot is not None:
            report.snapshot_full_hash = snapshot.full_hash
            report.snapshot_static_hash = snapshot.static_hash
            report.snapshot_prefix_hash = snapshot.prefix_hash
            report.snapshot_prefix_chars = snapshot.prefix_chars
            report.section_hashes = list(snapshot.section_hashes)
            report.band_hashes = snapshot.band_hashes
            # 前缀度量与 SDK 渲染对齐（当前语言 + 双换行 + 过滤空段），直接复用
            # snapshot 结果；覆盖旧 _prefix_metric 口径（首个非空语言 + 单换行），
            # 否则日志里的 prefix_chars/hash 与模型实际收到的文本不符。
            report.prefix_hash = snapshot.prefix_hash
            report.prefix_chars = snapshot.prefix_chars
        else:
            report.prefix_hash, report.prefix_chars = self._prefix_metric(sections)
        report.static_hash_drift = (
            self._static_hash_seed is not None
            and report.static_hash != self._static_hash_seed
        )
        return report

    def build_snapshot(self, sections: list[Any] | None = None) -> PromptSnapshot | None:
        """Return the deterministic offline snapshot for the current sections."""
        builder = self.system_prompt_builder
        if sections is None:
            if builder is None:
                return None
            sections = self._collect_sections(builder)
        language = getattr(builder, "language", None) or "cn"
        return build_prompt_snapshot(sections, language=language)

    # ── 各检查项 ───────────────────────────────────────────────

    @staticmethod
    def _collect_sections(builder) -> list[Any]:
        try:
            sections = builder.get_all_sections() or []
        except AttributeError:
            return []
        # 真实 SystemPromptBuilder.get_all_sections() 返回 Dict[name, PromptSection]；
        # list(dict) 会得到 key（段名字符串）而非段对象，必须显式取 values()。单测的
        # fake builder 返回 list[PromptSection]，这里兼容两种形态。
        if isinstance(sections, dict):
            return list(sections.values())
        return list(sections)

    @staticmethod
    def _check_duplicates(sections: list[Any], report: AuditReport) -> None:
        by_priority: dict[int, list[str]] = {}
        by_name: dict[str, int] = {}
        for s in sections:
            name = str(s.name)
            priority = int(getattr(s, "priority", 0) or 0)
            by_priority.setdefault(priority, []).append(name)
            by_name[name] = by_name.get(name, 0) + 1
        report.duplicate_priorities = [
            (priority, names) for priority, names in sorted(by_priority.items()) if len(names) > 1
        ]
        report.duplicate_names = sorted(name for name, count in by_name.items() if count > 1)

    @staticmethod
    def _check_unrendered_vars(sections: list[Any], report: AuditReport) -> None:
        # 只扫静态区（carrier=system）：未渲染变量是 flash 静态模板渲染的产物；
        # 动态/relay 段（request_system_prompt 等 attachment）可能合法携带正则/JSON
        # 等含 ``{}`` 的正文，不该扫（评审 #4）。
        for s in sections:
            if not _is_static_section(s):
                continue
            name = str(s.name)
            content = getattr(s, "content", None)
            if not isinstance(content, dict):
                continue
            for text in content.values():
                if not isinstance(text, str):
                    continue
                for match in _UNRENDERED_VAR_RE.finditer(text):
                    token = match.group(1).strip()
                    if token in _KNOWN_SAFE_PLACEHOLDERS:
                        continue
                    report.unrendered_vars.append((name, f"{{{token}}}"))

    @staticmethod
    def _check_unregistered_priorities(sections: list[Any], report: AuditReport) -> None:
        seen: set[int] = set()
        for s in sections:
            priority = int(getattr(s, "priority", 0) or 0)
            if priority in seen:
                continue
            seen.add(priority)
            if priority not in FLASH_SECTION_REGISTRY:
                report.unregistered_priorities.append(priority)
        report.unregistered_priorities.sort()

    def _check_ghost_tools(self, sections: list[Any], report: AuditReport) -> None:
        manager = self._ability_manager
        known = self._known_tool_names(manager)
        if known is None:
            report.skipped_checks.append("ghost_tools_no_ability_manager")
            return
        mentioned: set[str] = set()
        for s in sections:
            content = getattr(s, "content", None)
            if not isinstance(content, dict):
                continue
            for text in content.values():
                if not isinstance(text, str):
                    continue
                for token in _TOOL_TOKEN_RE.findall(text):
                    mentioned.add(token)
        # 只把「看起来像工具名且工具面确实没有」的记作幽灵工具；反引号里的路径/
        # 变量/普通标识符不误报——因此额外要求命中一个已知工具名时才视为工具语境。
        report.ghost_tools = sorted(
            token for token in mentioned if token not in known and self._looks_like_tool(token, known)
        )

    @staticmethod
    def _known_tool_names(manager: Any) -> set[str] | None:
        if manager is None:
            return None
        for attr in ("cards", "_tools", "tools"):
            value = getattr(manager, attr, None)
            if isinstance(value, dict):
                return {str(k) for k in value}
        names = getattr(manager, "list_tool_names", None)
        if callable(names):
            try:
                result = names()
                if isinstance(result, (list, set, tuple)):
                    return {str(n) for n in result}
            except Exception:
                logger.debug("[FlashPromptAuditRail] list_tool_names() failed", exc_info=True)
        return None

    @staticmethod
    def _looks_like_tool(token: str, known: set[str]) -> bool:
        """粗略判定：含下划线或命中已知工具名的词干（memory/todo/read/write/edit）。"""
        if token in known:
            return False
        if "_" in token:
            return True
        return any(
            token == stem or token.endswith(f"_{stem}") or token.startswith(f"{stem}_")
            for stem in ("memory", "todo", "skill", "tool", "read", "write", "edit", "bash", "web")
        )

    @staticmethod
    def _static_hash(sections: list[Any]) -> str:
        static = [s for s in sections if _is_static_section(s)]
        payload_lines: list[str] = []
        for s in sorted(static, key=lambda s: int(getattr(s, "priority", 0) or 0)):
            name = str(s.name)
            content = getattr(s, "content", None)
            if isinstance(content, dict):
                rendered = repr(sorted(content.items()))
            else:
                rendered = repr(content)
            payload_lines.append(f"{int(getattr(s, 'priority', 0) or 0)}\t{name}\t{rendered}")
        return hashlib.sha256("\n".join(payload_lines).encode("utf-8")).hexdigest()

    @staticmethod
    def _prefix_metric(sections: list[Any]) -> tuple[str, int]:
        """字符级前缀度量：静态区（carrier=system）按升序拼接正文后 hash。

        这是点 7「前缀稳定性」的数据源——前缀 hash 每轮打点，变化即缓存失效信号；
        ``prefix_chars`` 是静态前缀的字符长度（影响 KV 命中与成本）。
        """
        static = sorted(
            (s for s in sections if _is_static_section(s)),
            key=lambda s: int(getattr(s, "priority", 0) or 0),
        )
        parts: list[str] = []
        for s in static:
            content = getattr(s, "content", None)
            if isinstance(content, dict):
                text = next((v for v in content.values() if v), "")
            else:
                text = content or ""
            parts.append(text)
        joined = "\n".join(parts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest(), len(joined)


__all__ = ["AuditReport", "AuditViolation", "FlashPromptAuditRail"]
