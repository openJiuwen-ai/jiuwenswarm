# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""预置技能（skill_prebuilt）：按租户将预置技能同步到 workspace 与 ``skills_state.json``。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.common.utils import _require_tenant_ids
from jiuwenswarm.server.runtime.skill.skill_manager import (
    SkillManager,
    SkillNameConflictError,
    _safe_rmtree,
)

logger = logging.getLogger(__name__)

_RESERVED_SKILL_DIR_NAMES = frozenset({"_marketplace", "skills_state.json"})
SOURCE_PREBUILT = "prebuilt"
SOURCE_USER = "user"

_SKILLS_DIR_SYNC_LOCKS: dict[str, asyncio.Lock] = {}
_SKILLS_DIR_SYNC_LOCKS_META = threading.Lock()


async def _skills_dir_sync_lock_for(skills_dir: Path) -> asyncio.Lock:
    key = str(skills_dir.resolve())
    with _SKILLS_DIR_SYNC_LOCKS_META:
        lock = _SKILLS_DIR_SYNC_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _SKILLS_DIR_SYNC_LOCKS[key] = lock
        return lock


@dataclass
class SkillPrebuiltItem:
    """预置项。provider：``source_id+skill_id+version_id``；url：``package_url``。"""

    id: str
    version: str = ""
    source: str = ""  # package_url
    source_id: str = ""
    version_id: str = ""
    sha256: str = ""

    @property
    def package_url(self) -> str:
        return str(self.source or "").strip()

    def is_provider_path(self) -> bool:
        return bool(self.source_id and self.id and self.version_id)

    def is_url_path(self) -> bool:
        url = self.package_url
        return bool(url) and (
            url.startswith("http://") or url.startswith("https://")
        )

    def install_mode(self) -> str | None:
        """按字段推断：provider 优先；否则合法 URL；否则 None。"""
        if self.is_provider_path():
            return "provider"
        if self.is_url_path():
            return "url"
        return None


@dataclass
class AgentSkillPrebuiltConfig:
    agent_id: str
    service_id: str
    skills: list[SkillPrebuiltItem] = field(default_factory=list)

    @property
    def items_with_source(self) -> list[SkillPrebuiltItem]:
        """可安装项：能推断出 provider 或 url 路径。"""
        return [item for item in self.skills if item.install_mode() is not None]


@dataclass
class SkillPrebuiltSyncResult:
    enabled_skill_dirs: list[str] = field(default_factory=list)
    prebuilt_skill_dirs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    succeeded: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    ok: bool = True


def is_skill_prebuilt_tenant(agent_id: str | None, service_id: str | None) -> bool:
    """ACP/default 或 ID 缺失的租户不启用白名单逻辑；仅企业版下生效."""
    if not is_enterprise():
        return False
    try:
        sid, aid = _require_tenant_ids(service_id, agent_id)
    except ValueError:
        return False
    if aid == "acp" and sid == "global_acp":
        return False
    if aid == "default" and sid == "default":
        return False
    return True


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _extract_data_object(raw: dict[str, Any]) -> dict[str, Any]:
    data = raw.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            data = None
    return data if isinstance(data, dict) else {}


def _extract_sha256(raw: dict[str, Any]) -> str:
    """url 路径可选摘要：仅取 ``data.sha256``（顶层 sha256 兼容旧下发）。"""
    data = _extract_data_object(raw)
    value = str(data.get("sha256") or raw.get("sha256") or "").strip().lower()
    if value and _SHA256_RE.fullmatch(value):
        return value
    return value  # 非规范值仍传给安装层，由校验失败关闭


