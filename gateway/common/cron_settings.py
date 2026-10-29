# Gateway 侧副本，源文件 jiuwenswarm/common/cron_settings.py；改动请同步两侧。
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cron 存储后端配置解析（纯配置读取，gateway/runtime/server 三方共用）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway.common.paths import get_cron_jobs_path

_DEFAULT_PREFIX = "/jiuwenswarm/cron/jobs/"


@dataclass(frozen=True)
class CronStoreSettings:
    backend: str = "file"
    endpoints: tuple[str, ...] = ()
    prefix: str = _DEFAULT_PREFIX
    file_path: Path | None = None


def _as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_endpoints(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    text = str(raw or "").strip()
    if not text:
        return ()
    return tuple(part.strip() for part in text.split(",") if part.strip())


def load_cron_store_settings(config: dict[str, Any] | None = None) -> CronStoreSettings:
    cfg = config if isinstance(config, dict) else {}
    cron = _as_mapping(_as_mapping(cfg.get("gateway")).get("cron"))
    backend = str(cron.get("store_backend") or "file").strip().lower() or "file"
    prefix = str(cron.get("etcd_prefix") or _DEFAULT_PREFIX).strip() or _DEFAULT_PREFIX
    return CronStoreSettings(
        backend=backend,
        endpoints=_parse_endpoints(cron.get("etcd_endpoints")),
        prefix=prefix,
        file_path=get_cron_jobs_path(),
    )
