# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""IdentityRail -- dynamically inject IDENTITY.md content as the identity section.

On every ``before_model_call``, read the agent workspace's ``IDENTITY.md``.
If the file has content, override the static "identity" section with it so
the agent introduces itself using the user-defined identity. If the file is
missing or empty, restore the default identity section from the system prompt.

Follows the same always-remove-then-add pattern as ProjectMemoryRail so
disk state wins every turn, including mid-session edits.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.sections.context import (
    _identity_has_filled_name,
    _is_unfilled_template,
)
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.prompt.prompt_builder import (
    PromptPriority,
    _identity_prompt,
)
from jiuwenswarm.common.utils import logger

if TYPE_CHECKING:
    from openjiuwen.harness.deep_agent import DeepAgent

_SECTION_NAME = "identity"
_SECTION_PRIORITY = PromptPriority.IDENTITY  # 10


class IdentityRail(DeepAgentRail):
    """Override the identity section with IDENTITY.md content when available.

    On each ``before_model_call``:
    1. Remove the existing "identity" section.
    2. Read ``IDENTITY.md`` from the agent workspace.
    3. If the file has content, add a new "identity" section with it.
    4. If not, add the default identity section back.

    This ensures the agent always reflects the latest IDENTITY.md state.

    Yield rule (expert persona guard): the rail only manages identity sections
    it recognizes — the default static identity (same factory as
    ``build_shared_identity_section``) or one installed by itself from
    IDENTITY.md.  If the current identity section is anything else (e.g. an
    expert persona hot-bound via ``load_agent_template`` with
    ``replace_existing=True``), the rail leaves it untouched: IDENTITY.md is
    meant to win over the *default* assistant identity, never over an
    explicitly loaded expert.  After the expert is unloaded, the unbind
    snapshot restore brings back a rail-managed section and the rail resumes.
    """

    def __init__(
            self,
            *,
            language: str = "en",
            identity_md_path: str | None = None,
    ) -> None:
        super().__init__()
        self._language: str = language
        self._identity_md_path: str | None = identity_md_path
        self._system_prompt_builder = None
        self._default_section: PromptSection | None = None
        # 本 rail 从 IDENTITY.md 装上的 identity section 内容（判等用）；
        # 与 _default_section 一起构成「可覆盖」集合，其余 identity 一律让路。
        self._installed_section_content: dict | None = None
        self._foreign_identity_yield_logged = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(self, agent: "DeepAgent") -> None:
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        if self._system_prompt_builder is None:
            logger.warning(
                "[IdentityRail] agent has no system_prompt_builder; disabled"
            )
            return
        self._default_section = _identity_prompt()
        logger.info(
            "[IdentityRail] initialized, identity_md_path=%s language=%s",
            self._resolve_identity_md_path(),
            self._language,
        )

    def uninit(self, agent: "DeepAgent") -> None:
        _ = agent
        if self._system_prompt_builder is not None and self._default_section is not None:
            self._system_prompt_builder.add_section(self._default_section)
        self._system_prompt_builder = None

    # ------------------------------------------------------------------
    # Public knobs
    # ------------------------------------------------------------------

    def set_language(self, language: str) -> None:
        """Per-request language switch (cn/en)."""
        if language and language != self._language:
            self._language = language
            self._default_section = _identity_prompt()

    def set_identity_md_path(self, path: str | None) -> None:
        """Per-request hot update of the IDENTITY.md file path."""
        self._identity_md_path = path

    # ------------------------------------------------------------------
    # Hook
    # ------------------------------------------------------------------

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Refresh the identity section from IDENTITY.md."""
        _ = ctx
        if self._system_prompt_builder is None:
            return

        current = self._system_prompt_builder.get_section(_SECTION_NAME)
        if current is not None and not self._is_managed_identity(current):
            # 专家 persona 等显式绑定的 identity：只让 IDENTITY.md 赢过默认身份，
            # 不赢过显式装载的专家——整段让路，专家在场期间不干预。
            if not self._foreign_identity_yield_logged:
                self._foreign_identity_yield_logged = True
                logger.info(
                    "[IdentityRail] identity section held by an external binding "
                    "(e.g. expert persona); yield until it is released"
                )
            return

        self._system_prompt_builder.remove_section(_SECTION_NAME)

        content = self._read_identity_md()

        if content:
            section = PromptSection(
                name=_SECTION_NAME,
                content={self._language: content},
                priority=_SECTION_PRIORITY,
            )
            self._system_prompt_builder.add_section(section)
            self._installed_section_content = dict(section.content)
        elif self._default_section is not None:
            self._installed_section_content = None
            self._system_prompt_builder.add_section(self._default_section)
        else:
            self._installed_section_content = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _is_managed_identity(self, section: PromptSection) -> bool:
        """该 identity section 是否归本 rail 管理（默认身份或自己装上的）。"""
        if (
                self._default_section is not None
                and section.content == self._default_section.content
        ):
            return True
        installed = self._installed_section_content
        return installed is not None and section.content == installed

    def _resolve_identity_md_path(self) -> Path:
        if self._identity_md_path:
            return Path(self._identity_md_path)
        from jiuwenswarm.common.utils import get_deepagent_identity_md_path

        return get_deepagent_identity_md_path()

    def _read_identity_md(self) -> str | None:
        """Read IDENTITY.md; return stripped content or None.

        Returns None (→ caller falls back to default identity section) when:
        - file missing or read fails
        - file is empty
        - content is an unfilled workspace template (placeholder text only)
          AND has no filled ``名字`` / ``Name`` field

        Returns the raw content when:
        - the file has a filled ``名字`` / ``Name`` field (even if template
          guidance text is still present — users often fill in the name but
          leave the original template lines intact)
        - the file has substantive content even without a recognizable
          ``名字:`` field (e.g. free-form identity description)

        Template detection reuses openjiuwen's
        :func:`_is_unfilled_template` and :func:`_identity_has_filled_name`
        so IdentityRail stays aligned with ContextAssembleRail's handling
        of the same IDENTITY.md file.
        """
        try:
            path = self._resolve_identity_md_path()
            if not path.exists():
                return None
            raw = path.read_text(encoding="utf-8", errors="replace").strip()
            if not raw:
                return None
            # 先查名字：填了真实名字就直接采用，即使保留了模板引导行。
            # 这修复了用户按模板填名字但没删引导语被误判为"未填模板"的问题。
            if _identity_has_filled_name(raw):
                return raw
            # 没填名字 + 还是默认模板原样 → 回退默认身份段。
            if _is_unfilled_template(raw):
                logger.debug(
                    "[IdentityRail] IDENTITY.md is the default template with "
                    "no filled name; using default identity section"
                )
                return None
            # 没填名字但有实质内容（非模板） → 采用。
            return raw
        except Exception as exc:
            logger.error("[IdentityRail] failed to read IDENTITY.md: %s", exc)
            return None


__all__ = ["IdentityRail"]
