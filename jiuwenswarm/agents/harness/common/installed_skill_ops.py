# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业用户技能装卸编排：decide → 落盘后写账本 / 卸载守卫。

供 ``SkillToolkit``（对话）与 ``SkillManager``（Web RPC）共用，避免企业逻辑堆在工具层。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from jiuwenswarm.agents.harness.common.installed_skill import (
    DECISION_ALREADY_INSTALLED,
    DECISION_BLOCKED,
    DECISION_PREBUILT,
    SOURCE_PREBUILT,
    SOURCE_USER,
    decide_user_reinstall,
    delete_installed_skill,
    format_user_skill_source,
    get_installed_skill,
    upsert_installed_skill,
)

logger = logging.getLogger(__name__)

ChannelInstallFn = Callable[..., Awaitable[dict[str, Any]]]
GetSkillMetaFn = Callable[[str], dict[str, Any] | None]
RemoveSkillDirFn = Callable[[str], None]


def _is_exists_detail(detail: str) -> bool:
    text = str(detail or "").lower()
    return (
        "已存在" in detail
        or "已安装" in detail
        or "already" in text
        or "exists" in text
    )


def _reject_for_decision(
    decision: str,
    skill_name: str,
    *,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """拒绝类 decision → 统一失败响应；可装/可升级返回 ``None``。"""
    name = str(skill_name or "").strip()
    source = str(channel or "").strip().lower() or None
    base: dict[str, Any] = {"success": False, "installed": False}
    if source:
        base["source"] = source

    if decision == DECISION_PREBUILT:
        return {
            **base,
            "error_code": "conflict_prebuilt",
            "detail": f"Skill `{name}` is prebuilt and cannot be overwritten by user install.",
            "error_message": f"skill `{name}` is prebuilt and cannot be overwritten",
        }
    if decision == DECISION_ALREADY_INSTALLED:
        return {
            **base,
            "already_installed": True,
            "error_code": "already_installed",
            "name": name,
            "detail": f"Skill `{name}` is already installed with the same version.",
            "error_message": f"skill `{name}` already installed with the same version",
        }
    if decision == DECISION_BLOCKED:
        return {
            **base,
            "error_code": "blocked",
            "detail": f"Skill `{name}` cannot be installed (unknown source_type).",
            "error_message": f"skill `{name}` cannot be installed (unknown source_type)",
        }
    return None


async def precheck_install(
    *,
    service_id: str,
    agent_id: str,
    skill_name: str,
    skill_version: str | None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """落盘前轻量预检：读账本 + decide；拒绝则返回失败响应，否则 ``None``。"""
    existing = await get_installed_skill(
        service_id=str(service_id or "").strip(),
        agent_id=str(agent_id or "").strip(),
        skill_name=str(skill_name or "").strip(),
    )
    decision = decide_user_reinstall(existing, new_version=skill_version)
    return _reject_for_decision(decision, skill_name, channel=channel)


async def commit_install(
    *,
    service_id: str,
    agent_id: str,
    skill_name: str,
    skill_version: str | None,
    channel: str,
    identifier: str,
    group_id: str | None = None,
    bot_id: str | None = None,
    user_id: str | None = None,
    remove_skill_dir: RemoveSkillDirFn | None = None,
) -> dict[str, Any]:
    """落盘后写账本：decide → upsert；失败可回滚删盘。"""
    sid = str(service_id or "").strip()
    aid = str(agent_id or "").strip()
    name = str(skill_name or "").strip()
    version = str(skill_version or "").strip() or None
    source = str(channel or "").strip().lower() or "skillnet"

    existing = await get_installed_skill(
        service_id=sid,
        agent_id=aid,
        skill_name=name,
    )
    decision = decide_user_reinstall(existing, new_version=version)
    reject = _reject_for_decision(decision, name, channel=source)
    if reject is not None:
        # BLOCKED：本次 force 落盘可能写入未知类型冲突目录，允许回滚删盘。
        # PREBUILT：勿删，目录可能仍属预置。
        if decision == DECISION_BLOCKED and remove_skill_dir is not None:
            remove_skill_dir(name)
        return reject

    try:
        await upsert_installed_skill(
            service_id=sid,
            agent_id=aid,
            skill_name=name,
            source_type=SOURCE_USER,
            skill_source=format_user_skill_source(source, identifier),
            skill_version=version,
            group_id=group_id,
            bot_id=bot_id,
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001
        if remove_skill_dir is not None:
            remove_skill_dir(name)
        logger.warning("[InstalledSkillOps] DB write failed, rolled back disk: %s", exc)
        return {
            "success": False,
            "source": source,
            "installed": False,
            "error_code": "db_write_failed",
            "detail": str(exc),
            "error_message": str(exc)[:500],
        }

    return {
        "success": True,
        "installed": True,
        "name": name,
        "skill_version": version,
        "source": source,
        "identifier": identifier,
    }


async def install_from_channel(
    *,
    service_id: str,
    agent_id: str,
    target: str,
    source: str,
    timeout_sec: int,
    channel_install: ChannelInstallFn,
    get_skill_meta: GetSkillMetaFn,
    remove_skill_dir: RemoveSkillDirFn,
    group_id: str | None = None,
    bot_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """对话路径：非 force 落盘 → ``commit_install``。

    盘上撞名不盲 force：prebuilt → conflict；否则 → already_installed。
    对话侧暂不自动升级（避免同版本误覆盖）；升级走 Web 验签安装。
    """
    src = str(source or "").strip().lower()
    payload = await channel_install(target, src, timeout_sec, force=False)
    if not payload.get("success") and _is_exists_detail(str(payload.get("detail") or "")):
        guess_name = Path(target).name if src == "skillnet" else target
        guess_name = str(guess_name or "").strip()
        existing = None
        if guess_name:
            existing = await get_installed_skill(
                service_id=service_id,
                agent_id=agent_id,
                skill_name=guess_name,
            )
        if existing is None and guess_name:
            meta = get_skill_meta(guess_name) or {}
            if meta.get("name"):
                guess_name = str(meta.get("name")).strip()
                existing = await get_installed_skill(
                    service_id=service_id,
                    agent_id=agent_id,
                    skill_name=guess_name,
                )
        name = guess_name or str(target or "").strip()
        if (
            existing is not None
            and str(existing.get("source_type") or "").strip() == SOURCE_PREBUILT
        ):
            return _reject_for_decision(DECISION_PREBUILT, name, channel=src) or {
                "success": False,
                "source": src,
                "installed": False,
                "error_code": "conflict_prebuilt",
            }
        return _reject_for_decision(DECISION_ALREADY_INSTALLED, name, channel=src) or {
            "success": False,
            "source": src,
            "installed": False,
            "already_installed": True,
            "error_code": "already_installed",
            "name": name,
            "detail": str(payload.get("detail") or "skill already exists"),
        }

    if not payload.get("success"):
        return {
            "success": False,
            "source": src,
            "installed": False,
            "detail": str(payload.get("detail", "")).strip() or "skill installation failed",
        }

    skill = payload.get("skill") or {}
    name = str(skill.get("name", "")).strip()
    if not name:
        name = Path(target).name if src == "skillnet" else target
    meta = get_skill_meta(name) or {}
    version = str(meta.get("version") or skill.get("version") or "").strip() or None

    return await commit_install(
        service_id=service_id,
        agent_id=agent_id,
        skill_name=name,
        skill_version=version,
        channel=src,
        identifier=target,
        group_id=group_id,
        bot_id=bot_id,
        user_id=user_id,
        remove_skill_dir=remove_skill_dir,
    )


async def uninstall(
    *,
    service_id: str,
    agent_id: str,
    skill_name: str,
    remove_from_disk: Callable[[str], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """仅 ``source_type=user``：硬删账本后删盘。"""
    name = str(skill_name or "").strip()
    sid = str(service_id or "").strip()
    aid = str(agent_id or "").strip()
    if not name:
        return {
            "success": False,
            "removed": False,
            "error_code": "missing_params",
            "detail": "name is required",
            "error_message": "name is required",
        }

    existing = await get_installed_skill(
        service_id=sid,
        agent_id=aid,
        skill_name=name,
    )
    if existing is None:
        return {
            "success": False,
            "removed": False,
            "name": name,
            "error_code": "not_found",
            "detail": f"Skill `{name}` is not installed.",
            "error_message": f"skill `{name}` is not installed",
        }
    if str(existing.get("source_type") or "").strip() != SOURCE_USER:
        return {
            "success": False,
            "removed": False,
            "name": name,
            "error_code": "prebuilt_not_removable",
            "detail": f"Skill `{name}` is prebuilt and cannot be uninstalled.",
            "error_message": f"skill `{name}` is prebuilt and cannot be uninstalled",
        }

    try:
        await delete_installed_skill(
            service_id=sid,
            agent_id=aid,
            skill_name=name,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "removed": False,
            "name": name,
            "error_code": "db_delete_failed",
            "detail": str(exc),
            "error_message": str(exc)[:500],
        }

    disk = await remove_from_disk(name)
    if not disk.get("success"):
        logger.warning(
            "[InstalledSkillOps] uninstall disk remove incomplete: name=%s detail=%s",
            name,
            disk.get("detail"),
        )
    return {
        "success": True,
        "removed": True,
        "name": name,
        "source": "user",
        "detail": f"Skill `{name}` uninstalled successfully.",
    }


__all__ = (
    "commit_install",
    "install_from_channel",
    "precheck_install",
    "uninstall",
)
