"""Etcd-backed CronJob store for appliance active/standby Gateway."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE
from jiuwenswarm.gateway.cron.cron_job_mutations import (
    apply_cron_job_patch,
    build_new_cron_job,
    ensure_proactive_deletable,
    migrate_work_mode_on_items,
    parse_cron_jobs,
)
from jiuwenswarm.gateway.cron.etcd_client import (
    EtcdCasError,
    EtcdError,
    EtcdJsonClient,
    prefix_range_end,
)
from jiuwenswarm.gateway.cron.models import CronJob

logger = logging.getLogger(__name__)

_DEFAULT_PREFIX = "/jiuwenswarm/cron/jobs/"
_CONNECT_INITIAL_DELAY = 1.0
_CONNECT_MAX_DELAY = 30.0


class EtcdCronJobStore:
    """Per-job keys under ``/jiuwenswarm/cron/jobs/{job_id}``.

    Connection failure does not block Gateway start: ``list_jobs`` returns []
    until etcd is reachable; a watch loop retries with backoff then reloads.
    """

    supports_watch = True

    def __init__(
        self,
        *,
        endpoints: list[str],
        prefix: str = _DEFAULT_PREFIX,
        client: EtcdJsonClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._prefix = str(prefix or _DEFAULT_PREFIX)
        if not self._prefix.endswith("/"):
            self._prefix += "/"
        self._client = client or EtcdJsonClient(list(endpoints or []), timeout=timeout)
        self._lock = asyncio.Lock()
        self._revision = 0
        self._logged_empty_endpoints = False

    def _job_key(self, job_id: str) -> bytes:
        return f"{self._prefix}{job_id}".encode("utf-8")

    def _prefix_bytes(self) -> bytes:
        return self._prefix.encode("utf-8")

    def _unavailable(self, action: str) -> EtcdError:
        if not self._client.endpoints:
            if not self._logged_empty_endpoints:
                logger.error(
                    "[Cron] gateway.cron.store_backend=etcd requires etcd_endpoints; "
                    "not falling back to local file"
                )
                self._logged_empty_endpoints = True
            return EtcdError("etcd endpoints are empty")
        return EtcdError(f"etcd {action} failed")

    async def get_revision(self) -> int:
        return int(self._revision or 0)

    async def list_jobs(self) -> list[CronJob]:
        try:
            result = await self._client.range(
                self._prefix_bytes(),
                range_end=prefix_range_end(self._prefix_bytes()),
            )
        except EtcdError as exc:
            logger.warning("[Cron] etcd list_jobs failed: %s", exc)
            return []
        self._revision = result.revision or self._revision
        jobs_raw: list[Any] = []
        for kv in result.kvs:
            try:
                item = json.loads(kv.value.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(item, dict):
                jobs_raw.append(item)
        jobs_raw, changed = migrate_work_mode_on_items(jobs_raw)
        if changed:
            for item in jobs_raw:
                if not isinstance(item, dict):
                    continue
                job_id = str(item.get("id") or "").strip()
                if not job_id:
                    continue
                try:
                    await self._client.put(
                        self._job_key(job_id),
                        json.dumps(item, ensure_ascii=False).encode("utf-8"),
                    )
                except EtcdError as exc:
                    logger.warning("[Cron] etcd work_mode migrate put failed job=%s: %s", job_id, exc)
        return parse_cron_jobs(jobs_raw)

    async def get_job(self, job_id: str) -> CronJob | None:
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        try:
            result = await self._client.range(self._job_key(job_id))
        except EtcdError as exc:
            logger.warning("[Cron] etcd get_job failed job=%s: %s", job_id, exc)
            return None
        if result.revision:
            self._revision = result.revision
        if not result.kvs:
            return None
        try:
            item = json.loads(result.kvs[0].value.decode("utf-8"))
            if isinstance(item, dict):
                return CronJob.from_dict(item)
        except Exception:
            return None
        return None

    async def _get_job_with_rev(self, job_id: str) -> tuple[CronJob | None, int]:
        result = await self._client.range(self._job_key(job_id))
        if result.revision:
            self._revision = result.revision
        if not result.kvs:
            return None, 0
        kv = result.kvs[0]
        try:
            item = json.loads(kv.value.decode("utf-8"))
            if isinstance(item, dict):
                return CronJob.from_dict(item), int(kv.mod_revision)
        except Exception:
            return None, int(kv.mod_revision)
        return None, int(kv.mod_revision)

    async def create_job(
        self,
        *,
        job_id: str | None = None,
        name: str,
        cron_expr: str,
        timezone: str,
        description: str,
        targets: str,
        enabled: bool = True,
        wake_offset_seconds: int | None = None,
        session_id: str | None = None,
        chat_type: str | None = None,
        mode: str | None = None,
        delete_after_run: bool | None = None,
        timeout_seconds: int | None = None,
        project_id: str = "",
        model_name: str | None = None,
        app_id: str = "",
        work_mode: str = DEFAULT_WEB_WORK_MODE,
        user_id: str = "",
    ) -> CronJob:
        job = build_new_cron_job(
            job_id=job_id,
            name=name,
            cron_expr=cron_expr,
            timezone=timezone,
            description=description,
            targets=targets,
            enabled=enabled,
            wake_offset_seconds=wake_offset_seconds,
            session_id=session_id,
            chat_type=chat_type,
            mode=mode,
            delete_after_run=delete_after_run,
            timeout_seconds=timeout_seconds,
            project_id=project_id,
            model_name=model_name,
            app_id=app_id,
            work_mode=work_mode,
            user_id=user_id,
        )
        async with self._lock:
            try:
                self._revision = await self._client.put(
                    self._job_key(job.id),
                    json.dumps(job.to_dict(), ensure_ascii=False).encode("utf-8"),
                )
            except EtcdError as exc:
                raise self._unavailable("put") from exc
        return job

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> CronJob:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("id is required")
        async with self._lock:
            try:
                existing, mod_rev = await self._get_job_with_rev(job_id)
            except EtcdError as exc:
                raise self._unavailable("get") from exc
            if existing is None:
                raise KeyError("job not found")
            updated = apply_cron_job_patch(existing, dict(patch or {}))
            payload = json.dumps(updated.to_dict(), ensure_ascii=False).encode("utf-8")
            try:
                self._revision = await self._client.put_if_mod_revision(
                    self._job_key(job_id),
                    payload,
                    mod_revision=mod_rev,
                )
            except EtcdCasError:
                try:
                    existing, mod_rev = await self._get_job_with_rev(job_id)
                except EtcdError as exc:
                    raise self._unavailable("get") from exc
                if existing is None:
                    raise KeyError("job not found") from None
                updated = apply_cron_job_patch(existing, dict(patch or {}))
                payload = json.dumps(updated.to_dict(), ensure_ascii=False).encode("utf-8")
                try:
                    self._revision = await self._client.put_if_mod_revision(
                        self._job_key(job_id),
                        payload,
                        mod_revision=mod_rev,
                    )
                except EtcdError as exc:
                    raise self._unavailable("cas-put") from exc
            except EtcdError as exc:
                raise self._unavailable("cas-put") from exc
        return updated

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        job_id = str(job_id or "").strip()
        if not job_id:
            return False
        existing = await self.get_job(job_id)
        if existing is None:
            return False
        ensure_proactive_deletable(existing, force=force)
        async with self._lock:
            try:
                self._revision = await self._client.delete(self._job_key(job_id))
            except EtcdError as exc:
                raise self._unavailable("delete") from exc
        return True

    async def watch(self, callback: Callable[[], Awaitable[None]]) -> None:
        delay = _CONNECT_INITIAL_DELAY
        while True:
            try:
                if not self._client.endpoints:
                    raise self._unavailable("watch")
                await callback()
                delay = _CONNECT_INITIAL_DELAY
                async for _events in self._client.watch_prefix(self._prefix_bytes()):
                    await callback()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Cron] etcd watch/connect retry in %.1fs: %s", delay, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _CONNECT_MAX_DELAY)

    async def aclose(self) -> None:
        await self._client.aclose()
