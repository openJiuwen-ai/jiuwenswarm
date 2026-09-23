"""Bounded shared catalogs, immutable snapshot publication and background refresh."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass, replace
import logging
from pathlib import Path
import threading
import time

from .catalog import read_catalog
from .config import SelectionSettings
from .freshness import catalog_signature, sources_unchanged
from .diagnostics import configuration_metadata, emit
from .execution import (
    SelectionBusy,
    SelectionRuntime,
    SelectionTimedOut,
    WorkBudget,
    background,
    budget,
    checkpoint,
    get_runtime,
)
from .pipeline import SelectionPipeline, SelectionResult

logger = logging.getLogger(__name__)
_services: OrderedDict = OrderedDict()
_services_lock = threading.Lock()


@dataclass(frozen=True)
class _PreparedCatalog:
    pipeline: SelectionPipeline | None
    signature: tuple
    source_files: dict
    changed: bool


class SelectionService:
    def __init__(
        self,
        roots: tuple[Path, ...],
        settings: SelectionSettings,
        *,
        runtime: SelectionRuntime | None = None,
    ):
        self.roots, self.settings = roots, settings
        self.runtime = runtime or get_runtime()
        self._lock = threading.RLock()
        self._build: Future | None = None
        self._pipeline: SelectionPipeline | None = None
        self._ready = False
        self._pending_refresh = False
        self._force_refresh = False
        self._signature: tuple | None = None
        self._source_files: dict = {}
        self._active_requests = 0
        self._retry_at = 0.0
        self._error: str | None = None
        self._checked_at = 0.0
        self._building_forced = False
        self.retired = False
        self.generation = 0
        self.last_used = time.monotonic()

    def refresh(self, *, force: bool = False):
        with self._lock:
            if self.retired:
                raise SelectionBusy("Skill catalog was evicted; reacquire the service")
            self._force_refresh |= force
            if self._build is not None:
                self._pending_refresh |= force
                return
            if not force and (self._ready or time.monotonic() < self._retry_at):
                return
            try:
                self._submit_build()
            except SelectionBusy:
                self._pending_refresh |= force
                raise

    def _submit_build(self):
        background_token = background.set(True)
        budget_token = budget.set(None)
        try:
            self._building_forced = self._force_refresh
            self._build = self.runtime.builds.submit(
                self._prepare_checked, self._pipeline, self._signature,
                self._source_files, self._force_refresh,
            )
            self._force_refresh = False
        except SelectionBusy:
            self._retry_at = time.monotonic() + 1
            raise
        finally:
            budget.reset(budget_token)
            background.reset(background_token)
        # The callback may run synchronously for an already completed Future.
        submitted = self._build
        submitted.add_done_callback(self._after_build)
        return submitted

    def _after_build(self, completed):
        with self._lock:
            if self._build is not completed:
                return
            self._build = None
            self._building_forced = False
            try:
                prepared = completed.result()
            except BaseException as exc:
                self._error = str(exc)
                self._force_refresh = True
                self._retry_at = time.monotonic() + 5
                logger.warning(
                    "[SkillSelection] background preparation failed: %s", exc
                )
                # Keep the old object alive for in-flight readers, but a new
                # search must not silently return it after validation failed.
            else:
                if not self.retired:
                    self._pipeline, self._ready = prepared.pipeline, True
                    self._signature, self._source_files = prepared.signature, prepared.source_files
                    if prepared.changed:
                        self.generation += 1
                        logger.info(
                            '[SkillSelection] ready skills=%s generation=%s scheme=bm25-main-llm',
                            len(prepared.pipeline.documents) if prepared.pipeline else 0,
                            self.generation,
                        )
                    self._error = None
                    self._checked_at = time.monotonic()
                    self._retry_at = 0
            if self._pending_refresh and not self.retired:
                self._pending_refresh = False
                try:
                    self._submit_build()
                except SelectionBusy:
                    self._pending_refresh = True

    def evict_if_idle(self) -> bool:
        with self._lock:
            if self._active_requests or self._build is not None:
                return False
            self.retired = True
            self._pipeline = None
            self._signature = None
            self._source_files = {}
            self._ready = False
            return True

    def _prepare_checked(self, previous, signature, source_files, force):
        current = catalog_signature(self.roots)
        if not force and current == signature and sources_unchanged(source_files):
            return _PreparedCatalog(previous, signature, source_files, False)
        # Do not publish a mixture of files from the middle of an installation.
        # A continuously changing directory fails boundedly; a later search retries.
        for _ in range(3):
            observed = {}
            pipeline = self._prepare(observed)
            after = catalog_signature(self.roots)
            if current == after and sources_unchanged(observed):
                return _PreparedCatalog(pipeline, after, observed, True)
            current = after
        raise SelectionBusy('Skill files are changing; retry after the update finishes')

    def _prepare(self, observed_files=None) -> SelectionPipeline | None:
        started = time.perf_counter()
        documents = read_catalog(
            self.roots, text_max_chars=self.settings.text_max_chars, text_mode="capability",
            observed_files=observed_files,
        )
        catalogued = time.perf_counter()
        if not documents:
            emit(
                logger,
                "prepare",
                configuration_metadata(self.settings),
                status="empty_catalog",
                catalog_count=0,
                total_ms=(catalogued - started) * 1000,
            )
            return None
        pipeline = SelectionPipeline(self.settings)
        pipeline.prepare(documents)
        indexed = time.perf_counter()
        emit(
            logger,
            "prepare",
            configuration_metadata(self.settings),
            status="ready",
            catalog_count=len(documents),
            timings_ms={
                "catalog": (catalogued - started) * 1000,
                "index_prepare": (indexed - catalogued) * 1000,
            },
            total_ms=(time.perf_counter() - started) * 1000,
        )
        return pipeline

    async def _snapshot(self, *, allow_stale=False):
        """Validate once per search, coalescing concurrent checks/builds.

        No timer or idle polling: unchanged files reuse the same index. A
        forced refresh in progress is awaited rather than serving the old one.
        """
        with self._lock:
            if self.retired:
                raise SelectionBusy('Skill catalog evicted')
            # Interactive searches may use the last verified snapshot while
            # a bounded background check runs. Forced refreshes await freshness;
            # selected files and permissions are always rechecked before loading.
            verified = allow_stale and self._ready and self._error is None
            refresh_required = self._force_refresh or self._pending_refresh or self._building_forced
            if verified and not refresh_required:
                if (self._build is None and
                        time.monotonic() - self._checked_at >= self.settings.catalog_check_interval_s):
                    self._submit_build()
                return self._pipeline, self.generation
            if self._build is None:
                if time.monotonic() < self._retry_at:
                    raise SelectionBusy(self._error or 'Skill preparation capacity exhausted')
                self._pending_refresh = False
                build = self._submit_build()
            else:
                build = self._build
        while True:
            try:
                await asyncio.shield(asyncio.wrap_future(build))
            except SelectionBusy as exc:
                # A forced refresh may already supersede a failed installation
                # snapshot. Follow that build; keep unrecovered failures visible.
                self._after_build(build)
                with self._lock:
                    if self._build is None and self._error is not None:
                        raise exc
            self._after_build(build)
            with self._lock:
                if self.retired:
                    raise SelectionBusy("Skill catalog evicted")
                if self._build is None:
                    if self._pending_refresh:
                        if time.monotonic() < self._retry_at:
                            raise SelectionBusy('Skill preparation capacity exhausted')
                        self._pending_refresh = False
                        build = self._submit_build()
                        continue
                    elif self._error is not None:
                        raise SelectionBusy(self._error)
                    else:
                        return self._pipeline, self.generation
                build = self._build

    async def current_snapshot(self, *, allow_stale=False):
        """Validate before the rail consults its permission/result caches."""
        with self.runtime.request():
            with self._lock:
                self._active_requests += 1
                self.last_used = time.monotonic()
            try:
                # A hot change may need the same full build as startup. Keep
                # the shorter query deadline for BM25 execution, not indexing.
                async with asyncio.timeout(self.settings.startup_timeout_s):
                    return await self._snapshot(allow_stale=allow_stale)
            except TimeoutError as exc:
                self.runtime.timeout()
                raise SelectionTimedOut('Skill catalog refresh timed out') from exc
            finally:
                with self._lock:
                    self._active_requests -= 1

    async def search(
        self, query: str, keywords=(), *, allowed_ids: frozenset[str], snapshot=None,
    ) -> SelectionResult:
        if len(query) > self.settings.max_query_chars:
            raise ValueError("Skill query exceeds max_query_chars")
        if len(allowed_ids) > 10000:
            raise ValueError("Skill scope exceeds 10000 entries")
        if not query.strip() or not allowed_ids:
            return SelectionResult(
                generation=snapshot[1] if snapshot is not None else 0,
                diagnostics={
                    "reason": "empty_query"
                    if not query.strip()
                    else "no_allowed_skills",
                    "allowed_count": len(allowed_ids),
                },
            )
        with self.runtime.request():
            with self._lock:
                if self.retired:
                    raise SelectionBusy("Skill catalog evicted")
                self._active_requests += 1
                self.last_used = time.monotonic()
            work = None
            request_budget = None
            try:
                async with asyncio.timeout(self.settings.startup_timeout_s):
                    snapshot_started = time.perf_counter()
                    # The rail may already have validated a snapshot before
                    # refreshing native permissions and checking its result cache.
                    pipeline, generation = snapshot if snapshot is not None else await self._snapshot()
                    snapshot_ms = (time.perf_counter() - snapshot_started) * 1000
                    if pipeline is None:
                        return SelectionResult(
                            generation=generation,
                            diagnostics={
                                "reason": "empty_catalog",
                                "catalog_count": 0,
                                "snapshot_wait_ms": snapshot_ms,
                            },
                        )
                    request_budget = WorkBudget(
                        time.monotonic() + self.settings.query_timeout_s,
                        threading.Event(),
                    )
                    token = budget.set(request_budget)
                    try:
                        work = self.runtime.queries.submit(
                            self._query,
                            pipeline,
                            query,
                            keywords,
                            allowed_ids,
                            time.perf_counter(),
                        )
                    finally:
                        budget.reset(token)
                    async with asyncio.timeout(self.settings.query_timeout_s):
                        result = await asyncio.wrap_future(work)
                        return replace(
                            result,
                            generation=generation,
                            diagnostics={
                                **result.diagnostics,
                                "snapshot_wait_ms": snapshot_ms,
                            },
                        )
            except TimeoutError as exc:
                self.runtime.timeout()
                raise SelectionTimedOut("Skill selection timed out") from exc
            finally:
                if request_budget is not None:
                    request_budget.cancelled.set()
                if work is not None:
                    work.cancel()
                with self._lock:
                    self._active_requests -= 1

    @staticmethod
    def _query(pipeline, query, keywords, allowed_ids, submitted_at):
        queue_ms = (time.perf_counter() - submitted_at) * 1000
        checkpoint()
        result = pipeline.search(query, keywords, allowed_ids=allowed_ids)
        checkpoint()
        return replace(
            result, diagnostics={**result.diagnostics, "queue_wait_ms": queue_ms}
        )


def get_service(
    roots: tuple[Path, ...], settings: SelectionSettings
) -> SelectionService:
    from .catalog import directory_id

    roots = tuple(Path(directory_id(root)) for root in roots)
    key = (tuple(map(str, roots)), settings.identity())
    runtime = get_runtime()
    with _services_lock:
        service = _services.get(key)
        if service is None or service.retired:
            if service is not None:
                del _services[key]
            while len(_services) >= runtime.max_catalogs:
                for old_key, old in sorted(
                    _services.items(), key=lambda pair: pair[1].last_used
                ):
                    if old.evict_if_idle():
                        del _services[old_key]
                        break
                else:
                    raise SelectionBusy(
                        "All local skill catalogs are active; capacity exhausted"
                    )
            service = SelectionService(roots, settings, runtime=runtime)
            _services[key] = service
        _services.move_to_end(key)
        service.last_used = time.monotonic()
    service.refresh()
    return service
