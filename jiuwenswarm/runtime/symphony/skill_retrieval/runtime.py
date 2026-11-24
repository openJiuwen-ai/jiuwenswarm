"""Live Skill retrieval status and host scheduling for Core taxonomy builds."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from openjiuwen.symphony import SymphonyConfig, SymphonyRuntime

from jiuwenswarm.runtime.symphony.capabilities import build_catalog_fingerprints


@dataclass(frozen=True)
class LiveRetrievalStatus:
    """Collection counts and discovery settings before session overrides."""

    installed_count: int
    installed_enabled_count: int
    candidate_scale: str
    estimated_candidate_tokens: int
    candidate_budget_tokens: int
    layout: str
    index_state: str


async def read_live_retrieval_status(
    config: SymphonyConfig,
    *,
    disabled_capability_ids: Callable[[], Iterable[str]],
    source_by_id: Callable[[], Mapping[str, str]],
) -> LiveRetrievalStatus:
    """Read the collection's live discovery status.

    Args:
        config: Resolved host Symphony configuration.
        disabled_capability_ids: Provide disabled IDs in the configured collection.
        source_by_id: Provide installation provenance by capability ID.

    Returns:
        Flat-view counts and prompt budget together with the active index layout.
    """
    settings = config.retrieval
    runtime = SymphonyRuntime(None, config)
    try:
        flat_config = settings.model_copy(update={"use_existing_index": False})
        installed_count = len(runtime.retrieval.bind_search(config=flat_config).records())
        directory = runtime.retrieval.bind_search(
            config=flat_config,
            disabled_capability_ids=disabled_capability_ids,
            source_by_id=source_by_id,
        )
        installed_enabled_count = len(directory.records())
        snapshot = directory.prompt_snapshot(refresh=False)
        candidate_scale = "small" if snapshot.all_candidates_included else "large"
        if candidate_scale == "large" and settings.use_existing_index:
            directory = runtime.retrieval.bind_search(
                config=settings,
                disabled_capability_ids=disabled_capability_ids,
                source_by_id=source_by_id,
            )
        artifact = directory.artifact
        return LiveRetrievalStatus(
            installed_count=installed_count,
            installed_enabled_count=installed_enabled_count,
            candidate_scale=candidate_scale,
            estimated_candidate_tokens=snapshot.estimated_candidate_tokens,
            candidate_budget_tokens=snapshot.candidate_budget_tokens,
            layout=artifact.layout,
            index_state=artifact.index_state,
        )
    finally:
        await runtime.aclose()


@dataclass
class _BuildJob:
    runtime: SymphonyRuntime
    job_id: str = field(default_factory=lambda: uuid4().hex)
    task: asyncio.Task | None = None
    state: dict[str, Any] = field(default_factory=lambda: {
        "status": "running", "stage": "queued", "build_id": "", "progress": 0.0,
    })
    result: dict[str, Any] | None = None


_JOBS: dict[Path, _BuildJob] = {}
_JOBS_LOCK = RLock()


class SkillTaxonomyRuntime:
    """Schedule Core builds on the host loop and retain their cancellation owner."""

    def __init__(self, *, config: SymphonyConfig, model: Any = None,
                 disabled_capability_ids=None, source_by_id=None) -> None:
        self.config = config.model_copy(deep=True)
        self._runtime = SymphonyRuntime(model, self.config)
        self._model = model
        self._disabled = disabled_capability_ids
        self._sources = source_by_id

    @property
    def index_root(self) -> Path:
        return self._runtime.retrieval.artifact_root

    def start_build(self, *, force: bool = False) -> dict[str, Any]:
        """Start only on an existing host loop; no model or loop is created here."""
        loop = asyncio.get_running_loop()
        if not self.config.retrieval.enabled:
            raise RuntimeError("Symphony retrieval is disabled")
        with _JOBS_LOCK:
            current = _JOBS.get(self.index_root)
            if current is not None and current.task is not None and not current.task.done():
                return self._started_result(current, "Skill taxonomy build is already running.")
            job = _BuildJob(SymphonyRuntime(self._model, self.config))
            job.task = loop.create_task(self._build(job, force, current), name="symphony-skill-taxonomy")
            _JOBS[self.index_root] = job
        return self._started_result(job, "Skill taxonomy build queued.")

    @staticmethod
    def _started_result(job: _BuildJob, message: str) -> dict[str, Any]:
        return {"success": True, "result": message,
                "data": {"state": job.state["status"], "build_id": job.job_id,
                         "core_build_id": str(job.state.get("build_id") or "")}}

    async def _build(self, job: _BuildJob, force: bool, previous: _BuildJob | None) -> dict[str, Any]:
        def progress(state):
            job.state = dict(state)
            if job.state.get("status") == "success":
                job.state["status"] = "running"

        try:
            if previous is not None:
                await previous.runtime.aclose()
            # Only the configured collection is publishable. Session overlays
            # belong exclusively to the Runtime's search binding.
            result = await job.runtime.retrieval.build(
                force=force, progress=progress, disabled_capability_ids=self._disabled,
                source_by_id=self._sources,
            )
            if result.get("success"):
                job.state = {**job.state, "status": "running", "stage": "capability_fingerprints"}
                await build_catalog_fingerprints(self.config, self._model, force=force)
            job.result = result
            job.state = self._runtime.retrieval.status(
                disabled_capability_ids=self._disabled,
                source_by_id=self._sources,
            )
            return result
        except asyncio.CancelledError:
            job.state = {**job.state, "status": "cancelled", "stage": "cancelled"}
            raise
        except Exception as exc:
            message = str(exc)
            job.state = {**job.state, "status": "failed", "stage": "failed", "error": message, "message": message}
            job.result = {"success": False, "result": message, "data": job.state,
                          "error": {"code": "build_failed", "message": message}}
            return job.result

    def status(self, *, build_id: str | None = None) -> dict[str, Any]:
        with _JOBS_LOCK:
            job = _JOBS.get(self.index_root)
        job_state = dict(job.state) if job is not None else {}
        core_build_id = str(job_state.get("build_id") or "")
        if job is not None and job.task is not None and not job.task.done():
            if job_state.get("stage") == "queued":
                # Core binds the artifact root before emitting build progress.
                # Report the host queue until that initialization has completed.
                return {**job_state, "enabled": self.config.retrieval.enabled,
                        "build_id": job.job_id, "core_build_id": core_build_id}
        requested_id = core_build_id if job is not None and build_id == job.job_id else build_id
        state = self._runtime.retrieval.status(build_id=requested_id or None,
                                               disabled_capability_ids=self._disabled, source_by_id=self._sources)
        if job is not None and job.task is not None:
            if not job.task.done() or job_state.get("status") in {"failed", "cancelled"}:
                state = {**state, **job_state, "enabled": self.config.retrieval.enabled}
            # The host job ID stays stable from the queued response onward.
            # Core publication identity remains available as version/core_build_id.
            state = {**state, "build_id": job.job_id, "core_build_id": core_build_id}
        return state

    async def cancel(self, *, build_id: str | None = None) -> dict[str, Any]:
        return await self.cancel_at(self.index_root, build_id=build_id)

    @classmethod
    async def cancel_at(cls, index_root: str | Path, *, build_id: str | None = None) -> dict[str, Any]:
        root = Path(index_root).expanduser().resolve()
        with _JOBS_LOCK:
            job = _JOBS.get(root)
        if job is None or job.task is None or job.task.done():
            if job is not None:
                await job.runtime.aclose()
            return {"success": True, "result": "No running Skill taxonomy build.",
                    "data": {"state": "idle", "build_id": job.job_id if job is not None else build_id or ""}}
        active_id = str(job.state.get("build_id") or "")
        if build_id and build_id not in {active_id, job.job_id}:
            return {"success": False, "result": "Build id does not match the active taxonomy build.",
                    "data": {"state": "running", "build_id": job.job_id, "core_build_id": active_id},
                    "error": {"code": "build_id_mismatch"}}

        async def cancel_owner():
            if job.state.get("stage") in {"queued", "capability_fingerprints"}:
                job.task.cancel()
            else:
                await job.runtime.retrieval.cancel_build(build_id=active_id or None)
            try:
                await asyncio.shield(job.task)
            except asyncio.CancelledError:
                pass
            job.state = {**job.state, "status": "cancelled", "stage": "cancelled"}
            await job.runtime.aclose()

        loop = asyncio.get_running_loop()
        if job.task.get_loop() is loop:
            await cancel_owner()
        else:
            future = asyncio.run_coroutine_threadsafe(cancel_owner(), job.task.get_loop())
            await asyncio.wrap_future(future)
        return {"success": True, "result": "Skill taxonomy build cancellation completed.",
                "data": {"state": "cancelled", "build_id": job.job_id, "core_build_id": active_id}}

    @classmethod
    async def cancel_all(cls) -> None:
        with _JOBS_LOCK:
            roots = tuple(_JOBS)
        for root in roots:
            await cls.cancel_at(root)
