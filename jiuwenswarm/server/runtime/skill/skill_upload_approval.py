# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版 Skill 按包上传审批：包描述符校验与暂存目录工具。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
# 暂存内容与 OBS 副本共用同一保留期；manager 按同一环境变量计算审批单过期时间，
# 两侧必须配置一致，否则会出现「审批单还在但包已删除」。
RETENTION_DAYS_ENV = "SKILL_UPLOAD_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 7
_MIN_RETENTION_DAYS = 1

_STAGE_DIRNAME = "_skill_upload_pending"
_SKILL_NAME_MAX_LEN = 128
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def retention_days() -> int:
    """包保留天数：默认 7 天，小于 1 或非法值回落默认值，不设上限。"""
    raw = os.getenv(RETENTION_DAYS_ENV, "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    if value < _MIN_RETENTION_DAYS:
        return DEFAULT_RETENTION_DAYS
    return value


def stage_ttl_seconds() -> int:
    """暂存内容与 OBS 副本的统一有效期。"""
    return retention_days() * 24 * 60 * 60


def stage_root() -> Path:
    from jiuwenswarm.common.utils import get_agent_root_dir

    return get_agent_root_dir() / _STAGE_DIRNAME


def read_manifest(staging: Path) -> dict[str, Any] | None:
    """读取暂存目录清单；缺失或损坏时返回 None。"""
    try:
        raw = (staging / "manifest.json").read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def manifest_expired(manifest: dict[str, Any] | None) -> bool:
    """清单过期或缺少有效 ``expires_at`` 时视为过期。"""
    raw = str((manifest or {}).get("expires_at") or "").strip()
    if not raw:
        return True
    try:
        expires = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


def is_valid_upload_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", value or ""))


def validate_skill_name(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        raise ValueError("invalid skill name")
    bad_size = len(value) > _SKILL_NAME_MAX_LEN or ".." in value
    bad_prefix = value[0] in {"_", "."}
    bad_char = any(char in value for char in "/\\\0")
    if bad_size or bad_prefix or bad_char:
        raise ValueError("invalid skill name")
    return value


def skill_tree_digest(root: Path) -> tuple[str, int, int]:
    """返回 ``(sha256:<hex>, file_count, uncompressed_bytes)``，按相对路径排序。"""
    digest = hashlib.sha256()
    file_count = 0
    total = 0
    files = sorted(
        path for path in Path(root).rglob("*") if path.is_file()
    )
    for path in files:
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        size = 0
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        digest.update(b"\0")
        file_count += 1
        total += size
    return f"sha256:{digest.hexdigest()}", file_count, total
