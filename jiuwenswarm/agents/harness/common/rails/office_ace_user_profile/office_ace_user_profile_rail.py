# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""OfficeAceUserProfileRail -- jiuwenswarm product-side rail.

每轮 ``before_model_call`` 从本地缓存读取云端用户画像概览，注入
``office_ace_user_profile`` prompt 段。缓存文件不存在时同步触发一次拉取
（带超时，不阻塞对话）。

后台周期任务在 ``init`` 启动、``uninit`` 取消，每 N 分钟拉一次概览，
原子写缓存（tmp→rename）。

本 rail 住在 jiuwenswarm（非 agent-core），与 ProjectMemoryRail 同构。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.office_ace_user_profile.fetcher import (
    UserProfileConfig,
    UserProfileFetcher,
)
from jiuwenswarm.agents.harness.common.rails.office_ace_user_profile.section import (
    SECTION_NAME,
    build_office_ace_user_profile_section,
)
from jiuwenswarm.common.utils import logger

if TYPE_CHECKING:
    from openjiuwen.harness.deep_agent import DeepAgent


class OfficeAceUserProfileRail(DeepAgentRail):
    """Inject the cloud-side user profile (memory overview) into the system prompt.

    Loaded source: ``~/.office-claw/users/<userId>/user-profile.md``, written
    by :class:`UserProfileFetcher` (on-demand sync fetch in ``before_model_call``
    when the local cache is missing or stale).

    Priority 130 — higher than ProjectMemoryRail (120), lower than runtime prompt
    rails, so the user profile sits close to the agent's runtime identity.
    """

    SECTION_PRIORITY = 130

    def __init__(
        self,
        config: UserProfileConfig,
        *,
        cache_dir: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self._fetcher = UserProfileFetcher(config, cache_dir=cache_dir)
        self._system_prompt_builder: Optional[object] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(self, agent: "DeepAgent") -> None:
        """绑定 system_prompt_builder。后台周期拉取由进程级 manager 负责,不在此启动。"""
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        if self._system_prompt_builder is None:
            logger.warning(
                "[OfficeAceUserProfileRail] agent has no system_prompt_builder; "
                "prompt injection disabled",
            )

    def uninit(self, agent: "DeepAgent") -> None:
        """清段。无后台周期任务，仅需移除注入的 prompt 段。"""
        if self._system_prompt_builder is not None:
            try:
                self._system_prompt_builder.remove_section(SECTION_NAME)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Hook
    # ------------------------------------------------------------------

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Refresh the ``office_ace_user_profile`` section from cache."""
        if self._system_prompt_builder is None:
            return

        # 先读本地缓存。缓存新鲜（距上次拉取 < interval）直接用，不拉云端。
        # 缓存不存在或已过期才同步触发一次拉取（带超时，不阻塞对话）。
        # 拉取失败/业务态无内容时降级用过期缓存（可能略旧）而非清空，零阻断。
        content = self._fetcher.read_cache()
        if content is None or not self._fetcher.is_cache_fresh():
            stale = content  # 旧缓存（可能为 None），fetch 失败时降级用
            try:
                content = await asyncio.wait_for(
                    self._fetcher.fetch_once(),
                    timeout=self._fetcher.config.timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.info(
                    "[OfficeAceUserProfileRail] sync fetch timed out (user=%s); "
                    "falling back to stale cache",
                    self._fetcher.config.user_id,
                )
                content = stale
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[OfficeAceUserProfileRail] sync fetch failed (user=%s): %s; "
                    "falling back to stale cache",
                    self._fetcher.config.user_id,
                    exc,
                )
                content = stale
            else:
                if content is None and stale is not None:
                    # fetch 返回 None（业务态 403/404 等）→ 降级用过期缓存
                    content = stale

        # 总是先移除旧段，让当前缓存状态生效
        try:
            self._system_prompt_builder.remove_section(SECTION_NAME)
        except Exception:  # noqa: BLE001
            pass

        if not content or not content.strip():
            # 无内容 → 零残留（不注入段）
            return

        # 截断到 max_chars
        max_chars = self._fetcher.config.max_chars
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n[...已截断...]"

        section = build_office_ace_user_profile_section(
            content,
            priority=self.SECTION_PRIORITY,
        )
        if section is not None:
            try:
                self._system_prompt_builder.add_section(section)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[OfficeAceUserProfileRail] add_section failed: %s",
                    exc,
                )


__all__ = ["OfficeAceUserProfileRail"]
