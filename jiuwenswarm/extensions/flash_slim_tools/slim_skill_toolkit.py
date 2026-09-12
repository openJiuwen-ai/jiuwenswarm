# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SlimSkillToolkit — search_skill with install/uninstall folded in.

Subclass of the stock ``SkillToolkit``:
- ``search_skill`` gains an ``install`` param (default True) that auto-installs
  the best match after search
- ``get_tools()`` returns only ``search_skill`` (install_skill / uninstall_skill
  are no longer registered as separate tools)

The extension patches the adapter's ``_get_tool_cards`` to use this subclass
instead of the stock ``SkillToolkit``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.agents.harness.common.tools.skill_toolkits import SkillToolkit

logger = logging.getLogger(__name__)


class SlimSkillToolkit(SkillToolkit):
    """SkillToolkit with install/uninstall folded into search_skill."""

    async def search_skill(
        self,
        query: str,
        source: str = "skillnet",
        limit: int = 10,
        install: bool = True,
    ) -> Dict[str, Any]:
        """Search skills and auto-install the best match by default."""
        try:
            result = await super().search_skill(query, source=source, limit=limit)
            if install and result.get("items"):
                result["install"] = await self._auto_install_best_match(
                    result["items"]
                )
            elif install:
                result["install"] = {
                    "attempted": False,
                    "detail": "no search results to install",
                }
            return result
        except Exception as exc:
            logger.exception("SlimSkillToolkit.search_skill failed")
            return {"success": False, "source": str(source), "items": [], "detail": str(exc)}

    async def _auto_install_best_match(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Auto-install the first not-yet-installed item with a non-empty identifier."""
        top = None
        for item in items:
            if item.get("installed"):
                continue
            if str(item.get("identifier") or "").strip():
                top = item
                break
        if top is None:
            first = items[0]
            return {
                "attempted": False,
                "already_installed": True,
                "name": first.get("name", ""),
                "detail": (
                    f"Skill `{first.get('name', '')}` is already installed; "
                    "nothing new to install."
                ),
            }
        install_result = await self.install_skill(
            identifier=str(top["identifier"]),
            source=str(top["source"]),
            timeout_sec=60,
        )
        install_result["attempted"] = True
        install_result["query_rank"] = items.index(top) + 1
        return install_result

    def get_tools(self) -> List[Tool]:
        """Return only the merged search_skill tool (with install param)."""

        def make_tool(name: str, description: str, input_params: dict, func) -> Tool:
            card = ToolCard(
                id=name,
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                name="search_skill",
                description=(
                    "搜索 SkillNet、ClawHub、TeamSkillsHub 及本地内置（builtin）目录中的可安装技能。"
                    "默认自动安装最佳匹配（返回的 install 块报告安装结果，已安装技能自动跳过；"
                    "install=false 时仅搜索，用于对比多个候选）。"
                    "安装后用 skill_tool 读取其 SKILL.md 再使用。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query for the skill."},
                        "source": {
                            "type": "string",
                            "enum": ["auto", "skillnet", "clawhub", "teamskillshub", "builtin"],
                            "description": (
                                "Skill source to search. Defaults to skillnet. "
                                "Use auto to search all sources including builtin. "
                                "Use builtin to search locally available builtin skills."
                            ),
                            "default": "skillnet",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of skills to return.",
                            "default": 10,
                        },
                        "install": {
                            "type": "boolean",
                            "description": (
                                "Install the best match automatically. Default true; "
                                "set false for a pure search."
                            ),
                            "default": True,
                        },
                    },
                    "required": ["query"],
                },
                func=self.search_skill,
            ),
        ]


__all__ = ["SlimSkillToolkit"]