def parse_agent_skill_prebuilt(
    agent_id: str,
    service_id: str,
    skills: list[dict[str, Any]] | None,
) -> AgentSkillPrebuiltConfig:
    """解析 gateway 下发的预置清单（``skill_prebuilt``）。

    顶层一等字段：``skill_id``、``package_url``、``source_id``、``version_id``、``enabled``；
    ``data.sha256`` 仅用于 url 路径完整性校验（provider 忽略）。
    """
    items: list[SkillPrebuiltItem] = []
    for raw in skills or []:
        if not isinstance(raw, dict):
            continue
        if not raw.get("enabled", True):
            continue
        skill_id = str(raw.get("skill_id", "")).strip()
        if not skill_id:
            continue
        # SPI 三元组：顶层一等字段优先；兼容过渡期 data.*
        data = _extract_data_object(raw)
        source_id = (
            str(raw.get("source_id") or "").strip()
            or str(data.get("source_id") or "").strip()
        )
        version_id = (
            str(raw.get("version_id") or "").strip()
            or str(data.get("version_id") or "").strip()
        )
        package_url = str(raw.get("package_url") or "").strip()
        items.append(
            SkillPrebuiltItem(
                id=skill_id,
                source=package_url,
                source_id=source_id,
                version_id=version_id,
                sha256=_extract_sha256(raw),
            )
        )
    return AgentSkillPrebuiltConfig(agent_id=agent_id, service_id=service_id, skills=items)




