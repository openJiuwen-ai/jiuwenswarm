"""面向 agent 的 skill 管理工具封装。"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Callable

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenclaw.agentserver.skill_manager import SkillManager
from jiuwenclaw.config import get_config

logger = logging.getLogger(__name__)


def _skill_toolkit_search_skill_enabled() -> bool:
    """react.skill_toolkit.search_skill_enabled in config.yaml; default True."""
    try:
        raw = (
            get_config()
            .get("react", {})
            .get("skill_toolkit", {})
            .get("search_skill_enabled", False)
        )
    except Exception:  # noqa: BLE001
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)

_AUTO_SOURCE = "auto"
_DEFAULT_SOURCE = "skillnet"
_SUPPORTED_SOURCES = {"skillnet", "clawhub"}
# identifier 对模型是统一字段；这里根据其形态推断底层来源。
_INSTALL_SOURCE_BY_TARGET: tuple[tuple[str, str], ...] = (
    (r"^https?://", "skillnet"),
    (r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", "clawhub"),
)


class SkillToolkit:
    """把 SkillManager 暴露成模型友好的 tool 集合。"""

    def __init__(self, manager: SkillManager) -> None:
        self._manager = manager

    @staticmethod
    def _normalize_source(source: str) -> str:
        value = str(source or _DEFAULT_SOURCE).strip().lower()
        if value in _SUPPORTED_SOURCES or value == _AUTO_SOURCE:
            return value
        raise ValueError(f"unsupported source: {source}")

    @staticmethod
    def _detect_source(target: str) -> str:
        raw = str(target or "").strip()
        if not raw:
            raise ValueError("identifier is required")
        for pattern, source in _INSTALL_SOURCE_BY_TARGET:
            if re.match(pattern, raw):
                return source
        raise ValueError(f"cannot infer source from identifier: {target}")

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(parsed, 1)

    def _get_skill_meta(self, skill_name: str) -> dict[str, Any] | None:
        """从本地技能目录读取解析后的 SKILL.md 元数据。"""
        return self._manager.get_skill_meta(skill_name)

    def _get_installed_names(self) -> set[str]:
        return {str(item.get("name", "")) for item in self._manager.get_installed_plugins()}

    def _find_installed_by_target(self, identifier: str, source: str) -> dict[str, Any] | None:
        """按统一 identifier 反查是否已安装，避免重复安装。"""
        target = str(identifier or "").strip()
        if not target:
            return None

        for item in self._manager.get_local_skills():
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            origin = str(item.get("origin", "")).strip()
            local_source = str(item.get("source", "")).strip()
            if source == "skillnet" and local_source == "skillnet" and origin == target:
                return self._build_installed_item(name, "skillnet")
            if source == "clawhub" and local_source == "clawhub":
                if origin == f"clawhub:{target}" or origin == target or name == target:
                    return self._build_installed_item(name, "clawhub")

        for plugin in self._manager.get_installed_plugins():
            if not isinstance(plugin, dict):
                continue
            name = str(plugin.get("name", "")).strip()
            marketplace = str(plugin.get("marketplace", "")).strip()
            plugin_source = str(plugin.get("source", "")).strip()
            normalized_source = plugin_source or marketplace
            if source == "clawhub" and normalized_source == "clawhub" and name == target:
                return self._build_installed_item(name, "clawhub")
            if source == "skillnet" and normalized_source == "skillnet" and name == target:
                return self._build_installed_item(name, "skillnet")

        return None

    def _is_builtin_skill(self, skill_name: str) -> bool:
        """复用原有卸载语义：只有真正运行在 builtin 目录中的技能才视为内置。"""
        return self._manager.is_builtin_skill(skill_name)

    @staticmethod
    def _normalize_search_item(item: dict[str, Any], source: str, installed_names: set[str]) -> dict[str, Any]:
        """把不同来源的原始搜索结果归一成统一字段。"""
        if source == "skillnet":
            name = str(item.get("skill_name", "")).strip()
            description = str(item.get("skill_description", "")).strip()
            identifier = str(item.get("skill_url", "")).strip()
            version = ""
            author = str(item.get("author", "")).strip()
            score = item.get("stars", 0)
        else:
            name = str(item.get("display_name") or item.get("slug") or "").strip()
            description = str(item.get("summary", "")).strip()
            identifier = str(item.get("slug", "")).strip()
            version = str(item.get("version", "")).strip()
            author = ""
            score = None

        return {
            "name": name,
            "description": description,
            "source": source,
            "identifier": identifier,
            "installed": name in installed_names,
            "version": version,
            "author": author,
            "score": score,
        }

    @staticmethod
    def _summarize_search_payload(source: str, query: str, payload: dict[str, Any]) -> dict[str, Any]:
        """提取一小段调试摘要，便于日志与 tool 返回里排查问题。"""
        skills = payload.get("skills", []) or []
        first = skills[0] if skills else {}
        if not isinstance(first, dict):
            try:
                first = vars(first)
            except Exception:
                first = {"repr": repr(first)}
        return {
            "source": source,
            "query": query,
            "success": bool(payload.get("success")),
            "count": len(skills),
            "detail": str(payload.get("detail", "")).strip(),
            "sample": {
                "skill_name": first.get("skill_name") or first.get("display_name") or first.get("slug") or "",
                "skill_url": first.get("skill_url") or "",
                "summary": first.get("skill_description") or first.get("summary") or "",
            },
        }

    @staticmethod
    def _build_skill_line(name: str, description: str) -> str:
        desc = description.strip() or "No description provided."
        return f"- `{name}`: {desc}"

    def _build_installed_item(self, name: str, source: str) -> dict[str, Any]:
        """补齐已安装 skill 的展示信息，供 list/install 返回复用。"""
        meta = self._get_skill_meta(name) or {}
        description = str(meta.get("description", "")).strip()
        skill_dir = str(meta.get("skill_dir", ""))
        skill_file = str(meta.get("skill_file", ""))
        identifier = ""
        for item in self._manager.get_local_skills():
            if item.get("name") == name:
                identifier = str(item.get("origin", "")).strip()
                break
        if source == "clawhub" and identifier.startswith("clawhub:"):
            identifier = identifier.split(":", 1)[1].strip()
        return {
            "name": name,
            "description": description,
            "source": source,
            "identifier": identifier or name,
            "installed": True,
            "version": str(meta.get("version", "")),
            "author": str(meta.get("author", "")),
            "score": None,
            "skill_dir": skill_dir,
            "skill_file": skill_file,
        }

    async def search_skill(self, query: str, source: str = _DEFAULT_SOURCE, limit: int = 10) -> dict[str, Any]:
        """Search skills from SkillNet and/or ClawHub with a unified response."""
        try:
            normalized_source = self._normalize_source(source)
            query = str(query or "").strip()
            logger.info(
                "[SkillToolkit] search_skill called: query=%r source=%s limit=%s",
                query,
                normalized_source,
                limit,
            )
            if not query:
                return {
                    "success": False,
                    "source": normalized_source,
                    "items": [],
                    "detail": "query is required",
                }

            search_limit = self._safe_int(limit, 10)
            installed_names = self._get_installed_names()
            sources = sorted(_SUPPORTED_SOURCES) if normalized_source == _AUTO_SOURCE else [normalized_source]
            items: list[dict[str, Any]] = []
            errors: list[str] = []
            any_success = False
            for current_source in sources:
                params = {"q": query, "limit": search_limit}
                # SkillNet 的 vector 模式对多关键词查询召回更稳定。
                if current_source == "skillnet":
                    params["mode"] = "vector"
                payload = await (
                    self._manager.handle_skills_skillnet_search(params)
                    if current_source == "skillnet"
                    else self._manager.handle_skills_clawhub_search(params)
                )
                payload_summary = self._summarize_search_payload(current_source, query, payload)
                logger.info(
                    "[SkillToolkit] %s search payload summary: %s",
                    current_source,
                    payload_summary,
                )
                if not payload.get("success"):
                    detail = str(payload.get("detail", "")).strip() or f"{current_source} search failed"
                    errors.append(f"{current_source}: {detail}")
                    continue
                any_success = True
                for raw_item in payload.get("skills", []):
                    items.append(self._normalize_search_item(raw_item, current_source, installed_names))

            detail = "; ".join(errors)
            if not items:
                no_result_detail = (
                    f"No skills found from {normalized_source} for query {query!r}. "
                    "Underlying search returned success but an empty skills list."
                )
                if any_success and detail:
                    detail = f"{no_result_detail} Partial source errors: {detail}"
                elif not detail:
                    detail = no_result_detail

            return {
                "success": any_success,
                "source": normalized_source,
                "items": items[:search_limit],
                "detail": detail,
                "query_summary": f"search query={query!r} source={normalized_source} limit={search_limit}",
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("search_skill failed")
            return {"success": False, "source": str(source), "items": [], "detail": str(exc)}

    async def _install_skillnet_sync_wait(
        self,
        identifier: str,
        timeout_sec: int,
    ) -> dict[str, Any]:
        """在单次 tool 调用内轮询 SkillNet 安装状态，直到完成或超时。"""
        payload = await self._manager.handle_skills_skillnet_install({"url": identifier, "force": False})
        if not payload.get("success"):
            return payload
        if not payload.get("pending"):
            return payload

        install_id = str(payload.get("install_id", "")).strip()
        if not install_id:
            return {"success": False, "detail": "missing install_id from skillnet install"}

        async def _poll_status() -> dict[str, Any]:
            # 复用原有 install_status 轮询接口，直到安装完成或超时。
            while True:
                status_payload = await self._manager.handle_skills_skillnet_install_status(
                    {"install_id": install_id}
                )
                if status_payload.get("status") != "pending":
                    return status_payload
                await asyncio.sleep(0.5)

        try:
            final_payload = await asyncio.wait_for(_poll_status(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            return {
                "success": False,
                "detail": f"skill installation timed out after {timeout_sec} seconds",
            }

        if not final_payload.get("success"):
            return final_payload
        return {
            "success": True,
            "skill": final_payload.get("skill"),
        }

    async def install_skill(
        self,
        identifier: str,
        source: str,
        timeout_sec: int = 60,
    ) -> dict[str, Any]:
        """Install a skill with an explicit source and wait for completion when needed."""
        try:
            target = str(identifier or "").strip()
            raw_source = str(source or "").strip()
            logger.info(
                "[SkillToolkit] install_skill called: identifier=%r source=%s timeout_sec=%s",
                target,
                raw_source,
                timeout_sec,
            )
            if not target:
                return {
                    "success": False,
                    "source": raw_source,
                    "installed": False,
                    "detail": "identifier is required",
                }
            if not raw_source:
                return {
                    "success": False,
                    "source": raw_source,
                    "installed": False,
                    "detail": "source is required and must be either 'skillnet' or 'clawhub'",
                }
            normalized_source = self._normalize_source(raw_source)
            if normalized_source == _AUTO_SOURCE:
                return {
                    "success": False,
                    "source": normalized_source,
                    "installed": False,
                    "detail": "source must be explicitly set to 'skillnet' or 'clawhub'",
                }

            resolved_source = normalized_source
            wait_timeout = self._safe_int(timeout_sec, 60)
            existing_item = self._find_installed_by_target(target, resolved_source)
            if existing_item is not None:
                detail = (
                    f"Skill `{existing_item['name']}` is already installed. "
                    "Skipping duplicate installation."
                )
                return {
                    "success": True,
                    "source": resolved_source,
                    "installed": True,
                    "already_installed": True,
                    "name": existing_item["name"],
                    "description": existing_item["description"],
                    "identifier": existing_item["identifier"],
                    "skill_file": existing_item["skill_file"],
                    "detail": detail,
                }

            if resolved_source == "skillnet":
                payload = await self._install_skillnet_sync_wait(target, wait_timeout)
            else:
                payload = await self._manager.handle_skills_clawhub_download({"slug": target, "force": False})
        except Exception as exc:  # noqa: BLE001
            logger.exception("install_skill failed")
            return {
                "success": False,
                "source": str(source),
                "installed": False,
                "detail": str(exc),
            }

        if not payload.get("success"):
            return {
                "success": False,
                "source": resolved_source,
                "installed": False,
                "detail": str(payload.get("detail", "")).strip() or "skill installation failed",
            }

        skill = payload.get("skill") or {}
        name = str(skill.get("name", "")).strip()
        if not name:
            # 底层未显式返回名称时，尽量从 identifier 推断一个稳定值。
            name = Path(target).name if resolved_source == "skillnet" else target

        installed_item = self._build_installed_item(name, resolved_source)
        detail = (
            f"Skill installed successfully. Available now: - `{installed_item['name']}`: "
            f"{installed_item['description'].strip() or 'No description provided.'}"
        )
        if installed_item["skill_file"]:
            detail = f"{detail} Read SKILL.md before use."
        logger.info(
            "[SkillToolkit] install_skill succeeded: name=%s source=%s local_path=%s",
            installed_item["name"],
            resolved_source,
            installed_item["skill_dir"],
        )
        return {
            "success": True,
            "source": resolved_source,
            "installed": True,
            "name": installed_item["name"],
            "description": installed_item["description"],
            "identifier": installed_item["identifier"],
            "skill_file": installed_item["skill_file"],
            "detail": detail,
        }

    async def uninstall_skill(self, name: str) -> dict[str, Any]:
        """Uninstall a skill by name."""
        try:
            skill_name = str(name or "").strip()
            logger.info("[SkillToolkit] uninstall_skill called: name=%r", skill_name)
            if not skill_name:
                return {"success": False, "removed": False, "detail": "name is required"}
            if self._is_builtin_skill(skill_name):
                return {
                    "success": False,
                    "removed": False,
                    "name": skill_name,
                    "detail": "Built-in skills cannot be uninstalled.",
                }

            installed_payload = await self._list_installed_skills()
            if not installed_payload.get("success"):
                return {
                    "success": False,
                    "removed": False,
                    "name": skill_name,
                    "detail": str(installed_payload.get("detail", "")).strip() or "failed to inspect installed skills",
                }

            installed_item = None
            for item in installed_payload.get("items", []):
                if item.get("name") == skill_name:
                    installed_item = item
                    break
            if installed_item is None:
                return {
                    "success": False,
                    "removed": False,
                    "name": skill_name,
                    "detail": f"Skill `{skill_name}` is not installed.",
                }

            payload = await self._manager.handle_skills_uninstall({"name": skill_name})
        except Exception as exc:  # noqa: BLE001
            logger.exception("uninstall_skill failed")
            return {
                "success": False,
                "removed": False,
                "name": str(name or "").strip(),
                "detail": str(exc),
            }

        if not payload.get("success"):
            return {
                "success": False,
                "removed": False,
                "name": skill_name,
                "detail": str(payload.get("detail", "")).strip() or "skill uninstall failed",
            }

        return {
            "success": True,
            "removed": True,
            "name": skill_name,
            "source": installed_item.get("source", "") if installed_item else "",
            "detail": f"Skill `{skill_name}` uninstalled successfully.",
        }

    async def _list_installed_skills(self) -> dict[str, Any]:
        """列出已安装 skills，供 toolkit 内部逻辑复用。"""
        try:
            logger.info("[SkillToolkit] _list_installed_skills called")
            payload = await self._manager.handle_skills_installed({})
            if not payload.get("plugins"):
                return {"success": True, "items": [], "detail": ""}

            items: list[dict[str, Any]] = []
            for plugin in payload.get("plugins", []):
                name = str(plugin.get("plugin_name", "")).strip()
                source = str(plugin.get("marketplace", "")).strip() or "local"
                items.append(self._build_installed_item(name, source))
            return {"success": True, "items": items, "detail": ""}
        except Exception as exc:  # noqa: BLE001
            logger.exception("list_installed_skills failed")
            return {"success": False, "items": [], "detail": str(exc)}

    def get_tools(self) -> list[Tool]:
        """Return skill-management tools for agent registration."""

        def make_tool(name: str, description: str, input_params: dict, func: Callable[..., Any]) -> Tool:
            # 统一用 LocalFunction 包装，保持与现有 toolkit 注册方式一致。
            card = ToolCard(
                id=name,
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        tools: list[Tool] = []
        if _skill_toolkit_search_skill_enabled():
            tools.append(
                make_tool(
                    name="search_skill",
                    description=(
                        "Search installable skills from SkillNet and ClawHub. "
                        "Use the returned identifier with install_skill."
                    ),
                    input_params={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query for the skill."},
                            "source": {
                                "type": "string",
                                "enum": ["auto", "skillnet", "clawhub"],
                                "description": (
                                    "Skill source to search. Defaults to skillnet. "
                                    "Use auto to search both sources."
                                ),
                                "default": "skillnet",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of skills to return.",
                                "default": 10,
                            },
                        },
                        "required": ["query"],
                    },
                    func=self.search_skill,
                )
            )
        else:
            logger.info(
                "[SkillToolkit] search_skill not registered (react.skill_toolkit.search_skill_enabled=false)"
            )

        tools.extend(
            [
            make_tool(
                name="install_skill",
                description=(
                    "Install a skill using the identifier returned by search_skill. "
                    "Returns the installed skill summary and where to read SKILL.md."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "identifier": {
                            "type": "string",
                            "description": "Source-agnostic identifier returned by search_skill.",
                        },
                        "source": {
                            "type": "string",
                            "enum": ["skillnet", "clawhub"],
                            "description": "Explicit source of the identifier returned by search_skill.",
                        },
                        "timeout_sec": {
                            "type": "integer",
                            "description": "Installation timeout in seconds.",
                            "default": 60,
                        },
                    },
                    "required": ["identifier", "source"],
                },
                func=self.install_skill,
            ),
            make_tool(
                name="uninstall_skill",
                description="Uninstall an installed skill by name.",
                input_params={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Installed skill name to remove."},
                    },
                    "required": ["name"],
                },
                func=self.uninstall_skill,
            ),
            ]
        )
        return tools
