"""面向 agent 的 skill 管理工具封装。"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Callable

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

logger = logging.getLogger(__name__)

_AUTO_SOURCE = "auto"
_DEFAULT_SOURCE = _AUTO_SOURCE
_SEARCHABLE_SOURCES = {"clawhub", "teamskillshub", "builtin"}
_SUPPORTED_SOURCES = {"clawhub", "teamskillshub", "builtin"}
# identifier 对模型是统一字段；这里根据其形态推断底层来源。
_INSTALL_SOURCE_BY_TARGET: tuple[tuple[str, str], ...] = (
    (r"^https?://", "skillnet"),
    (r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", "clawhub"),
)


def _parse_skill_md_from_path(md_path: Path) -> dict[str, Any] | None:
    """Parse a SKILL.md file and return metadata dict (name, description, etc.).

    Used by ``_search_builtin_skills`` to parse builtin skills that are not
    in the user's local skills directory (where ``get_skill_meta`` would
    return None).  This avoids accessing the protected ``_parse_skill_md``
    method on ``SkillManager`` from outside the class (G.CLS.11).
    """
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception:
        return None
    meta: dict[str, Any] = {}
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if fm_match:
        body = fm_match.group(2).strip()
        try:
            import yaml

            loaded = yaml.safe_load(fm_match.group(1))
            if isinstance(loaded, dict):
                meta = {str(k): v for k, v in loaded.items()}
        except Exception:
            logger.debug("SKILL.md frontmatter YAML parse failed: %s", md_path)
        meta.setdefault("description", body[:500])
        meta.setdefault("name", md_path.parent.name)
    else:
        meta["name"] = md_path.parent.name
        meta["description"] = text[:500]
    return meta


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
        return {
            str(item.get("name", "")) for item in self._manager.get_installed_plugins()
        }

    def _search_builtin_skills(
        self,
        query: str,
        installed_names: set[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        """从内置技能目录中模糊匹配 query，返回未安装的内置技能列表。"""
        from jiuwenswarm.common.utils import (
            get_builtin_skills_dir,
            get_agent_skills_dir,
        )

        builtin_dir = get_builtin_skills_dir()
        user_skills_dir = get_agent_skills_dir()
        if not builtin_dir.exists():
            return []

        query_lower = query.lower()
        results: list[dict[str, Any]] = []

        for child in builtin_dir.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            user_skill_path = user_skills_dir / child.name
            if user_skill_path.exists() and user_skill_path.is_dir():
                continue

            meta = self._manager.get_skill_meta(child.name)
            if meta is None:
                md_path = child / "SKILL.md"
                if md_path.exists():
                    parsed = _parse_skill_md_from_path(md_path)
                    if parsed is None:
                        continue
                    meta = parsed
                    meta.setdefault("name", child.name)
                else:
                    continue

            name = str(meta.get("name", child.name))
            description = str(meta.get("description", ""))
            if (
                query_lower not in name.lower()
                and query_lower not in description.lower()
            ):
                continue

            results.append(
                {
                    "name": name,
                    "description": description[:80],
                    "source": "builtin",
                    "identifier": name,
                    "installed": name in installed_names,
                    "is_builtin": True,
                    "is_builtin_source": True,
                    "score": None,
                }
            )

            if len(results) >= limit:
                break

        return results

    def _find_installed_by_target(
        self, identifier: str, source: str
    ) -> dict[str, Any] | None:
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
                # target 可为 owner/slug 或旧版纯 slug；禁止用展示名匹配，避免同名误判
                # 磁盘按 slug 唯一，故 owner/slug ↔ slug / 旧 origin 需互通，否则
                # Agent 纯 slug 安装会从 already_installed 退化为 download 失败。
                origin_cf = origin.casefold()
                target_cf = target.casefold()
                if origin_cf == f"clawhub:{target_cf}" or origin_cf == target_cf:
                    return self._build_installed_item(name, "clawhub")
                slug_cf = target_cf.rsplit("/", 1)[-1]
                if slug_cf and (
                    origin_cf == f"clawhub:{slug_cf}"
                    or origin_cf.endswith(f"/{slug_cf}")
                ):
                    return self._build_installed_item(name, "clawhub")
            if source == "teamskillshub" and local_source == "teamskillshub":
                if (
                    origin == f"teamskillshub:{target}"
                    or origin == target
                    or name == target
                ):
                    return self._build_installed_item(name, "teamskillshub")

        for plugin in self._manager.get_installed_plugins():
            if not isinstance(plugin, dict):
                continue
            name = str(plugin.get("name", "")).strip()
            marketplace = str(plugin.get("marketplace", "")).strip()
            plugin_source = str(plugin.get("source", "")).strip()
            normalized_source = plugin_source or marketplace
            # ClawHub 不按插件名匹配：同名不同发布者会误判已安装
            if (
                source == "skillnet"
                and normalized_source == "skillnet"
                and name == target
            ):
                return self._build_installed_item(name, "skillnet")
            if (
                source == "teamskillshub"
                and normalized_source == "teamskillshub"
                and name == target
            ):
                return self._build_installed_item(name, "teamskillshub")

        return None

    def _check_already_installed(
        self, identifier: str, source: str
    ) -> dict[str, Any] | None:
        existing_item = self._find_installed_by_target(identifier, source)
        if existing_item is None:
            return None
        detail = (
            f"Skill `{existing_item['name']}` is already installed. "
            "Skipping duplicate installation."
        )
        return {
            "success": True,
            "source": source,
            "installed": True,
            "already_installed": True,
            "name": existing_item["name"],
            "description": existing_item["description"],
            "identifier": existing_item["identifier"],
            "skill_file": existing_item["skill_file"],
            "detail": detail,
        }

    def _is_builtin_skill(self, skill_name: str) -> bool:
        """复用原有卸载语义：只有真正运行在 builtin 目录中的技能才视为内置。"""
        return self._manager.is_builtin_skill(skill_name)

    @staticmethod
    def _normalize_search_item(
        item: dict[str, Any], source: str, installed_names: set[str]
    ) -> dict[str, Any]:
        """把不同来源的原始搜索结果归一成统一字段。"""
        if source == "skillnet":
            name = str(item.get("skill_name", "")).strip()
            description = str(item.get("skill_description", "")).strip()
            identifier = str(item.get("skill_url", "")).strip()
            version = ""
            author = str(item.get("author", "")).strip()
            score = item.get("stars", 0)
        elif source == "teamskillshub":
            asset_id = str(item.get("asset_id", "")).strip()
            name = str(item.get("display_name") or item.get("name") or asset_id).strip()
            description = str(item.get("summary", "")).strip()
            identifier = asset_id
            version = str(item.get("version", "")).strip()
            author = ""
            score = None
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
    def _summarize_search_payload(
        source: str, query: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
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
                "skill_name": first.get("skill_name")
                or first.get("display_name")
                or first.get("slug")
                or first.get("name")
                or "",
                "skill_url": first.get("skill_url") or "",
                "asset_id": first.get("asset_id") or "",
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
        display_name = ""
        for item in self._manager.get_local_skills():
            if item.get("name") == name:
                identifier = str(item.get("origin", "")).strip()
                display_name = str(item.get("display_name") or "").strip()
                break
        if not display_name:
            for plugin in self._manager.get_installed_plugins():
                if plugin.get("name") == name:
                    display_name = str(plugin.get("display_name") or "").strip()
                    break
        out_source = source
        if out_source == "clawhub" and identifier.startswith("clawhub:"):
            identifier = identifier.split(":", 1)[1].strip()
        if out_source == "teamskillshub" and identifier.startswith("teamskillshub:"):
            identifier = identifier.split(":", 1)[1].strip()
        return {
            "name": name,
            "display_name": display_name or name,
            "description": description,
            "source": out_source,
            "identifier": identifier or name,
            "installed": True,
            "version": str(meta.get("version", "")),
            "author": str(meta.get("author", "")),
            "score": None,
            "skill_dir": skill_dir,
            "skill_file": skill_file,
        }

    @staticmethod
    def _match_installed_item(
        items: list[dict[str, Any]], query: str
    ) -> dict[str, Any] | None:
        """按内部名 / 展示名匹配已安装技能（大小写不敏感）。

        UI 可能展示 ClawHub 的 ``Weather``，而磁盘规范名是 ``weather``；
        Agent 也可能任一侧传入。必须同时覆盖，否则会误报未安装。
        """
        needle = str(query or "").strip()
        if not needle:
            return None
        needle_cf = needle.casefold()
        for item in items:
            name = str(item.get("name") or "").strip()
            display_name = str(item.get("display_name") or "").strip()
            if name == needle or display_name == needle:
                return item
            if name.casefold() == needle_cf or display_name.casefold() == needle_cf:
                return item
        return None

    async def search_skill(
        self, query: str, source: str = _DEFAULT_SOURCE, limit: int = 10
    ) -> dict[str, Any]:
        """Search skills from enabled online sources and the builtin catalog."""
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
            sources = (
                sorted(_SEARCHABLE_SOURCES)
                if normalized_source == _AUTO_SOURCE
                else [normalized_source]
            )
            items: list[dict[str, Any]] = []
            errors: list[str] = []
            any_success = False
            for current_source in sources:
                if current_source == "builtin":
                    builtin_items = self._search_builtin_skills(
                        query, installed_names, search_limit
                    )
                    if builtin_items:
                        any_success = True
                        items.extend(builtin_items)
                    continue
                params = {"q": query, "limit": search_limit}
                # SkillNet 的 vector 模式对多关键词查询召回更稳定。
                if current_source == "skillnet":
                    params["mode"] = "vector"
                if current_source == "skillnet":
                    payload = await self._manager.handle_skills_skillnet_search(params)
                elif current_source == "clawhub":
                    payload = await self._manager.handle_skills_clawhub_search(params)
                elif current_source == "teamskillshub":
                    payload = await self._manager.handle_skills_team_skills_hub_search(
                        params
                    )
                else:
                    raise AssertionError(f"unexpected search source: {current_source}")
                payload_summary = self._summarize_search_payload(
                    current_source, query, payload
                )
                logger.info(
                    "[SkillToolkit] %s search payload summary: %s",
                    current_source,
                    payload_summary,
                )
                if not payload.get("success"):
                    detail = (
                        str(payload.get("detail", "")).strip()
                        or f"{current_source} search failed"
                    )
                    errors.append(f"{current_source}: {detail}")
                    continue
                any_success = True
                for raw_item in payload.get("skills", []):
                    items.append(
                        self._normalize_search_item(
                            raw_item, current_source, installed_names
                        )
                    )

            detail = "; ".join(errors)
            if not items:
                no_result_detail = (
                    f"No installable skill matched {query!r} in {normalized_source}."
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
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("search_skill failed")
            return {
                "success": False,
                "source": str(source),
                "items": [],
                "detail": str(exc),
            }

    async def _install_skillnet_sync_wait(
        self,
        identifier: str,
        timeout_sec: int,
    ) -> dict[str, Any]:
        """在单次 tool 调用内轮询 SkillNet 安装状态，直到完成或超时。"""
        payload = await self._manager.handle_skills_skillnet_install(
            {"url": identifier, "force": False}
        )
        if not payload.get("success"):
            return payload
        if not payload.get("pending"):
            return payload

        install_id = str(payload.get("install_id", "")).strip()
        if not install_id:
            return {
                "success": False,
                "detail": "missing install_id from skillnet install",
            }

        async def _poll_status() -> dict[str, Any]:
            # 复用原有 install_status 轮询接口，直到安装完成或超时。
            while True:
                status_payload = (
                    await self._manager.handle_skills_skillnet_install_status(
                        {"install_id": install_id}
                    )
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
                    "detail": (
                        "source is required; "
                        "must be one of: "
                        "'clawhub', "
                        "'teamskillshub', or 'builtin'"
                    ),
                }
            normalized_source = self._normalize_source(raw_source)
            if normalized_source == _AUTO_SOURCE:
                return {
                    "success": False,
                    "source": normalized_source,
                    "installed": False,
                    "detail": (
                        "source must be explicitly set "
                        "to 'clawhub', "
                        "'teamskillshub', or 'builtin'"
                    ),
                }

            resolved_source = normalized_source
            wait_timeout = self._safe_int(timeout_sec, 60)

            if resolved_source == "skillnet":
                r = self._check_already_installed(target, resolved_source)
                if r is not None:
                    return r
                payload = await self._install_skillnet_sync_wait(target, wait_timeout)
            elif resolved_source == "teamskillshub":
                r = self._check_already_installed(target, resolved_source)
                if r is not None:
                    return r
                payload = await self._manager.handle_skills_team_skills_hub_install(
                    {"asset_id": target, "force": False}
                )
            elif resolved_source == "builtin":
                payload = await self._manager.handle_skills_install_builtin(
                    {"name": target}
                )
            else:
                clawhub_slug = target
                clawhub_owner = ""
                if "/" in target:
                    clawhub_owner, _, clawhub_slug = target.partition("/")
                check_id = (
                    f"{clawhub_owner}/{clawhub_slug}" if clawhub_owner else clawhub_slug
                )
                r = self._check_already_installed(check_id, resolved_source)
                if r is not None:
                    return r
                payload = await self._manager.handle_skills_clawhub_download(
                    {
                        "slug": clawhub_slug,
                        "owner_handle": clawhub_owner,
                        "force": False,
                    }
                )
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
                "detail": str(payload.get("detail", "")).strip()
                or "skill installation failed",
            }

        skill = payload.get("skill") or {}
        name = str(skill.get("name", "")).strip()
        if not name:
            # 底层未显式返回名称时，尽量从 identifier 推断一个稳定值。
            name = Path(target).name if resolved_source == "skillnet" else target

        installed_item = self._build_installed_item(name, resolved_source)
        detail = (
            f"Skill `{installed_item['name']}` installed successfully: "
            f"{installed_item['description'].strip() or 'No description provided.'}"
        )
        if installed_item["skill_file"]:
            detail = (
                f"{detail} Load its instructions with "
                f"`skill_tool(skill_name={installed_item['name']!r})` before execution."
            )
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
                return {
                    "success": False,
                    "removed": False,
                    "detail": "name is required",
                }
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
                    "detail": str(installed_payload.get("detail", "")).strip()
                    or "failed to inspect installed skills",
                }

            installed_item = self._match_installed_item(
                list(installed_payload.get("items") or []),
                skill_name,
            )
            if installed_item is None:
                return {
                    "success": False,
                    "removed": False,
                    "name": skill_name,
                    "detail": f"Skill `{skill_name}` is not installed.",
                }

            # 实际卸载必须用磁盘规范名，避免展示名大小写导致删不到目录
            canonical_name = str(installed_item.get("name") or skill_name).strip()
            payload = await self._manager.handle_skills_uninstall(
                {"name": canonical_name}
            )
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
                "detail": str(payload.get("detail", "")).strip()
                or "skill uninstall failed",
            }

        return {
            "success": True,
            "removed": True,
            "name": canonical_name,
            "display_name": str(installed_item.get("display_name") or canonical_name),
            "source": installed_item.get("source", "") if installed_item else "",
            "detail": f"Skill `{canonical_name}` uninstalled successfully.",
        }

    async def _list_installed_skills(self) -> dict[str, Any]:
        """列出已安装 skills，供 toolkit 内部逻辑复用。"""
        try:
            logger.info("[SkillToolkit] _list_installed_skills called")
            payload = await self._manager.handle_skills_installed({})
            if not payload.get("plugins"):
                plugin_items: list[dict[str, Any]] = []
            else:
                plugin_items = []
                for plugin in payload.get("plugins", []):
                    name = str(plugin.get("plugin_name", "")).strip()
                    source = str(plugin.get("marketplace", "")).strip() or "local"
                    if name:
                        plugin_items.append(self._build_installed_item(name, source))

            items: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in plugin_items:
                name = str(item.get("name", "")).strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                items.append(item)

            for local_skill in self._manager.get_local_skills():
                if not isinstance(local_skill, dict):
                    continue
                name = str(local_skill.get("name", "")).strip()
                if not name or name in seen:
                    continue
                source = str(local_skill.get("source", "")).strip() or "local"
                seen.add(name)
                items.append(self._build_installed_item(name, source))
            return {"success": True, "items": items, "detail": ""}
        except Exception as exc:  # noqa: BLE001
            logger.exception("list_installed_skills failed")
            return {"success": False, "items": [], "detail": str(exc)}

    def get_tools(self) -> list[Tool]:
        """Return skill-management tools for agent registration."""

        def make_tool(
            name: str, description: str, input_params: dict, func: Callable[..., Any]
        ) -> Tool:
            # 统一用 LocalFunction 包装，保持与现有 toolkit 注册方式一致。
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
                    "Search installable skills from ClawHub, TeamSkillsHub, "
                    "and builtin directory. Use the returned identifier with install_skill "
                    "(ClawHub slug, TeamSkillsHub asset_id, or skill name "
                    "when source is builtin)."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "One combined query describing the required capability, "
                                "formats, libraries, or workflow."
                            ),
                        },
                        "source": {
                            "type": "string",
                            "enum": ["auto", "clawhub", "teamskillshub", "builtin"],
                            "description": (
                                "Skill source to search. Defaults to auto. "
                                "Use auto to search all sources including builtin. "
                                "Use builtin to search locally available builtin skills."
                            ),
                            "default": "auto",
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
            ),
            make_tool(
                name="install_skill",
                description=(
                    "Install one skill after the user requests or confirms installation. "
                    "Pass the exact identifier and source returned by search_skill. For a "
                    "known built-in skill, use source='builtin' and its exact name."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "identifier": {
                            "type": "string",
                        },
                        "source": {
                            "type": "string",
                            "enum": ["clawhub", "teamskillshub", "builtin"],
                            "description": (
                                "Explicit source matching search_skill items, or 'builtin' "
                                "for locally available skills that don't need online search. "
                                "Use teamskillshub for Team Skills Hub."
                            ),
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
                        "name": {
                            "type": "string",
                            "description": "Installed skill name to remove.",
                        },
                    },
                    "required": ["name"],
                },
                func=self.uninstall_skill,
            ),
        ]
