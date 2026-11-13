# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Single Flash-owned entry point for static prompt text transforms.

Step 4 起逐域接管：``takeover_domains`` 内的域不再跑对应转换 Rail，改由声明式注册表
:data:`flash.prompt.registry.FLASH_PROMPT_REGISTRY` 直接 supersede Deep 旧段（``remove_section``
supersedes + ``add_section`` render 新段）；未接管的域仍走 4 个纯文本转换 Rail 聚合。两条路径
共用同一注册表 render，故运行时与离线快照零漂移。``verify_only`` 模式下额外按注册表重建并比对
builder 现状，漂移仅告警不裁决。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from ..rails.flash_persona_rail import FlashPersonaRail
from ..rails.flash_response_rail import FlashResponseRail
from ..rails.flash_safety_rail import FlashSafetyRail
from ..rails.flash_tool_guide_rail import FlashToolGuideRail
from .registry import (
    FLASH_PROMPT_REGISTRY,
    PromptRenderContext,
    build_default_context,
    reconcile,
)

logger = logging.getLogger(__name__)


# 可安全走声明式接管（declarative takeover）的域：必须同时满足
# (1) 在 ``_domain_rails`` 内（composer 自有纯文本 rail，接管 = 跳过该 rail 改由注册表 replace）；
# (2) 注册表 ``FLASH_PROMPT_REGISTRY`` 内有 block + render，且与 rail 路径逐字节等价已证明。
# 当前仅 safety/response 满足。tools 尚未完整提取 310/320/工具目录/task_tool 子块——
# 直接接管会 remove 整个 tools/execution_principles 母段再只补 300/330/340，静默丢失
# 工具目录（P1 隐患）；persona 无注册表 block；todo/subagent/memory 归各自独立 rail、
# 不在 composer 的 ``_domain_rails`` 内，无法由 composer 接管。列入越界域一律告警并回退 rail 路径。
TAKEOVERABLE_DOMAINS: frozenset[str] = frozenset({"safety", "response"})


def coerce_takeover_domains(
    domains: frozenset[str] | set[str] | tuple[str, ...],
) -> frozenset[str]:
    """把接管域过滤到 :data:`TAKEOVERABLE_DOMAINS`，越界域告警并剔除。

    composer 与 adapter 共用此函数作为单一事实源，避免「adapter 放行、composer 拦截」
    口径不一致。越界的域（tools/persona/todo/subagent/memory/拼写错误）一律告警剔除，
    保证 tools 绝不进入声明式接管——否则 ``_apply_declarative`` 会 remove tools 母段后
    只补 300/330/340，静默丢失工具目录/310/320。
    """
    invalid = frozenset(domains) - TAKEOVERABLE_DOMAINS
    if invalid:
        logger.warning(
            "takeover_domains %s are not takeoverable (valid=%s); ignored — "
            "those domains keep running via their own rail",
            sorted(invalid),
            sorted(TAKEOVERABLE_DOMAINS),
        )
    return frozenset(domains) & TAKEOVERABLE_DOMAINS