class SkillPrebuiltSynchronizer:
    """将预置技能同步到租户 skills/，并写入 ``installed_skill``（按 skill 原子）."""

    def __init__(
        self,
        workspace_dir: str | Path,
        service_id: str,
        agent_id: str,
        *,
        group_id: str | None = None,
        bot_id: str | None = None,
        skill_manager: SkillManager | None = None,
    ) -> None:
        workspace = Path(workspace_dir)
        self._skills_dir = workspace / "skills"
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        self._manager = skill_manager or SkillManager(
            workspace_dir=str(workspace),
            persist_skills_state=True,
            service_id=service_id,
            agent_id=agent_id,
        )

    def _remove_installed_dir(self, installed_dir: str) -> None:
        if not installed_dir or installed_dir in _RESERVED_SKILL_DIR_NAMES:
            return
        target = self._skills_dir / installed_dir
        if target.is_dir():
            _safe_rmtree(target)

    @staticmethod
    def _skill_dir_ready(skills_dir: Path, skill_name: str) -> bool:
        if not skill_name:
            return False
        path = skills_dir / skill_name
        return path.is_dir() and (path / "SKILL.md").is_file()

    def _should_download_prebuilt(
        self,
        item: SkillPrebuiltItem,
        installed_skills_map: dict[str, dict[str, Any]],
    ) -> tuple[bool, str]:
        """是否需要下载预置包。返回 (need_download, installed_skill_name)."""
        mode = item.install_mode()
        installed_row: dict[str, Any] | None = None
        for row in installed_skills_map.values():
            if str(row.get("source_type")) != SOURCE_PREBUILT:
                continue
            if str(row.get("skill_id") or "").strip() == item.id:
                installed_row = row
                break
        else:
            # url 兼容：按 origin/package_url 回退匹配
            if mode == "url":
                source = item.package_url
                for row in installed_skills_map.values():
                    if str(row.get("source_type")) != SOURCE_PREBUILT:
                        continue
                    if source and str(row.get("origin") or "").strip() == source:
                        installed_row = row
                        break

        if installed_row is None:
            if mode == "url" and item.version:
                adopted = self._adopt_existing_dir_for(item)
                if adopted:
                    return False, adopted
            return True, ""

        installed_name = str(installed_row.get("name") or "").strip()
        if not installed_name:
            return True, ""
        if not self._skill_dir_ready(self._skills_dir, installed_name):
            return True, installed_name

        if mode == "provider":
            same_version = (
                str(installed_row.get("version_id") or "").strip()
                == item.version_id.strip()
            )
            if same_version:
                return False, installed_name
            return True, installed_name

        # url：比 origin/package_url；若模板带 data.sha256，再比账本摘要
        current_origin = str(installed_row.get("origin") or "").strip()
        if current_origin != item.package_url:
            return True, installed_name
        expected = item.sha256.strip().lower()
        if expected and self._recorded_checksum(installed_row) != expected:
            return True, installed_name
        return False, installed_name

    def _adopt_existing_dir_for(self, item: SkillPrebuiltItem) -> str:
        """盘→账本回填：返回 SKILL.md 声明同 ``skill_id+version`` 的就绪目录名，未命中返回空."""
        if not item.id or not item.version:
            return ""
        adopted = self._manager.find_skill_dir_by_identity(
            skill_id=item.id, version=item.version
        )
        if adopted and self._skill_dir_ready(self._skills_dir, adopted):
            return adopted
        return ""

    @staticmethod
    def _recorded_checksum(installed_row: dict[str, Any]) -> str:
        """读取安装记录中已校验过的 ``verification.checksum_sha256``."""
        verification = installed_row.get("verification")
        if isinstance(verification, dict):
            return str(verification.get("checksum_sha256") or "").strip().lower()
        return ""

    @staticmethod
    def _mark_failed(
        result: SkillPrebuiltSyncResult,
        *,
        skill_name: str,
        error_code: str,
        error_message: str,
    ) -> None:
        result.ok = False
        result.errors.append(error_message)
        result.failed.append(
            {
                "skill_name": skill_name,
                "error_code": error_code,
                "error_message": error_message,
            }
        )

    async def sync(self, config: AgentSkillPrebuiltConfig) -> SkillPrebuiltSyncResult:
        """按 skills 物理目录串行同步：同一落盘目录同时仅一个 sync 在飞."""
        lock = await _skills_dir_sync_lock_for(self._skills_dir)
        async with lock:
            return await self._run_sync(config)

    async def _run_sync(self, config: AgentSkillPrebuiltConfig) -> SkillPrebuiltSyncResult:
        """持锁同步：对齐模板预置 → 剔除多余 → 刷新启用集."""
        result = SkillPrebuiltSyncResult()
        installed_skills_map = await self._fetch_installed_skills_map(result)
        if installed_skills_map is None:
            return result

        kept_prebuilt_names: set[str] = set()
        for item in config.skills:
            mode = item.install_mode()
            if mode is None:
                self._mark_failed(
                    result,
                    skill_name="",
                    error_code="invalid_template",
                    error_message=(
                        f"cannot infer install path for skill_id={item.id}: "
                        "need source_id+version_id or http(s) package_url"
                    ),
                )
                continue
            try:
                outcome = await self._ensure_prebuilt_installed(item, installed_skills_map)
            except Exception as exc:  # noqa: BLE001
                msg = (
                    f"sync failed id={item.id} mode={mode} "
                    f"source={item.package_url or item.source_id}: {exc}"
                )
                logger.warning("[SkillPrebuilt] %s", msg)
                self._mark_failed(
                    result,
                    skill_name="",
                    error_code="sync_exception",
                    error_message=msg,
                )
                continue
            self._apply_prebuilt_outcome(
                outcome, installed_skills_map, kept_prebuilt_names, result
            )

        await self._remove_prebuilt_not_in_template(
            installed_skills_map, kept_prebuilt_names, result
        )
        result.enabled_skill_dirs = self._manager.list_enabled_skill_names()
        result.prebuilt_skill_dirs = [
            name
            for name, row in installed_skills_map.items()
            if str(row.get("source_type") or "").strip() == SOURCE_PREBUILT
        ]
        return result

    async def reconcile_disk_into_ledger(self) -> SkillPrebuiltSyncResult:
        """仅做盘→库对账并重算启用集（供热刷新路径复用，不跑预置模板 sync）."""
        lock = await _skills_dir_sync_lock_for(self._skills_dir)
        async with lock:
            result = SkillPrebuiltSyncResult()
            result.enabled_skill_dirs = self._manager.list_enabled_skill_names()
            return result

    async def _fetch_installed_skills_map(
        self, result: SkillPrebuiltSyncResult
    ) -> dict[str, dict[str, Any]] | None:
        """查询租户已装技能，返回 skill_name -> 行；失败时写 result 并返回 None."""
        try:
            existing_rows = self._manager.list_skill_installations()
        except Exception as exc:
            msg = f"list workspace skill state failed: {exc}"
            logger.warning("[SkillPrebuilt] %s", msg)
            self._mark_failed(
                result,
                skill_name="",
                error_code="state_list_failed",
                error_message=msg,
            )
            return None
        return {
            str(r.get("name") or "").strip(): r
            for r in existing_rows
            if str(r.get("name") or "").strip()
        }

    def _apply_prebuilt_outcome(
        self,
        outcome: dict[str, Any],
        installed_skills_map: dict[str, dict[str, Any]],
        kept_prebuilt_names: set[str],
        result: SkillPrebuiltSyncResult,
    ) -> None:
        """根据单项 sync 结果更新 kept 集合与本地索引（不再回读 DB）."""
        if outcome.get("ok"):
            name = str(outcome.get("skill_name") or "").strip()
            if not name:
                return
            kept_prebuilt_names.add(name)
            result.succeeded.append(name)
            row = outcome.get("row")
            if isinstance(row, dict) and row:
                installed_skills_map[name] = row
            return

        keep = str(outcome.get("skill_name") or "").strip()
        if (
            keep
            and keep in installed_skills_map
            and str(installed_skills_map[keep].get("source_type")) == SOURCE_PREBUILT
        ):
            kept_prebuilt_names.add(keep)
        self._mark_failed(
            result,
            skill_name=str(outcome.get("skill_name") or ""),
            error_code=str(outcome.get("error_code") or "sync_failed"),
            error_message=str(outcome.get("error_message") or "sync failed"),
        )

    async def _remove_prebuilt_not_in_template(
        self,
        installed_skills_map: dict[str, dict[str, Any]],
        kept_prebuilt_names: set[str],
        result: SkillPrebuiltSyncResult,
    ) -> None:
        """当前模板里没有的预置技能：硬删盘+库（不降回 user）."""
        for name, row in list(installed_skills_map.items()):
            if str(row.get("source_type")) != SOURCE_PREBUILT:
                continue
            if name in kept_prebuilt_names:
                continue
            try:
                removed = await asyncio.to_thread(
                    self._manager.remove_skill_installation_entity,
                    name=name,
                    origin=str(row.get("origin") or "") or None,
                    expected_source_type=SOURCE_PREBUILT,
                )
                if not removed:
                    logger.warning(
                        "[SkillPrebuilt] prebuilt record no longer matches workspace: %s",
                        name,
                    )
                    continue
                result.succeeded.append(f"removed:{name}")
                installed_skills_map.pop(name, None)
            except Exception as exc:  # noqa: BLE001
                msg = f"remove prebuilt failed name={name}: {exc}"
                logger.warning("[SkillPrebuilt] %s", msg)
                self._mark_failed(
                    result,
                    skill_name=name,
                    error_code="remove_failed",
                    error_message=msg,
                )

    async def _ensure_prebuilt_installed(
        self,
        item: SkillPrebuiltItem,
        installed_skills_map: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """确保模板项对应的预置已就绪：按需下载落盘 → 校验目录 → upsert 账本."""
        mode = item.install_mode()
        if mode is None:
            return {
                "ok": False,
                "skill_name": "",
                "error_code": "invalid_template",
                "error_message": f"cannot infer install path for skill_id={item.id}",
            }

        need_download, previous_skill_name = self._should_download_prebuilt(
            item, installed_skills_map
        )

        installed_dir = previous_skill_name
        install_version = item.version
        if need_download:
            if mode == "provider":
                # provider 不得回退到 package_url
                install_result = await self._manager.install_prebuilt_from_provider(
                    source_id=item.source_id,
                    skill_id=item.id,
                    version_id=item.version_id,
                    force=True,
                )
                if not install_result.get("ok"):
                    code = str(install_result.get("error_code") or "download_failed")
                    detail = install_result.get("detail") or "provider install failed"
                    return {
                        "ok": False,
                        "skill_name": previous_skill_name,
                        "error_code": code,
                        "error_message": (
                            f"sync failed id={item.id} source_id={item.source_id}: {detail}"
                        ),
                    }
                installed_dir = str(install_result.get("skill_name", "")).strip()
                install_version = str(
                    install_result.get("version") or item.version_id
                ).strip()
                row = install_result.get("row")
                if previous_skill_name and previous_skill_name != installed_dir:
                    self._remove_installed_dir(previous_skill_name)
                    installed_skills_map.pop(previous_skill_name, None)
                if isinstance(row, dict) and installed_dir:
                    # provider 路径已原子记 prebuilt，直接返回
                    return {"ok": True, "skill_name": installed_dir, "row": row}
            else:
                try:
                    install_result = await asyncio.to_thread(
                        self._manager.install_skill_sync,
                        item.package_url,
                        True,
                        None,
                        item.sha256,
                        protected_source_types=frozenset({SOURCE_USER}),
                    )
                except Exception as exc:
                    return {
                        "ok": False,
                        "skill_name": previous_skill_name,
                        "error_code": "download_failed",
                        "error_message": (
                            f"sync failed id={item.id} source={item.package_url}: {exc}"
                        ),
                    }
                if not install_result.get("ok"):
                    detail = install_result.get("detail") or "install failed"
                    code = str(install_result.get("error_code") or "install_failed")
                    return {
                        "ok": False,
                        "skill_name": previous_skill_name,
                        "error_code": code,
                        "error_message": (
                            f"sync failed id={item.id} source={item.package_url}: {detail}"
                        ),
                    }
                installed_dir = str(install_result.get("skill_name", "")).strip()
                meta = install_result.get("meta")
                if isinstance(meta, dict):
                    install_version = str(meta.get("version") or install_version).strip()
                if previous_skill_name and previous_skill_name != installed_dir:
                    self._remove_installed_dir(previous_skill_name)

        if not installed_dir or not self._skill_dir_ready(self._skills_dir, installed_dir):
            return {
                "ok": False,
                "skill_name": installed_dir,
                "error_code": "dir_missing" if installed_dir else "empty_skill_name",
                "error_message": (
                    f"installed dir missing after sync: id={item.id} dir={installed_dir}"
                ),
            }

        # 预置同步不得改变用户自安装技能的身份或实体。
        existing_user = None
        for record in self._manager.list_skill_installations():
            if (
                str(record.get("name") or "").strip() == installed_dir
                and str(record.get("source_type") or "").strip() == SOURCE_USER
            ):
                existing_user = record
                break
        if existing_user is not None:
            return {
                "ok": False,
                "skill_name": installed_dir,
                "error_code": "skill_name_conflict",
                "error_message": f"prebuilt skill conflicts with user skill: {installed_dir}",
            }

        origin = (
            f"{item.source_id}:{item.id}"
            if mode == "provider"
            else item.package_url
        )
        try:
            row = await asyncio.to_thread(
                self._manager.record_skill_installation,
                name=installed_dir,
                source_type=SOURCE_PREBUILT,
                source=item.source_id or "enterprise-prebuilt",
                origin=origin,
                version=install_version or item.version_id or item.version,
                skill_id=item.id,
                source_id=item.source_id or None,
                version_id=item.version_id or None,
                verification={"checksum_sha256": item.sha256} if item.sha256 else None,
                replace_by_name=True,
            )
        except SkillNameConflictError as exc:
            if need_download and mode == "url":
                self._remove_installed_dir(installed_dir)
            return {
                "ok": False,
                "skill_name": installed_dir,
                "error_code": "skill_name_conflict",
                "error_message": f"skill name conflict id={item.id} name={installed_dir}: {exc}",
            }
        except Exception as exc:
            if need_download and mode == "url":
                self._remove_installed_dir(installed_dir)
            return {
                "ok": False,
                "skill_name": installed_dir,
                "error_code": "state_write_failed",
                "error_message": f"write state failed id={item.id} name={installed_dir}: {exc}",
            }

        if previous_skill_name and previous_skill_name != installed_dir:
            installed_skills_map.pop(previous_skill_name, None)

        return {"ok": True, "skill_name": installed_dir, "row": row}
