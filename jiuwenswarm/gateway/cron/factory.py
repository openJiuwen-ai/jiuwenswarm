"""Factory: personal edition file store vs HA etcd store."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_cron_jobs_path
from jiuwenswarm.gateway.cron.etcd_store import EtcdCronJobStore
from jiuwenswarm.gateway.cron.store import FileCronJobStore
from jiuwenswarm.gateway.cron.store_base import CronJobStoreBackend

logger = logging.getLogger(__name__)

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


async def create_gateway_cron_store(
    config: dict[str, Any] | None = None,
) -> CronJobStoreBackend:
    """Return the configured cron job store.

    Default is the personal-edition file store. etcd is opt-in via
    ``gateway.cron.store_backend=etcd`` and is **not** inferred from
    AgentOS routing or from a non-empty endpoints list.
    """
    if config is None:
        try:
            from jiuwenswarm.common.config import get_config

            config = get_config()
        except Exception:
            config = {}
    settings = load_cron_store_settings(config)
    path = settings.file_path or get_cron_jobs_path()
    if settings.backend != "etcd":
        return FileCronJobStore(path=path)

    if not settings.endpoints:
        logger.error(
            "[Cron] gateway.cron.store_backend=etcd requires etcd_endpoints; "
            "not falling back to local file"
        )
    store = EtcdCronJobStore(
        endpoints=list(settings.endpoints),
        prefix=settings.prefix,
    )
    return store
