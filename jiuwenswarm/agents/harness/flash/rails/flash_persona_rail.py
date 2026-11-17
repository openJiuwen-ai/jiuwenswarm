# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.flash.prompt.content.persona import (
    _DIRECTORY_MODEL_CN,
    _DIRECTORY_MODEL_EN,
    _EXEC_PRINCIPLES_CN,
    _EXEC_PRINCIPLES_EN,
    _EXEC_PRIORITY_CN,
    _EXEC_PRIORITY_EN,
    _IDENTITY_BLOCKS_CN,
    _IDENTITY_BLOCKS_EN,
    _INTRO_NAME,
    _INTRO_TITLE_CN,
    _INTRO_TITLE_EN,
    _NAME_EXEC_PRIORITY,
    _NAME_PATH_PERMISSION,
    _PATH_PERMISSION_CN,
    _PATH_PERMISSION_EN,
    _PRIO_ENV_RESIDUAL,
    _PRIO_EXEC_PRIORITY,
    _PRIO_PATH_PERMISSION,
    _SECTION_IDENTITY,
    _WRITE_LOCATION_CN,
    _WRITE_LOCATION_EN,
    _build_env_residual_entity,
    _split_identity,
)

logger = logging.getLogger(__name__)


class FlashPersonaRail(DeepAgentRail):
    """拆分 identity 巨石段为 000/200/210/220/400/420/460（零内容损失）。"""

    # identity 段构造期一次性构建，本 rail 只需在任意一次 before_model_call 拆一次即可；
    # 与 FlashSkillRail（每轮重做 skills 翻牌）不同，identity 不每轮重建，故取任意优先级，
    # 只要不依赖其它 rail 对 identity 的写入。放在 RuntimePromptRail(5) 之后、audit(2) 之前。
    priority = 4
    inherit_to_subagents = False

    def __init__(self) -> None:
        super().__init__()
        self._system_prompt_builder: Any = None
        # 已拆分过的 builder 身份（id）。按 builder 而非实例级 bool 判断：热重载会重建
        # system_prompt_builder 并重新带入完整 identity 巨石段，实例级 flag 会阻止对新
        # builder 再次拆分，导致闪回未拆分的巨石提示词。
        self._split_builder_id: int | None = None

    def init(self, agent: Any) -> None:
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        self._system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = (
            getattr(ctx.agent, "system_prompt_builder", None)
            or self._system_prompt_builder
        )
        self.apply(builder)

    def apply(self, builder: Any) -> None:
        """Apply the identity transform without owning Rail callback registration."""
        if builder is None:
            return
        self._system_prompt_builder = builder
        self._maybe_split_identity(builder)

    def _maybe_split_identity(self, builder: Any) -> None:
        """按 builder 身份幂等拆分：同一 builder 拆一次；新 builder 重新拆分。

        拆分成功后记录 ``id(builder)``，避免 intro 段仍叫 ``identity``（priority 0）
        导致每轮重复命中守卫打 warning；热重载重建 builder 后 ``id`` 变化，会重新
        走拆分流程（新 builder 的 identity 段是重新构造的完整巨石）。
        """
        if self._split_builder_id == id(builder):
            return
        section = self._get_section(builder, _SECTION_IDENTITY)
        if section is None:
            return

        content = self._extract_content(section, builder)
        language = self._resolve_language(builder)
        blocks = _IDENTITY_BLOCKS_CN if language == "cn" else _IDENTITY_BLOCKS_EN
        split = _split_identity(content, blocks)
        if split is None:
            # 拆分成功后已记录 _split_builder_id，后续调用不会走到这里；
            # 能到这里的 None 只有首轮真实守卫命中（标题缺失/乱序）——需告警并保留原生段。
            logger.warning(
                "[FlashPersonaRail] identity 拆分命中守卫（标题缺失/乱序），保留原生 identity 段原样"
            )
            return

        builder.remove_section(_SECTION_IDENTITY)
        intro_title = _INTRO_TITLE_CN if language == "cn" else _INTRO_TITLE_EN
        for name, priority, text in split:
            # 000 身份：intro 原以正文开头、无标题，按语言补 H1（cn「# 身份」/ en「# Identity」）。
            if name == _INTRO_NAME and text.strip() and not text.lstrip().startswith("#"):
                text = intro_title + "\n\n" + text
            # 210 运行环境残留 → 独立实体「Python 库与权限」（一次性裁剪：库清单 + 沙箱权限），
            # 迁到 1001 与 env@1000 并列；丢弃与 env 重复的平台/Shell/命令表。
            elif name == "runtime_environment":
                text = _build_env_residual_entity(text, language)
                priority = _PRIO_ENV_RESIDUAL
            builder.add_section(PromptSection(
                name=name,
                content={language: text},
                priority=priority,
            ))
        self._rewrite_workspace(builder, language)
        self._inject_execution_priority(builder, language)
        self._inject_path_permission(builder, language)
        self._split_builder_id = id(builder)

    def _inject_execution_priority(self, builder: Any, language: str) -> None:
        """020 执行优先级（新段）：六级裁决表，外部门禁清单由 1080 注入（此处仅引用）。"""
        cn = language == "cn"
        builder.add_section(PromptSection(
            name=_NAME_EXEC_PRIORITY,
            content={language: _EXEC_PRIORITY_CN if cn else _EXEC_PRIORITY_EN},
            priority=_PRIO_EXEC_PRIORITY,
        ))

    def _inject_path_permission(self, builder: Any, language: str) -> None:
        """210 路径与权限（新段，补写静态规则）：workdir/cd 语义、工作区外确认、权限拒停。

        运行时「运行环境」事实（identity 拆分出的 runtime_environment）由
        FlashSectionOrderRail 并入 env@1000（并集）后删除，此处不涉及。
        """
        cn = language == "cn"
        builder.add_section(PromptSection(
            name=_NAME_PATH_PERMISSION,
            content={language: _PATH_PERMISSION_CN if cn else _PATH_PERMISSION_EN},
            priority=_PRIO_PATH_PERMISSION,
        ))

    def _rewrite_workspace(self, builder: Any, language: str) -> None:
        """2xx/400 内容改写（§七）：200 四术语表 / 220 优先级链 / 400 执行质量准则。

        在 identity 拆分成功后立即执行（同一 ``_split_builder_id`` 幂等 guard 内），
        故 workspace_model/output_placement/execution_principles 一定存在；仍带存在性守卫
        以防未来拆分块改名。210（runtime_environment）不改——依赖 durable 800/810，本期延期。
        """
        cn = language == "cn"
        replacements = (
            ("workspace_model", 200, _DIRECTORY_MODEL_CN if cn else _DIRECTORY_MODEL_EN),
            ("output_placement", 220, _WRITE_LOCATION_CN if cn else _WRITE_LOCATION_EN),
            ("execution_principles", 400, _EXEC_PRINCIPLES_CN if cn else _EXEC_PRINCIPLES_EN),
        )
        for name, priority, text in replacements:
            if self._get_section(builder, name) is None:
                continue
            builder.add_section(PromptSection(
                name=name,
                content={language: text},
                priority=priority,
            ))

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


__all__ = ["FlashPersonaRail", "_split_identity"]
