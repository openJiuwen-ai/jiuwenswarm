# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版 Skill 上传暂存：过期包物理清理。

只按保留期删除，不看审批单状态；未到保留期一律不删。
清单缺失的目录按 mtime 宽限期兜底，避免误删写清单前进行中的暂存。
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import logger

CLEANUP_INTERVAL_SECONDS = 60 * 60
OBJECT_PREFIX = "skill-approvals/"
# 对象存储的 LastModified 精度和时钟偏差都可能让删除提前，这里只允许删得更晚。
_OBJECT_CLEANUP_GRACE = timedelta(days=1)
# 暂存目录在 manifest.json 落盘前就已存在：解包、算摘要、上传 OBS 都在这段窗口内。
# 无清单不能直接当过期，否则并发上传或每轮清理会删掉别人正在写的目录。
_MISSING_MANIFEST_GRACE = timedelta(hours=1)


def purge_expired_staging() -> int:
    """删除超过保留期的本地暂存目录，返回删除数量。

    清单缺失的目录需超过宽限期才删除，其余仍只按 ``expires_at`` 判断。
    """
    from jiuwenswarm.server.runtime.skill.skill_upload_approval import (
        is_valid_upload_id,
        manifest_expired,
        read_manifest,
        stage_root,
    )

    try:
        root = stage_root()
        entries = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        return 0

    removed = 0
    for entry in entries:
        if not entry.is_dir():
            continue
        if not is_valid_upload_id(entry.name):
            continue
        manifest = read_manifest(entry)
        if manifest is None:
            if not _stale_without_manifest(entry):
                continue
        elif not manifest_expired(manifest):
            continue
        _remove_staged(entry, manifest)
        removed += 1
    return removed


def _stale_without_manifest(staging: Path) -> bool:
    """无清单的暂存目录是否已超过宽限期；读不到 mtime 时保守保留。"""
    try:
        modified = datetime.fromtimestamp(staging.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    return datetime.now(timezone.utc) - modified >= _MISSING_MANIFEST_GRACE


def _remove_staged(staging: Path, manifest: dict[str, Any] | None) -> None:
    shutil.rmtree(staging, ignore_errors=True)
    object_key = str((manifest or {}).get("object_key") or "").strip()
    if not object_key:
        return
    try:
        from jiuwenswarm.common.obs_files import AttachmentStore

        AttachmentStore.from_env().remove_key(object_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SkillUploadCleanup] remove object failed: %s", exc)


def purge_expired_objects(*, now: datetime | None = None) -> int:
    """按对象 LastModified + 保留期 + 缓冲删除 OBS 副本，返回删除数量。"""
    from jiuwenswarm.common.obs_files import AttachmentStore
    from jiuwenswarm.server.runtime.skill.skill_upload_approval import retention_days

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(
        days=retention_days()
    ) - _OBJECT_CLEANUP_GRACE
    try:
        store = AttachmentStore.from_env()
        objects = store.list_keys(OBJECT_PREFIX)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SkillUploadCleanup] list objects failed: %s", exc)
        return 0

    removed = 0
    for key, last_modified in objects:
        if last_modified > cutoff:
            continue
        try:
            store.remove_key(key)
            removed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[SkillUploadCleanup] remove object failed key=%s error=%s", key, exc
            )
    return removed


def purge_expired_once() -> tuple[int, int]:
    """清理一轮过期暂存与对象，返回 ``(本地目录数, 对象数)``。"""
    return purge_expired_staging(), purge_expired_objects()


async def run_skill_upload_cleanup_loop(stop: asyncio.Event) -> None:
    """进程级清理循环，直到 ``stop`` 被设置。"""
    while not stop.is_set():
        try:
            staging, objects = await asyncio.to_thread(purge_expired_once)
            if staging or objects:
                logger.info(
                    "[SkillUploadCleanup] purged staging=%s objects=%s",
                    staging,
                    objects,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SkillUploadCleanup] purge failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=CLEANUP_INTERVAL_SECONDS)
        except TimeoutError:
            continue