class FlashPromptComposerRail(DeepAgentRail):
    """Run all static Flash prompt transforms from one deterministic entry point.

    Deep keeps ownership of its original Rails and side effects. This Rail only
    reconciles prompt sections on the current Flash agent:

    1. identity/workspace transform;
    2. safety split/rewrite;
    3. tools transform after identity exposes execution principles;
    4. response/output renumber and rewrite.

    Stateful behavior remains in dedicated Rails. In particular, A2UI ownership
    stays with Deep's ResponsePromptRail; this composer only supersedes the
    response text sections when the response transform is enabled.

    ``takeover_domains``（Step 4）：列出的域不再跑对应 Rail，改由声明式注册表
    :data:`~flash.prompt.registry.FLASH_PROMPT_REGISTRY` 直接 supersede Deep 旧段
    （仅 :data:`TAKEOVERABLE_DOMAINS` = safety/response 可接管；tools 因未完整提取
    310/320/工具目录而暂禁，persona 无 block，todo/subagent/memory 归独立 rail）。

    ``verify_only``：额外按注册表重建并与 builder 现状比对，漂移告警不裁决。默认关。
    """

    # Base RuntimePromptRail/ResponsePromptRail run at 5. Audit runs at 2.
    priority = 4
    inherit_to_subagents = False

    def __init__(
        self,
        *,
        persona_enabled: bool = True,
        safety_enabled: bool = True,
        tool_guide_enabled: bool = True,
        response_enabled: bool = True,
        verify_only: bool = False,
        takeover_domains: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self._system_prompt_builder: Any = None
        self._domain_rails = (
            ("persona", persona_enabled, FlashPersonaRail()),
            ("safety", safety_enabled, FlashSafetyRail()),
            ("tools", tool_guide_enabled, FlashToolGuideRail()),
            ("response", response_enabled, FlashResponseRail()),
        )
        self._verify_only = verify_only
        self._takeover_domains = coerce_takeover_domains(takeover_domains)

    @property
    def transform_names(self) -> tuple[str, ...]:
        """Return rails that will actually run (enabled and not declaratively taken over)."""
        return tuple(
            rail.__class__.__name__
            for key, enabled, rail in self._domain_rails
            if enabled and key not in self._takeover_domains
        )

    def init(self, agent: Any) -> None:
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        for _key, _enabled, rail in self._domain_rails:
            rail.init(agent)

    def uninit(self, agent: Any) -> None:
        for _key, _enabled, rail in self._domain_rails:
            rail.uninit(agent)
        self._system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = (
            getattr(ctx.agent, "system_prompt_builder", None)
            or self._system_prompt_builder
        )
        if builder is None:
            return
        self._system_prompt_builder = builder

        for key, enabled, rail in self._domain_rails:
            if not enabled or key in self._takeover_domains:
                continue
            rail.apply(builder)

        self._apply_declarative(builder)

        if self._verify_only:
            self._verify_declarative(builder)

    def _build_render_context(self, builder: Any) -> PromptRenderContext:
        """为声明式 render 构建渲染上下文（语言 + 工具面）。

        被接管域（safety/response/tools）均无工具门控，``enabled_tools`` 置空；
        todo/subagent/memory 虽有门控但归各自独立 rail，不在此接管。
        """
        return PromptRenderContext(
            lang=self._resolve_language(builder),
            enabled_tools=frozenset(),
        )

    def _apply_declarative(self, builder: Any) -> None:
        """Step 4 接管：按声明式注册表 replace 被接管域的 Deep 旧段为 Flash 新段。

        ``takeover_domains`` 内的域不再跑对应 Rail（``before_model_call`` 已跳过），
        改由本方法依据 :data:`FLASH_PROMPT_REGISTRY` 声明撤旧（``supersedes``）+ 补新
        （``render``）。仅对「composer 自有 + 注册表内有 block + enabled」的域生效；
        persona 等尚未登记注册表的域不可接管。
        """
        enabled_takeover = {
            key
            for key, enabled, _rail in self._domain_rails
            if enabled and key in self._takeover_domains
        }
        if not enabled_takeover:
            return
        ctx = self._build_render_context(builder)
        applied: set[str] = set()
        for block in FLASH_PROMPT_REGISTRY:
            if block.key not in enabled_takeover:
                continue
            if block.enabled_when is not None and not block.enabled_when(ctx):
                continue
            if block.render is None:
                continue
            try:
                sections = block.render(ctx)
            except AssertionError as exc:
                # 上游 Deep 源漂移触发 guard（与 rail 路径同构）：保留源段原样。
                logger.warning(
                    "[FlashPromptComposerRail] declarative render guard hit for %s: %s",
                    block.key,
                    exc,
                )
                continue
            for name in block.supersedes:
                builder.remove_section(name)
            for section in sections:
                builder.add_section(section)
            applied.add(block.key)
        missing = enabled_takeover - applied
        if missing:
            logger.warning(
                "[FlashPromptComposerRail] takeover domains not applied "
                "(no registry block / gated / guard hit): %s",
                sorted(missing),
            )

    def _verify_declarative(self, builder: Any) -> None:
        """Step 3 verify-only：按声明式注册表重建静态段，与 builder 现状比对。

        仅告警不裁决（不接管内容）。注：persona 域非确定性、不在注册表内，故只校验
        safety/response/tools/todo/subagent/memory 六域；todo/subagent/memory 在运行时
        由各自独立 rail 装配，其工具门控与默认上下文可能不一致，漂移记录 debug 级。
        """
        language = self._resolve_language(builder)
        expected = reconcile(build_default_context(language))

        drift: list[str] = []
        for section in expected:
            actual = self._get_section(builder, section.name)
            if actual is None:
                drift.append(f"{section.name}: missing (expect priority={section.priority})")
                continue
            actual_priority = getattr(actual, "priority", None)
            if actual_priority != section.priority:
                drift.append(
                    f"{section.name}: priority {actual_priority} != {section.priority}"
                )
            actual_text = self._extract_content(actual, builder)
            expected_text = self._extract_content(section, builder)
            if actual_text != expected_text:
                drift.append(
                    f"{section.name}: text drift ({len(actual_text)} vs {len(expected_text)} chars)"
                )

        if drift:
            logger.warning(
                "[FlashPromptComposerRail] declarative reconcile drift:\n%s",
                "\n".join(drift),
            )
        else:
            logger.debug("[FlashPromptComposerRail] declarative reconcile matches builder")

    @staticmethod
    def _get_section(builder: Any, name: str) -> Any | None:
        get_section = getattr(builder, "get_section", None)
        if not callable(get_section):
            return None
        try:
            return get_section(name)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _extract_content(section: Any, builder: Any) -> str:
        content = getattr(section, "content", None)
        if isinstance(content, dict):
            language = getattr(builder, "language", None) or "cn"
            language = "cn" if language in ("cn", "zh") else "en"
            value = content.get(language)
            if not value:
                value = next((v for v in content.values() if v), "")
            return str(value)
        return str(content or "")

    @staticmethod
    def _resolve_language(builder: Any) -> str:
        language = getattr(builder, "language", None) or "cn"
        return "cn" if language in ("cn", "zh") else "en"


__all__ = ["FlashPromptComposerRail", "TAKEOVERABLE_DOMAINS", "coerce_takeover_domains"]
