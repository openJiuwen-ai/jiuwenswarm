"""Process-local JiuwenSwarm integration for public Symphony orchestration."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import inspect
import json
import logging
import os
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, TypeAlias

import openjiuwen.symphony as core_symphony  # type: ignore[import-untyped]
from openjiuwen.symphony import FINGERPRINT_ARTIFACT_FILENAME

from jiuwenswarm.common.config import get_config
from jiuwenswarm.server.runtime.skill import load_execution_disabled_skills
from jiuwenswarm.server.runtime.skill.skillpack import (
    read_skillpack_section,
    scan_skillpacks,
)
from jiuwenswarm.symphony.adapter import (
    candidate_ids_from_skill_ids,
    graph_config_from_swarm,
    llm_config_signature,
    model_from_config,
    model_response_observer_from_config,
    orchestration_config_from_swarm,
)
from jiuwenswarm.symphony.llm import LLMConfig, probe_model_connection
from jiuwenswarm.symphony.config import (
    SymphonyConfig,
    evolution_flow_enabled,
    load_symphony_config,
)
from jiuwenswarm.symphony.build import build_graph as service_build_graph
from jiuwenswarm.symphony.build import graph_status
from jiuwenswarm.symphony.graph_state import GRAPH_STATE_FILENAME
from jiuwenswarm.symphony.graph_storage import (
    BUILD_RUNS_DIRNAME,
    CURRENT_POINTER_FILENAME,
    resolve_graph_artifact_dir,
)

logger = logging.getLogger(__name__)

CombinationCandidateType: TypeAlias = Any
SymphonyRuntimeType: TypeAlias = Any
SymphonyRuntime = core_symphony.SymphonyRuntime
normalize_name_key = core_symphony.normalize_name_key
CapabilityPackager = core_symphony.flow.CapabilityPackager
LLMPackageReviewAgent = core_symphony.LLMPackageReviewAgent
PackageReviewGate = core_symphony.flow.PackageReviewGate
SymphonyFlowEngine = core_symphony.flow.SymphonyFlowEngine
SymphonyFlowConfig = core_symphony.orchestration.config.SymphonyFlowConfig
SkillPackAdapter = core_symphony.flow.SkillPackAdapter
VERDICT_APPROVED = core_symphony.flow.VERDICT_APPROVED


def _symphony_flow_config(flow_cfg: Any) -> SymphonyFlowConfig:
    """Pass distill thresholds only when this openjiuwen build accepts them."""

    accepted = inspect.signature(SymphonyFlowConfig).parameters
    kwargs: dict[str, Any] = {}
    if "min_successes" in accepted:
        kwargs["min_successes"] = flow_cfg.min_successes
    else:
        for name in ("min_successes_candidate", "min_successes_verified"):
            if name in accepted:
                kwargs[name] = flow_cfg.min_successes
    if "min_pack_success_rate" in accepted:
        kwargs["min_pack_success_rate"] = flow_cfg.min_pack_success_rate
    elif "min_pack_success_rate_verified" in accepted:
        kwargs["min_pack_success_rate_verified"] = flow_cfg.min_pack_success_rate
    return SymphonyFlowConfig(**kwargs)


ProgressCallback = Callable[[dict[str, Any]], Any]


def experience_request_id(recipe_id: str, version: int) -> str:
    """Return the stable Host request id for one immutable Recipe version."""

    digest = hashlib.sha256(f"{recipe_id}:{version}".encode()).hexdigest()[:20]
    return f"symphony_experience_{digest}"


def _graph_scope_id(graph_dir: Path) -> str:
    digest = hashlib.sha256(str(graph_dir.resolve()).encode()).hexdigest()[:20]
    return f"jiuwenswarm-{digest}"


def _unique_candidates(
    candidates: tuple[CombinationCandidateType, ...],
) -> tuple[CombinationCandidateType, ...]:
    output: list[CombinationCandidateType] = []
    seen: set[tuple[str, int]] = set()
    for candidate in candidates:
        key = (candidate.recipe_id, candidate.version)
        if key not in seen:
            seen.add(key)
            output.append(candidate)
    return tuple(output)


def _candidate_question(
    candidate: CombinationCandidateType,
    *,
    request_id: str,
) -> dict[str, Any]:
    structure = (
        "\n".join(
            f"`{source}` → `{target}`"
            for source, target, _relation in candidate.structure
        )
        or "根据任务需要组合使用"
    )
    package_name = candidate.name or "组合技能包"
    applicability = candidate.applicability or "适合需要多个技能配合完成的类似任务。"
    question = "\n".join(
        (
            "系统发现这套能力组合在类似任务中表现稳定，可以保存为技能包，"
            "以后遇到类似任务时直接使用。",
            "",
            "**技能包名称**",
            package_name,
            "",
            "**适用场景**",
            applicability,
            "",
            "**包含的技能及执行顺序**",
            structure,
        )
    )
    return {
        "event_type": "chat.ask_user_question",
        "request_id": request_id,
        "questions": [
            {
                "header": "发现可复用的技能包",
                "question": question,
                "options": [
                    {
                        "label": "创建技能包",
                        "value": "install",
                        "description": "保存这套能力组合，供以后直接使用。",
                    },
                    {
                        "label": "暂不创建",
                        "value": "defer",
                        "description": "保留这条推荐，本次不创建技能包。",
                    },
                ],
                "multi_select": False,
            }
        ],
        "evolution_meta": {
            "event_kind": "approval",
            "rail_kind": "symphony_experience",
            "approval_kind": "install",
            "request_id": request_id,
            "recipe_id": candidate.recipe_id,
            "recipe_version": candidate.version,
        },
    }


def _candidate_payload(candidate: CombinationCandidateType) -> dict[str, Any]:
    return {
        "recipe_id": candidate.recipe_id,
        "recipe_version": candidate.version,
        "name": candidate.name,
        "applicability": candidate.applicability,
        "structure": [list(item) for item in candidate.structure],
        "execution_count": candidate.execution_count,
        "success_count": candidate.success_count,
        "success_rate": candidate.success_rate,
    }


def _read_install_receipt(flow_root: Path, request_id: str) -> dict[str, Any] | None:
    path = flow_root / "install_receipts" / f"{request_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("request_id") != request_id:
        return None
    return payload if isinstance(payload.get("installed"), bool) else None


def _save_install_receipt(flow_root: Path, receipt: dict[str, Any]) -> None:
    directory = flow_root / "install_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{receipt['request_id']}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class SwarmSymphonyService:
    """Own the process-local Symphony runtime used by all Agent tools."""

    def __init__(self) -> None:
        self._build_guard = asyncio.Lock()
        self._active_build_task: asyncio.Task | None = None
        self._runtime: SymphonyRuntimeType | None = None
        self._runtime_key: tuple[Any, ...] | None = None
        self._flow_started = False
        self._flow_start_task: asyncio.Task | None = None
        self._flow_start_runtime: Any = None
        self._close_task: asyncio.Task | None = None
        self._recovered_candidates: tuple[CombinationCandidateType, ...] = ()
        self._candidate_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._notified_candidates: set[tuple[str, int]] = set()
        self._install_lock = asyncio.Lock()
        self._install_receipts: dict[str, dict[str, Any]] = {}
        self._retired_runtime_tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._graph_status_lock = asyncio.Lock()
        self._graph_status_cache_key: _GraphStatusCacheKey | None = None
        self._graph_status_cache_payload: dict[str, Any] | None = None
        self._graph_status_cache_created_at = 0.0
        self._graph_status_generation = 0

    async def graph_status(
        self,
    ) -> dict[str, Any]:
        config = load_symphony_config()
        skills_root = config.paths.skills_root
        graph_dir = config.paths.graph_dir
        await self._repair_interrupted_build_state(graph_dir)
        cache_key = await asyncio.to_thread(
            _graph_status_cache_key,
            config,
            skills_root,
            graph_dir,
        )
        graph_payload = await self._cached_graph_status(
            config,
            skills_root,
            graph_dir,
            cache_key,
        )
        return await asyncio.to_thread(
            _compose_graph_status_payload,
            graph_payload,
            graph_dir,
        )

    async def _cached_graph_status(
        self,
        config: SymphonyConfig,
        skills_root: Path,
        graph_dir: Path,
        cache_key: _GraphStatusCacheKey,
    ) -> dict[str, Any]:
        """Cache only the expensive graph scan. Build progress stays live."""
        cached = self._graph_status_cache_payload
        if self._graph_status_cache_is_fresh(cache_key) and cached is not None:
            return cached

        def status() -> dict[str, Any]:
            return graph_status(
                skills_root,
                graph_dir,
                # Freshness follows the model identity captured by the published
                # graph. The current default model is an online planning concern
                # and must not make that graph stale merely because it changed.
                llm_config=None,
                symphony_config=config,
            ).to_dict()

        async with self._graph_status_lock:
            cached = self._graph_status_cache_payload
            if self._graph_status_cache_is_fresh(cache_key) and cached is not None:
                return cached
            generation = self._graph_status_generation
            payload = await asyncio.to_thread(status)
            if generation == self._graph_status_generation:
                self._graph_status_cache_key = cache_key
                self._graph_status_cache_payload = payload
                self._graph_status_cache_created_at = time.monotonic()
            return payload

    def _graph_status_cache_is_fresh(self, cache_key: _GraphStatusCacheKey) -> bool:
        if (
            self._graph_status_cache_key != cache_key
            or self._graph_status_cache_payload is None
        ):
            return False
        age = time.monotonic() - self._graph_status_cache_created_at
        return age < _GRAPH_STATUS_CACHE_TTL_SECONDS

    def _invalidate_graph_status_cache(self) -> None:
        """Drop the cached status after a build changes its inputs."""
        self._graph_status_generation += 1
        self._graph_status_cache_key = None
        self._graph_status_cache_payload = None
        self._graph_status_cache_created_at = 0.0

    async def refresh_graph(
        self,
        *,
        force: bool = False,
        progress: ProgressCallback | None = None,
        llm_config: LLMConfig | None = None,
    ) -> dict[str, Any]:
        return await self._build_graph(
            force=force,
            progress=progress,
            llm_config=llm_config,
        )

    async def start_refresh_graph(
        self,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Start or reuse a process-local background graph build."""

        config = load_symphony_config()
        graph_dir = config.paths.graph_dir
        async with self._build_guard:
            self._invalidate_graph_status_cache()
            task = self._active_build_task
            if task is not None and not task.done():
                payload = {
                    "success": True,
                    "background": True,
                    "build_status": "running",
                    "graph_dir": str(graph_dir),
                    "detail": "技能总谱已在后台构建中。",
                }
                payload.update(_build_log_payload(graph_dir))
                build_progress = payload.get("build_progress")
                if (
                    not isinstance(build_progress, dict)
                    or build_progress.get("status") != "running"
                ):
                    payload.update({"build_progress": _starting_build_progress()})
                return payload
            if task is not None:
                self._active_build_task = None
            build_logger = _BuildProcessLogger(graph_dir / "build_log.jsonl")
            build_logger.reset()
            build_logger.record(
                "update.start",
                skills_root=str(config.paths.skills_root),
                out_dir=str(graph_dir),
                force=force,
            )
            task = asyncio.create_task(
                self._build_graph(
                    force=force,
                    progress=None,
                    prestarted=True,
                    config=config,
                ),
                name="symphony-graph-build",
            )
            self._active_build_task = task
            task.add_done_callback(self._consume_background_build_result)

        payload = {
            "success": True,
            "background": True,
            "build_status": "running",
            "graph_dir": str(graph_dir),
            "detail": "技能总谱后台构建已启动。",
        }
        payload.update(_build_log_payload(graph_dir))
        return payload

    async def cancel_build(
        self,
    ) -> dict[str, Any]:
        config = load_symphony_config()
        graph_dir = config.paths.graph_dir
        build_logger = _BuildProcessLogger(graph_dir / "build_log.jsonl")
        async with self._build_guard:
            task = self._active_build_task
            if task is None or task.done():
                if task is not None:
                    self._active_build_task = None
                _repair_interrupted_build_log(graph_dir)
                self._invalidate_graph_status_cache()
                payload = {
                    "success": False,
                    "graph_dir": str(graph_dir),
                    "cancelled": False,
                    "build_status": "idle",
                    "detail": "当前没有正在运行的技能总谱构建。",
                }
                payload.update(_build_log_payload(graph_dir))
                payload["build_status"] = "idle"
                return payload
            build_logger.record("update.cancel_requested")
            task.cancel("skills.graph.cancel")
            build_logger.record("update.cancelled")
            self._invalidate_graph_status_cache()
        try:
            await task
        except asyncio.CancelledError:
            pass
        payload = {
            "success": True,
            "graph_dir": str(graph_dir),
            "cancelled": True,
            "build_status": "cancelled",
            "detail": "已取消技能总谱构建，已完成的缓存和 checkpoint 会保留。",
        }
        payload.update(_build_log_payload(graph_dir))
        return payload

    async def graph(self) -> dict[str, Any]:
        """Return the public graph artifact in the shape consumed by the Web UI."""

        config = load_symphony_config()
        graph_dir = config.paths.graph_dir
        await self._repair_interrupted_build_state(graph_dir)
        try:
            artifact = self._read_graph_artifact(graph_dir)
        except (FileNotFoundError, ValueError) as exc:
            payload = {
                "success": False,
                "graph_dir": str(graph_dir),
                "detail": "技能总谱不存在或不完整，请先构建总谱。",
                "error": str(exc),
            }
        else:
            payload = _web_graph_payload(
                artifact,
                graph_dir=graph_dir,
                min_edge_confidence=config.orchestration.min_edge_confidence,
                disabled_skill_names=load_execution_disabled_skills(),
                dynamic_overlay=None,
                skills_root=config.paths.skills_root,
            )
        payload.update(_build_log_payload(graph_dir))
        _prefer_build_failure_detail(payload)
        return payload

    async def plan(
        self,
        query: str,
        mode: str | None = None,
        candidate_skill_ids: list[str] | None = None,
        *,
        progress: ProgressCallback | None = None,
        llm_config: LLMConfig | None = None,
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            return {"success": False, "detail": "query is required"}
        candidate_ids = candidate_ids_from_skill_ids(candidate_skill_ids)
        language = _resolve_orchestration_language(
            get_config().get("preferred_language", "zh")
        )
        config = load_symphony_config()
        requested_mode = str(mode or config.orchestration.mode).strip()
        try:
            orchestration_config_from_swarm(config, mode=requested_mode)
        except ValueError as exc:
            return {
                "success": False,
                "detail": str(exc),
            }

        status = await self.graph_status()
        if not status.get("success"):
            return {
                "success": False,
                "detail": "Skill Score status check failed before planning",
                "graph_status": status,
            }
        if _graph_needs_build(status):
            refresh_kwargs: dict[str, Any] = {"progress": progress}
            if llm_config is not None:
                refresh_kwargs["llm_config"] = llm_config
            graph_build = await self.refresh_graph(**refresh_kwargs)
            if not graph_build.get("success"):
                failure = {
                    "success": False,
                    "detail": "Skill Score build failed before planning",
                    "graph_status": status,
                    "graph_build": graph_build,
                }
                if graph_build.get("reason") == "graph_preparing":
                    failure.update(
                        {
                            "reason": "graph_preparing",
                            "retryable": False,
                            "build_status": "running",
                            "operation": "plan",
                            "detail": graph_build.get("detail") or failure["detail"],
                        }
                    )
                return failure
            graph_build["rebuilt"] = True
        else:
            graph_build = None
        transient_runtime = llm_config is not None
        runtime = None
        try:
            runtime = (
                self._runtime_for(config, llm_config=llm_config)
                if llm_config is not None
                else self._runtime_for(config)
            )
            public_payload = await runtime.graph_engine.plan(
                query,
                graph_scope_id=runtime.graph_scope_id,
                candidate_ids=candidate_ids,
                language=language,
                progress=progress,
                disabled_capability_ids=load_execution_disabled_skills(),
                mode=requested_mode,
            )
            payload = public_payload.to_dict()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Symphony planning failed")
            payload = {"success": False, "detail": str(exc)}
        finally:
            if transient_runtime and runtime is not None:
                await self._close_transient_runtime(runtime)
        if not isinstance(payload, dict):
            return {
                "success": False,
                "detail": "Symphony orchestration returned an invalid payload",
                "graph_status": status,
                **({"graph_build": graph_build} if graph_build else {}),
            }
        if payload.get("success") is False:
            return {
                "success": False,
                "graph_status": status,
                **({"graph_build": graph_build} if graph_build else {}),
                **payload,
            }
        planned_graph = payload.get("planned_graph")
        if not isinstance(planned_graph, dict):
            return {
                "success": False,
                "detail": "Symphony orchestration returned no planned_graph",
                "graph_status": status,
                **({"graph_build": graph_build} if graph_build else {}),
            }
        result = {
            "success": True,
            "planned_graph": planned_graph,
        }
        if graph_build is not None:
            result["graph_build"] = graph_build
        return result

    async def _build_graph(
        self,
        *,
        force: bool,
        progress: ProgressCallback | None,
        prestarted: bool = False,
        config: SymphonyConfig | None = None,
        llm_config: LLMConfig | None = None,
    ) -> dict[str, Any]:
        config = config or load_symphony_config()
        skills_root = config.paths.skills_root
        graph_dir = config.paths.graph_dir
        current_task = asyncio.current_task()
        async with self._build_guard:
            active_task = self._active_build_task
            if (
                active_task is not None
                and active_task is not current_task
                and not active_task.done()
            ):
                payload = {
                    "success": False,
                    "graph_dir": str(graph_dir),
                    "reason": "graph_preparing",
                    "retryable": False,
                    "build_status": "running",
                    "detail": "已有技能总谱构建正在运行，请等待完成或先取消当前构建。",
                }
                payload.update(_build_log_payload(graph_dir))
                return payload
            self._active_build_task = current_task
            self._invalidate_graph_status_cache()
        progress_dispatcher = _OrderedProgressDispatcher(progress)
        progress_dispatcher.start()
        build_logger = _BuildProcessLogger(
            graph_dir / "build_log.jsonl",
            progress=progress_dispatcher,
        )
        abort_progress = False
        try:
            if not prestarted:
                build_logger.reset()
                build_logger.record(
                    "update.start",
                    skills_root=str(skills_root),
                    out_dir=str(graph_dir),
                    force=force,
                )
            try:
                try:
                    llm_config = llm_config or LLMConfig.from_default_model()
                    model_name = str(getattr(llm_config, "model", "") or "")
                    build_logger.record(
                        "model.probe.start",
                        model=model_name,
                    )
                    await probe_model_connection(llm_config)
                    build_logger.record("model.probe.done", model=model_name)
                except Exception as exc:  # noqa: BLE001
                    error = str(exc).strip() or type(exc).__name__
                    detail = f"主模型连接测试未通过：{error}"
                    build_logger.record(
                        "update.failed",
                        error=error,
                        detail=detail,
                        failure_stage="model.probe",
                    )
                    payload = {
                        "success": False,
                        "graph_dir": str(graph_dir),
                        "detail": detail,
                        "error": error,
                    }
                    payload.update(_build_log_payload(graph_dir))
                    return payload
                result = (
                    await service_build_graph(
                        skills_root,
                        graph_dir,
                        llm_config,
                        force=force,
                        symphony_config=config,
                        build_log=build_logger.record,
                    )
                ).to_dict()
            except asyncio.CancelledError:
                abort_progress = True
                if (
                    _build_progress(_read_build_log(graph_dir)).get("status")
                    != "cancelled"
                ):
                    build_logger.record("update.cancelled")
                payload = {
                    "success": False,
                    "graph_dir": str(graph_dir),
                    "cancelled": True,
                    "build_status": "cancelled",
                    "detail": "技能总谱构建已取消，可再次执行增量构建继续。",
                }
                payload.update(_build_log_payload(graph_dir))
                return payload
            except Exception as exc:  # noqa: BLE001
                error = str(exc).strip() or type(exc).__name__
                detail = f"Symphony 总谱构建失败: {error}"
                build_logger.record(
                    "update.failed",
                    error=error,
                    detail=detail,
                )
                payload = {
                    "success": False,
                    "graph_dir": str(graph_dir),
                    "detail": detail,
                    "error": error,
                }
                payload.update(_build_log_payload(graph_dir))
                return payload
            if result.get("success") is not True:
                result["success"] = False
                build_logger.record("update.failed", **result)
                result.update(_build_log_payload(graph_dir))
                return result
            build_logger.record("update.done", **result)
            result.update(_build_log_payload(graph_dir))
            return result
        finally:
            self._invalidate_graph_status_cache()
            try:
                if abort_progress:
                    await progress_dispatcher.abort()
                else:
                    await progress_dispatcher.close()
            finally:
                await self._clear_active_build_task(current_task)

    def _runtime_for(
        self,
        config,
        *,
        llm_config: LLMConfig | None = None,
    ) -> SymphonyRuntimeType:
        if self._closing:
            raise RuntimeError("Symphony service is closing")
        if llm_config is not None:
            return self._create_runtime(config, llm_config, with_flow=False)
        llm_config = LLMConfig.from_default_model()
        llm_signature = llm_config_signature(llm_config)
        key = (
            str(config.paths.graph_dir),
            config.orchestration.mode,
            config.orchestration.top_k,
            config.orchestration.max_depth,
            config.orchestration.min_edge_confidence,
            evolution_flow_enabled(config),
            llm_signature,
        )
        if self._runtime is None or self._runtime_key != key:
            previous_runtime = self._runtime
            if (
                previous_runtime is not None
                and getattr(previous_runtime, "flow_engine", None) is not None
            ):
                # A Flow store has exactly one process owner. Configuration
                # changes take effect after orderly service restart.
                return previous_runtime
            if previous_runtime is not None:
                self._retire_runtime(previous_runtime)
            self._runtime = self._create_runtime(config, llm_config, with_flow=True)
            self._runtime_key = key
            self._flow_started = False
            self._notified_candidates.clear()
        return self._runtime

    @staticmethod
    def _create_runtime(
        config: Any,
        llm_config: LLMConfig,
        *,
        with_flow: bool,
    ) -> SymphonyRuntimeType:
        model = model_from_config(llm_config)
        flow_engine = None
        if with_flow and evolution_flow_enabled(config):
            flow_dir = config.paths.graph_dir.parent / "flow"
            flow_cfg = config.evolution.flow
            flow_engine = SymphonyFlowEngine(
                flow_dir,
                config=_symphony_flow_config(flow_cfg),
                llm_client=model,
                gate=PackageReviewGate(LLMPackageReviewAgent(model)),
                skill_adapter=SkillPackAdapter(),
            )
        return SymphonyRuntime(
            graph_artifact_root=config.paths.graph_dir,
            capability_provider=(),
            model=model,
            model_response_observer=model_response_observer_from_config(llm_config),
            orchestration_config=orchestration_config_from_swarm(config),
            graph_config=graph_config_from_swarm(config),
            flow_engine=flow_engine,
            graph_scope_id=_graph_scope_id(config.paths.graph_dir),
        )

    @staticmethod
    async def _close_transient_runtime(runtime: SymphonyRuntimeType) -> None:
        close = getattr(runtime, "aclose", None)
        if callable(close):
            await close()
            return
        close = getattr(runtime, "close", None)
        if callable(close):
            close()

    def _retire_runtime(self, runtime: SymphonyRuntimeType) -> None:
        """Close a replaced Runtime without blocking synchronous Rail assembly."""

        if getattr(runtime, "flow_engine", None) is None:
            close = getattr(runtime, "close", None)
            if callable(close):
                close()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(runtime.aclose())
            return
        task = loop.create_task(runtime.aclose(), name="symphony-runtime-retire")
        self._retired_runtime_tasks.add(task)
        task.add_done_callback(self._consume_retired_runtime_result)

    def _consume_retired_runtime_result(self, task: asyncio.Task[None]) -> None:
        self._retired_runtime_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Retired Symphony runtime close failed (%s)",
                type(exc).__name__,
            )

    def runtime(self) -> SymphonyRuntimeType:
        """Return the process-local Core runtime for Rail assembly."""

        return self._runtime_for(load_symphony_config())

    async def submit_evolution_and_notify(
        self,
        planned_graph: dict[str, Any] | None,
        execution_graph: dict[str, Any],
        *,
        session_id: str,
        capture_mode: str,
        channel_id: str | None,
    ) -> None:
        """Submit one Rail graph pair and deliver any new install candidates."""

        config = load_symphony_config()
        if not config.enabled or not evolution_flow_enabled(config):
            return
        runtime = self._runtime_for(config)
        result = await runtime.submit_evolution(
            planned_graph,
            execution_graph,
            session_id=session_id,
            capture_mode=capture_mode,
        )
        recovered = ()
        try:
            started_candidates = await self._start_flow(runtime)
            # Background recovery may finish while this caller awaits the same
            # single-flight. Read its retained candidates after that await.
            recovered = (*self._recovered_candidates, *started_candidates)
            if execution_graph.get("outcome") == "success":
                self._recovered_candidates = ()
            else:
                # Startup recovery has no originating session. Keep those
                # candidates queued until a successful task provides an
                # appropriate conversation context; a cancelled/partial run
                # must not surface an unrelated historical recommendation.
                self._recovered_candidates = _unique_candidates(recovered)
                recovered = ()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Symphony Flow startup recovery failed (%s)",
                type(exc).__name__,
            )
        candidates = _unique_candidates((*recovered, *result.new_candidates))
        for candidate in candidates:
            try:
                await self._notify_candidate(
                    candidate, session_id=session_id, channel_id=channel_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Symphony candidate delivery failed (%s)",
                    type(exc).__name__,
                )

    async def _start_flow(
        self,
        runtime: SymphonyRuntimeType,
    ) -> tuple[CombinationCandidateType, ...]:
        if self._closing:
            raise RuntimeError("Symphony service is closing")
        if runtime.flow_engine is None or self._flow_started:
            return ()
        task = self._flow_start_task
        if task is not None and self._flow_start_runtime is not runtime:
            if not task.done():
                raise RuntimeError("Symphony Flow is preparing for another runtime")
            task = None
        retry = task is None
        if task is not None:
            retry = task.cancelled() or (task.done() and task.exception() is not None)
        if retry:
            task = asyncio.create_task(runtime.flow_engine.start(), name="symphony-flow-start")
            self._flow_start_task = task
            self._flow_start_runtime = runtime
            task.add_done_callback(self._consume_flow_start_result)
        candidates = await asyncio.shield(task)
        if self._closing:
            raise RuntimeError("Symphony service is closing")
        # The recovered candidates have exactly one claimant. Other callers
        # consume the shared retained queue through submit_evolution_and_notify.
        if self._flow_started:
            return ()
        self._flow_started = True
        return tuple(candidates)

    async def start(self) -> None:
        """Start Flow recovery when the feature is enabled."""

        config = load_symphony_config()
        if not config.enabled or not evolution_flow_enabled(config):
            return
        runtime = self._runtime_for(config)
        recovered = await self._start_flow(runtime)
        if recovered:
            self._recovered_candidates = _unique_candidates(
                (*self._recovered_candidates, *recovered)
            )

    async def _notify_candidate(
        self,
        candidate: CombinationCandidateType,
        *,
        session_id: str,
        channel_id: str | None,
        force: bool = False,
    ) -> None:
        key = (candidate.recipe_id, candidate.version)
        lock = self._candidate_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._notified_candidates and not force:
                return
            request_id = experience_request_id(*key)
            payload = _candidate_question(candidate, request_id=request_id)
            from jiuwenswarm.runtime.host_services import RuntimeHostPushTransport
            from jiuwenswarm.server.runtime.agent_adapter.evolution_helpers import (
                EvolutionPushContext,
                push_evolution_event,
            )
            from jiuwenswarm.server.runtime.session.session_metadata import (
                build_server_push_message,
            )

            await push_evolution_event(
                EvolutionPushContext(
                    transport=RuntimeHostPushTransport(),
                    channel_id=channel_id,
                    session_id=session_id,
                ),
                request_id,
                payload,
                build_server_push_message,
            )
            # Delivery succeeded. Keep the process-local guard even if the
            # user has not answered yet; otherwise concurrent submissions can
            # show the same prompt again.
            self._notified_candidates.add(key)

    def defer_candidate(self, recipe_id: str, version: int) -> None:
        """Allow a deferred candidate to be offered by a later successful run."""

        key = (recipe_id, version)
        self._notified_candidates.discard(key)
        runtime = self._runtime
        if runtime is None or runtime.flow_engine is None:
            return
        try:
            runtime.flow_engine.release_candidate(*key)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Symphony deferred candidate release failed (%s)",
                type(exc).__name__,
            )

    @staticmethod
    def _acknowledge_installed_candidate(
        flow: Any,
        recipe_id: str,
        recipe_version: int,
    ) -> None:
        try:
            flow.acknowledge_candidate(recipe_id, recipe_version)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Symphony installed candidate acknowledgement failed (%s)",
                type(exc).__name__,
            )

    def list_experience_candidates(self) -> dict[str, Any]:
        """Return installable Recipe versions retained by the Core Flow store."""

        config = load_symphony_config()
        if not config.enabled or not evolution_flow_enabled(config):
            return {"success": True, "enabled": False, "candidates": []}
        flow = self._runtime_for(config).flow_engine
        candidates = flow.list_candidates() if flow is not None else ()
        return {
            "success": True,
            "enabled": True,
            "candidates": [_candidate_payload(item) for item in candidates],
        }

    async def request_experience_candidate(
        self,
        *,
        recipe_id: str,
        recipe_version: int,
        session_id: str,
        channel_id: str | None,
    ) -> dict[str, Any]:
        """Re-send one retained candidate through the existing Host question flow."""

        config = load_symphony_config()
        if not config.enabled or not evolution_flow_enabled(config):
            return {"success": False, "reason": "flow_disabled"}
        flow = self._runtime_for(config).flow_engine
        candidate = (
            flow.get_candidate(recipe_id, version=recipe_version)
            if flow is not None
            else None
        )
        if candidate is None:
            return {"success": False, "reason": "candidate_not_found"}
        await self._notify_candidate(
            candidate,
            session_id=session_id,
            channel_id=channel_id,
            force=True,
        )
        return {
            "success": True,
            "request_id": experience_request_id(recipe_id, recipe_version),
            "candidate": _candidate_payload(candidate),
        }

    async def install_candidate(
        self,
        *,
        request_id: str,
        recipe_id: str,
        recipe_version: int,
        package_id: str | None,
        integrity: str | None,
        skill_manager: Any,
    ) -> dict[str, Any]:
        """Prepare, validate, and install one server-owned Skill artifact."""

        expected_request_id = experience_request_id(recipe_id, recipe_version)
        if request_id != expected_request_id:
            return {"installed": False, "reason": "invalid_request_id"}
        config = load_symphony_config()
        if not config.enabled or not evolution_flow_enabled(config):
            return {"installed": False, "reason": "flow_disabled"}
        async with self._install_lock:
            previous = self._install_receipts.get(request_id)
            if previous is not None and previous.get("installed") is True:
                flow = self.runtime().flow_engine
                if flow is not None:
                    self._acknowledge_installed_candidate(
                        flow, recipe_id, recipe_version
                    )
                return {**previous, "newly_installed": False, "replayed": True}
            self._install_receipts.pop(request_id, None)
            runtime = self.runtime()
            flow = runtime.flow_engine
            if flow is None:
                return {"installed": False, "reason": "flow_disabled"}
            persisted = _read_install_receipt(flow.store.root.resolve(), request_id)
            if persisted is not None and persisted.get("installed") is True:
                self._install_receipts[request_id] = dict(persisted)
                self._acknowledge_installed_candidate(flow, recipe_id, recipe_version)
                return {**persisted, "newly_installed": False, "replayed": True}
            preparation = await flow.review_and_prepare_install(
                recipe_id,
                recipe_version=recipe_version,
            )
            package = preparation.package or {}
            actual_package_id = str(package.get("package_id") or "")
            actual_integrity = str(package.get("integrity") or "")
            if preparation.verdict != VERDICT_APPROVED:
                result = {
                    "installed": False,
                    "newly_installed": False,
                    "replayed": False,
                    "reason": preparation.verdict,
                    "details": list(preparation.reasons),
                    "recipe_id": recipe_id,
                    "recipe_version": recipe_version,
                    "request_id": request_id,
                }
                return result
            if package_id and package_id != actual_package_id:
                return {"installed": False, "reason": "package_id_mismatch"}
            if integrity and integrity != actual_integrity:
                return {"installed": False, "reason": "integrity_mismatch"}
            if not CapabilityPackager.verify_package_integrity(package):
                return {"installed": False, "reason": "invalid_integrity"}
            flow_root = flow.store.root.resolve()
            artifact_value = preparation.artifact_dir
            if not artifact_value:
                return {"installed": False, "reason": "missing_artifact"}
            artifact_path = Path(artifact_value)
            artifact_dir = artifact_path.resolve()
            expected_artifact_dir = (
                flow_root / "packages" / actual_package_id / "skill"
            ).resolve()
            if (
                artifact_dir != expected_artifact_dir
                or not artifact_dir.is_dir()
                or artifact_path.is_symlink()
            ):
                return {"installed": False, "reason": "invalid_artifact"}
            recover = getattr(skill_manager, "recover_symphony_skill_install", None)
            installed = (
                recover(
                    artifact_dir,
                    package_id=actual_package_id,
                    integrity=actual_integrity,
                )
                if callable(recover)
                else None
            )
            if installed is None:
                try:
                    installed = skill_manager.install_symphony_skill_artifact(
                        artifact_dir,
                        expected_root=flow_root,
                        package_id=actual_package_id,
                        integrity=actual_integrity,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Symphony Skill install failed (%s)",
                        type(exc).__name__,
                    )
                    if getattr(exc, "code", "") in {
                        "SKILL_INVALID_METADATA",
                        "SKILL_INVALID_PACKAGE",
                    }:
                        return {
                            "installed": False,
                            "reason": "not_installable",
                            "details": [str(exc)],
                        }
                    return {"installed": False, "reason": "install_failed"}
            receipt = {
                "installed": bool(installed.get("success")),
                "newly_installed": bool(installed.get("success")),
                "replayed": False,
                "recipe_id": recipe_id,
                "recipe_version": recipe_version,
                "package_id": actual_package_id,
                "integrity": actual_integrity,
                "request_id": request_id,
                "skill": installed.get("skill"),
            }
            if receipt["installed"]:
                _save_install_receipt(flow_root, receipt)
                self._install_receipts[request_id] = dict(receipt)
                self._acknowledge_installed_candidate(flow, recipe_id, recipe_version)
            return receipt

    async def close(self) -> None:
        """Drain Flow and Graph workers and release the process runtime."""

        if self._close_task is None or self._close_task.done():
            self._closing = True
            self._close_task = asyncio.create_task(self._close_owned(), name="symphony-service-close")
            self._close_task.add_done_callback(self._consume_close_result)
        await asyncio.shield(self._close_task)

    async def _close_owned(self) -> None:
        preparation, self._flow_start_task = self._flow_start_task, None
        self._flow_start_runtime = None
        if preparation is not None and not preparation.done():
            preparation.cancel()
            await asyncio.gather(preparation, return_exceptions=True)
        runtime, self._runtime = self._runtime, None
        self._runtime_key = None
        self._flow_started = False
        self._recovered_candidates = ()
        self._candidate_locks.clear()
        self._notified_candidates.clear()
        runtime_error: BaseException | None = None
        try:
            if runtime is not None:
                await runtime.aclose()
        except BaseException as exc:
            runtime_error = exc
        finally:
            retired = tuple(self._retired_runtime_tasks)
            if retired:
                await asyncio.gather(*retired, return_exceptions=True)
            self._retired_runtime_tasks.clear()
            self._closing = False
        if runtime_error is not None:
            raise runtime_error

    @staticmethod
    def _consume_flow_start_result(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Symphony Flow preparation failed", exc_info=task.exception())

    @staticmethod
    def _consume_close_result(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Symphony service close failed", exc_info=task.exception())

    @staticmethod
    def _read_graph_artifact(graph_dir: Path) -> dict[str, Any]:
        """Read an existing graph without requiring an LLM client configuration."""

        return (
            SymphonyRuntime(
                graph_artifact_root=graph_dir,
                capability_provider=(),
                model=None,
            )
            .orchestration.read()
            .to_dict()
        )

    async def _clear_active_build_task(self, task: asyncio.Task | None) -> None:
        async with self._build_guard:
            if self._active_build_task is task:
                self._active_build_task = None

    @staticmethod
    def _consume_background_build_result(task: asyncio.Task) -> None:
        """Consume unexpected task failures so background builds never leak them."""

        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("Background Skill Graph build failed")

    async def _repair_interrupted_build_state(self, graph_dir: Path) -> bool:
        async with self._build_guard:
            task = self._active_build_task
            if task is not None and not task.done():
                return False
            if task is not None:
                self._active_build_task = None
            repaired = await asyncio.to_thread(_repair_interrupted_build_log, graph_dir)
            if repaired:
                self._invalidate_graph_status_cache()
            return repaired


_SHARED_SERVICE: SwarmSymphonyService | None = None


def get_swarm_symphony_service() -> SwarmSymphonyService:
    global _SHARED_SERVICE
    if _SHARED_SERVICE is None:
        _SHARED_SERVICE = SwarmSymphonyService()
    return _SHARED_SERVICE


def set_swarm_symphony_service(service: SwarmSymphonyService | None) -> None:
    """Override/reset the process service for tests and controlled embedding."""

    global _SHARED_SERVICE
    _SHARED_SERVICE = service


def _graph_needs_build(status: dict[str, Any]) -> bool:
    if not bool(status.get("exists", False)):
        return True
    # A cancelled refresh must not be revived implicitly by the next ordinary
    # task.  Keep using the last atomically published graph until the user (or
    # an explicit refresh action) starts a new build.
    build_progress = status.get("build_progress")
    if isinstance(build_progress, dict) and build_progress.get("status") == "cancelled":
        return False
    if bool(status.get("stale", False)):
        return True
    for key in ("added_count", "changed_count", "removed_count"):
        try:
            if int(status.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _web_graph_payload(
    artifact: dict[str, Any],
    *,
    graph_dir: Path,
    min_edge_confidence: float,
    disabled_skill_names: set[str] | list[str] | tuple[str, ...],
    dynamic_overlay: dict[str, Any] | None,
    skills_root: Path | None = None,
) -> dict[str, Any]:
    """Adapt the public capability graph for the existing Skill Graph panel."""

    skills = []
    web_node_refs: dict[str, str] = {}
    web_node_types: dict[str, str] = {}
    disabled_refs = {
        _normalize_capability_ref(value)
        for value in disabled_skill_names
        if _normalize_capability_ref(value)
    }
    disabled_ids: set[str] = set()
    for raw_capability in artifact.get("capabilities") or []:
        if not isinstance(raw_capability, dict):
            continue
        capability = dict(raw_capability)
        capability_id = _capability_id(
            capability.get("capability_id") or capability.get("id")
        )
        if not capability_id:
            continue
        capability["id"] = capability_id
        capability_type = str(
            capability.get("capability_type") or capability.get("type") or "skill"
        )
        capability["type"] = capability_type
        if (
            _normalize_capability_ref(capability_id) in disabled_refs
            or _normalize_capability_ref(capability.get("name")) in disabled_refs
        ):
            disabled_ids.add(capability_id)
            continue
        skills.append(capability)
        web_node_refs[capability_id] = (
            f"skill:{capability_id}"
            if capability_type == "skill"
            else f"capability:{capability_id}"
        )
        web_node_types[capability_id] = capability_type

    nodes: list[dict[str, Any]] = []
    for node in artifact.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        capability_id = _capability_id(node.get("id"))
        if capability_id in disabled_ids:
            continue
        web_node = dict(node)
        if capability_id in web_node_refs:
            web_node["id"] = web_node_refs[capability_id]
            web_node["type"] = web_node_types[capability_id]
        nodes.append(web_node)

    edges: list[dict[str, Any]] = []
    for edge in artifact.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        source_id = _capability_id(edge.get("source"))
        target_id = _capability_id(edge.get("target"))
        if source_id in disabled_ids:
            continue
        if target_id in disabled_ids:
            continue
        web_edge = dict(edge)
        web_edge["source"] = web_node_refs.get(
            source_id,
            str(edge.get("source") or ""),
        )
        web_edge["target"] = web_node_refs.get(
            target_id,
            str(edge.get("target") or ""),
        )
        edges.append(web_edge)

    graph = {"nodes": nodes, "edges": edges}

    # Scan installed SkillPacks from the workspace skills directory; the graph,
    # the skills page, and the execution-disabled check all read one source.
    pack_nodes = []
    pack_edges = []
    pack_member_nodes = []  # Track member skill nodes that need to be added
    try:
        skills_dir = Path(skills_root) if skills_root is not None else graph_dir.parent.parent / "skills"
        for definition in scan_skillpacks(skills_dir):
            pack_id = definition.name
            member_ids = list(definition.members)
            pack_node_id = f"pack:{pack_id}"
            description = definition.description or pack_id
            # 节点 label 用包名（display_name 优先，frontmatter name 兜底），
            # 描述保留在 properties 里由详情面板展示
            label_source = definition.display_name or pack_id
            label = label_source[:50] + "..." if len(label_source) > 50 else label_source
            pack_dir = skills_dir / pack_id
            pack_nodes.append({
                "id": pack_node_id,
                "type": "skill_pack",
                "label": label,
                "properties": {
                    "pack_id": pack_id,
                    "member_ids": member_ids,
                    "description": description,
                    "task_description": description,
                    "execution_narrative": read_skillpack_section(
                        pack_dir, "Execution Process"
                    ),
                },
            })
            # Create contains edges from pack to each member
            for member_id in member_ids:
                member_ref = web_node_refs.get(member_id)
                if not member_ref:
                    # Member skill not in main graph, create a node for it
                    member_ref = f"skill:{member_id}"
                    pack_member_nodes.append({
                        "id": member_ref,
                        "type": "skill",
                        "label": member_id,
                        "properties": {"id": member_id, "name": member_id},
                    })
                pack_edges.append({
                    "source": pack_node_id,
                    "target": member_ref,
                    "type": "contains",
                    "confidence": 1.0,
                })
    except Exception:  # noqa: BLE001
        # If pack scanning fails, continue without packs
        pass

    result_graph = _graph_with_runtime_weights(graph, dynamic_overlay)

    # Add pack member nodes that are not in the main graph
    existing_node_ids = {node.get("id") for node in result_graph.get("nodes", [])}
    for member_node in pack_member_nodes:
        if member_node["id"] not in existing_node_ids:
            result_graph["nodes"].append(member_node)

    result_graph["pack_nodes"] = pack_nodes
    result_graph["pack_edges"] = pack_edges

    return {
        "success": True,
        "graph_dir": str(graph_dir),
        "graph_manifest": dict(artifact.get("config") or {}),
        "orchestration_min_edge_confidence": min_edge_confidence,
        "skills": skills,
        "graph": result_graph,
        "diagnostics": {"diagnostics": list(artifact.get("diagnostics") or [])},
    }


def _graph_with_runtime_weights(
    graph: dict[str, Any],
    overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    runtime_edges = overlay.get("edges") if isinstance(overlay, dict) else None
    if not isinstance(runtime_edges, dict) or not runtime_edges:
        return graph
    edges = []
    for raw_edge in graph.get("edges") or []:
        edge = dict(raw_edge)
        key = (
            f"{_capability_id(edge.get('source'))}->"
            f"{_capability_id(edge.get('target'))}:"
            f"{str(edge.get('type') or 'can_feed')}"
        )
        stats = runtime_edges.get(key)
        if isinstance(stats, dict):
            edge["runtime_weight"] = float(stats.get("runtime_weight") or 1.0)
        edges.append(edge)
    return {**graph, "edges": edges}


def _capability_id(value: Any) -> str:
    return str(value or "").removeprefix("skill:").removeprefix("capability:")


def _normalize_capability_ref(value: Any) -> str:
    return normalize_name_key(_capability_id(value))


def _resolve_orchestration_language(value: Any = None) -> str:
    language = str(value or "").strip().lower()
    if language == "zh":
        language = "cn"
    return language if language in {"cn", "en"} else "cn"


_BUILD_STAGE_LABELS = {
    "update.start": "开始构建技能总谱",
    "model.probe.start": "测试主模型连接",
    "model.probe.done": "主模型连接测试通过",
    "update.cancel_requested": "正在取消技能总谱构建",
    "update.cancelled": "技能总谱构建已取消",
    "scan.start": "扫描技能目录",
    "scan.done": "技能目录扫描完成",
    "diff.done": "计算技能变更",
    "fingerprint.reuse": "复用技能指纹",
    "fingerprint.parse.start": "解析技能指纹",
    "fingerprint.extract.start": "提取技能指纹",
    "fingerprint.normalize.start": "规范化技能指纹",
    "fingerprint.done": "技能指纹处理完成",
    "artifact.fingerprints.write.start": "写入技能指纹文件",
    "artifact.fingerprints.write.done": "技能指纹文件写入完成",
    "graph.build.start": "构建技能关系图",
    "graph.registry.start": "注册技能节点",
    "graph.registry.done": "技能节点注册完成",
    "graph.candidates.start": "生成候选关系",
    "graph.candidates.done": "候选关系生成完成",
    "graph.resolve.start": "解析候选关系",
    "graph.resolve.progress": "解析候选关系",
    "graph.resolve.done": "候选关系解析完成",
    "graph.materialize.start": "生成总谱结构",
    "graph.materialize.done": "总谱结构生成完成",
    "graph.lookup.start": "构建总谱检索结构",
    "graph.lookup.done": "总谱检索结构构建完成",
    "graph.build.done": "技能关系图构建完成",
    "artifact.graph.write.start": "写入总谱文件",
    "artifact.graph.write.done": "总谱文件写入完成",
    "state.write.start": "写入总谱状态",
    "state.write.done": "总谱状态写入完成",
    "update.failed": "总谱构建失败",
    "update.done": "总谱构建完成",
}


def _starting_build_progress() -> dict[str, Any]:
    return {
        "stage": "update.start",
        "label": _BUILD_STAGE_LABELS["update.start"],
        "percent": _BUILD_STAGE_PROGRESS["update.start"],
        "status": "running",
    }


_BUILD_STAGE_PROGRESS = {
    "update.start": 3,
    "model.probe.start": 5,
    "model.probe.done": 7,
    "update.cancel_requested": 100,
    "update.cancelled": 100,
    "scan.start": 8,
    "scan.done": 14,
    "diff.done": 20,
    "artifact.fingerprints.write.start": 52,
    "artifact.fingerprints.write.done": 55,
    "graph.build.start": 58,
    "graph.registry.start": 63,
    "graph.registry.done": 65,
    "graph.candidates.start": 66,
    "graph.candidates.done": 70,
    "graph.resolve.start": 72,
    "graph.resolve.done": 84,
    "graph.materialize.start": 86,
    "graph.materialize.done": 88,
    "graph.lookup.start": 90,
    "graph.lookup.done": 92,
    "graph.build.done": 94,
    "artifact.graph.write.start": 95,
    "artifact.graph.write.done": 96,
    "state.write.start": 98,
    "state.write.done": 99,
    "update.failed": 100,
    "update.done": 100,
}


@dataclass(frozen=True)
class _FileStamp:
    """File identity used to invalidate graph-status caches."""

    mtime_ns: int
    ctime_ns: int
    size: int
    inode: int
    device: int


def _path_stamp(path: Path) -> _FileStamp | None:
    """Return the identity used to invalidate graph-status caches."""
    try:
        info = path.stat()
    except OSError:
        return None
    return _FileStamp(
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_ctime_ns,
        size=info.st_size,
        inode=info.st_ino,
        device=info.st_dev,
    )


# Keep these rules aligned with the pinned Symphony Scanner. The cache key must
# change when the Scanner would read different Skill inputs, and no more often.
_MAX_CACHE_SCAN_DIRECTORIES = 50_000
_MAX_CACHE_DIRECTORY_ENTRIES = 20_000
_MAX_CACHE_ENTRYPOINTS = 10_000
_MAX_CACHE_ASSET_FILES_PER_SKILL = 10_000
_MAX_CACHE_ASSET_FILES = 100_000
_EXCLUDED_CACHE_DIRECTORY_NAMES = frozenset(
    {
        ".cache",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        ".ssh",
        "__pycache__",
        "cache",
        "caches",
        "credential",
        "credentials",
        "key",
        "keys",
        "node_modules",
        "private_keys",
        "secret",
        "secrets",
    }
)
_SENSITIVE_DIRECTORY_TOKENS = frozenset(
    {
        "credential",
        "credentials",
        "key",
        "keys",
        "password",
        "passwords",
        "private",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_SENSITIVE_FILE_TOKENS = frozenset(
    {
        "credential",
        "credentials",
        "key",
        "keys",
        "password",
        "private",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_KEY_MATERIAL_SUFFIXES = frozenset(
    {
        ".der",
        ".jks",
        ".key",
        ".keystore",
        ".p12",
        ".pem",
        ".pfx",
        ".pkcs12",
        ".ppk",
        ".pub",
    }
)
_SENSITIVE_EXACT_FILENAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "authorized_keys",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)


# Token checks intentionally under-exclude compared with the Scanner. Extra
# files only cause a cache miss; a false exclusion would hide a real change.
@lru_cache(maxsize=4096)
def _cache_name_tokens(name: str) -> frozenset[str]:
    normalized = "".join(
        char if char.isalnum() else "_" for char in name.casefold()
    )
    return frozenset(token for token in normalized.split("_") if token)


def _cache_excludes_directory(name: str) -> bool:
    lowered = name.casefold()
    if lowered == ".env" or lowered.startswith(".env.") or lowered == ".envrc":
        return True
    if lowered in _EXCLUDED_CACHE_DIRECTORY_NAMES or lowered.endswith(".cache"):
        return True
    return bool(_cache_name_tokens(name) & _SENSITIVE_DIRECTORY_TOKENS)


def _cache_excludes_file(name: str) -> bool:
    lowered = name.casefold()
    if lowered == ".env" or lowered.startswith(".env.") or lowered == ".envrc":
        return True
    if lowered in {".git", "__pycache__", "cache"}:
        return True
    stem, suffix = os.path.splitext(lowered)
    if lowered in _SENSITIVE_EXACT_FILENAMES or suffix in _KEY_MATERIAL_SUFFIXES:
        return True
    if lowered.endswith((".pyc", ".pyo")):
        return True
    return bool(_cache_name_tokens(stem) & _SENSITIVE_FILE_TOKENS)


def _cache_is_link_stat(info: os.stat_result) -> bool:
    file_attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(file_attributes & reparse_flag)


class _MetadataBudget:
    __slots__ = ("directories", "entrypoints", "files")

    def __init__(self) -> None:
        self.directories = 0
        self.entrypoints = 0
        self.files = 0


class _MetadataScanLimitExceeded(RuntimeError):
    """The metadata walk reached a Scanner resource limit."""


def _cache_relative_path(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root).replace(os.sep, "/")
    except ValueError:
        return os.path.basename(path)


def _read_metadata_entries(
    directory: str,
    budget: _MetadataBudget,
) -> list[tuple[str, str, os.stat_result]]:
    """List directory entries with the Scanner's limits and ordering."""
    if budget.directories >= _MAX_CACHE_SCAN_DIRECTORIES:
        raise _MetadataScanLimitExceeded
    budget.directories += 1
    entries: list[tuple[str, str, os.stat_result]] = []
    with os.scandir(directory) as iterator:
        for entry in iterator:
            if len(entries) >= _MAX_CACHE_DIRECTORY_ENTRIES:
                raise _MetadataScanLimitExceeded
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append((entry.name, entry.path, info))
    entries.sort(key=lambda item: (item[0].casefold(), item[0]))
    return entries


def _discover_skill_roots(
    root: Path,
    max_depth: int | None,
    budget: _MetadataBudget,
) -> list[str]:
    """Find Skill roots with the Scanner's ``max_depth`` semantics."""
    root_path = str(root)
    pending: list[tuple[str, int]] = [(root_path, 0)]
    roots: set[str] = set()
    while pending:
        directory, depth = pending.pop()
        try:
            entries = _read_metadata_entries(directory, budget)
        except OSError:
            if directory == root_path:
                raise
            continue
        subdirectories: list[str] = []
        for name, path, info in entries:
            if _cache_is_link_stat(info):
                continue
            if stat.S_ISREG(info.st_mode) and name == "SKILL.md":
                budget.entrypoints += 1
                if budget.entrypoints > _MAX_CACHE_ENTRYPOINTS:
                    raise _MetadataScanLimitExceeded
                roots.add(os.path.dirname(path))
                continue
            if stat.S_ISDIR(info.st_mode) and not _cache_excludes_directory(
                name
            ):
                if max_depth is None or depth < max_depth:
                    subdirectories.append(path)
        pending.extend((child, depth + 1) for child in reversed(subdirectories))
    return sorted(
        roots,
        key=lambda path: _cache_relative_path(path, root_path).casefold(),
    )


def _collect_skill_asset_files(
    skill_root: str,
    budget: _MetadataBudget,
) -> tuple[list[tuple[str, os.stat_result]], bool]:
    """Collect Skill asset metadata. ``max_depth`` does not limit assets."""
    pending: list[tuple[str, str]] = [(skill_root, "")]
    files: list[tuple[str, os.stat_result]] = []
    limited = False
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = _read_metadata_entries(directory, budget)
        except OSError:
            continue
        subdirectories: list[tuple[str, str]] = []
        for name, path, info in entries:
            if _cache_is_link_stat(info):
                continue
            relative_path = f"{prefix}/{name}" if prefix else name
            if stat.S_ISDIR(info.st_mode):
                if not _cache_excludes_directory(name):
                    subdirectories.append((path, relative_path))
                continue
            if not stat.S_ISREG(info.st_mode) or _cache_excludes_file(name):
                continue
            budget.files += 1
            if budget.files > _MAX_CACHE_ASSET_FILES:
                raise _MetadataScanLimitExceeded
            files.append((relative_path, info))
            if len(files) > _MAX_CACHE_ASSET_FILES_PER_SKILL:
                limited = True
                files.sort(key=lambda item: (item[0].casefold(), item[0]))
                return files, limited
        pending.extend(reversed(subdirectories))
    files.sort(key=lambda item: (item[0].casefold(), item[0]))
    return files, limited


def _skills_metadata_digest(root: Path, *, max_depth: int | None) -> str:
    """Hash Skill asset metadata without reading file contents.

    Entry discovery follows ``max_depth``. Asset collection always walks each
    discovered Skill recursively, matching ``SkillFolderScanner``.
    """
    digest = hashlib.sha256()
    digest.update(b"skills-metadata-v1\0")
    try:
        root_stat = root.lstat()
    except OSError:
        digest.update(b"missing")
        return digest.hexdigest()
    if _cache_is_link_stat(root_stat) or not stat.S_ISDIR(
        root_stat.st_mode
    ):
        digest.update(b"invalid")
        return digest.hexdigest()

    budget = _MetadataBudget()
    root_path = str(root)
    try:
        skill_roots = _discover_skill_roots(root, max_depth, budget)
        for skill_root in skill_roots:
            digest.update(b"skill\0")
            digest.update(
                _cache_relative_path(skill_root, root_path).encode(
                    "utf-8", "surrogateescape"
                )
            )
            digest.update(b"\0")
            files, limited = _collect_skill_asset_files(skill_root, budget)
            if limited:
                digest.update(b"limited\0")
            for relative_path, info in files:
                digest.update(
                    relative_path.encode("utf-8", "surrogateescape")
                )
                digest.update(b"\0")
                digest.update(
                    (
                        f"{info.st_mode}:{info.st_size}:{info.st_mtime_ns}:"
                        f"{info.st_ctime_ns}:{info.st_ino}:{info.st_dev}"
                    ).encode("ascii")
                )
                digest.update(b"\0")
    except (_MetadataScanLimitExceeded, OSError):
        # Force a cache miss so the real Scanner can report its own limit error.
        return f"unavailable:{os.urandom(8).hex()}"
    return digest.hexdigest()


def _build_runs_digest(graph_dir: Path) -> str:
    """Hash build run directories and checkpoints used by resume status."""
    digest = hashlib.sha256()
    runs_root = graph_dir / BUILD_RUNS_DIRNAME
    if not runs_root.is_dir():
        digest.update(b"missing")
        return digest.hexdigest()
    try:
        with os.scandir(runs_root) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name)
    except OSError:
        digest.update(b"unreadable")
        return digest.hexdigest()
    for entry in ordered:
        run_dir = Path(entry.path)
        digest.update(entry.name.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(repr(_path_stamp(run_dir)).encode("ascii"))
        digest.update(b"\0")
        digest.update(
            repr(_path_stamp(run_dir / "checkpoint.json")).encode("ascii")
        )
        digest.update(b"\0")
    return digest.hexdigest()


# Bound staleness when metadata cannot prove a change. On Unix, ctime detects
# content edits that restore mtime. On Windows, ctime is creation time.
_GRAPH_STATUS_CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class _GraphStatusCacheKey:
    skills_root: str
    graph_dir: str
    max_depth: int | None
    config_digest: str
    skills_digest: str
    graph_dir_stamp: _FileStamp | None
    pointer_stamp: _FileStamp | None
    artifact_stamps: tuple[_FileStamp | None, ...]
    build_runs_digest: str


def _graph_status_cache_key(
    config: Any,
    skills_root: Path,
    graph_dir: Path,
) -> _GraphStatusCacheKey:
    """Build a cheap identity for everything graph status reads."""
    fingerprint_config = getattr(config, "fingerprint", None)
    scan_config = getattr(fingerprint_config, "scan", None)
    max_depth = getattr(scan_config, "max_depth", None)
    try:
        artifact_dir = resolve_graph_artifact_dir(graph_dir)
    except (OSError, ValueError):
        artifact_dir = None

    artifact_stamps: tuple[_FileStamp | None, ...] = ()
    if artifact_dir is not None:
        artifact_stamps = (
            _path_stamp(artifact_dir / "graph.json"),
            _path_stamp(artifact_dir / GRAPH_STATE_FILENAME),
            _path_stamp(artifact_dir / FINGERPRINT_ARTIFACT_FILENAME),
        )

    return _GraphStatusCacheKey(
        skills_root=str(skills_root),
        graph_dir=str(graph_dir),
        max_depth=max_depth,
        config_digest=hashlib.sha256(
            repr(config).encode("utf-8", "surrogateescape")
        ).hexdigest(),
        skills_digest=_skills_metadata_digest(skills_root, max_depth=max_depth),
        graph_dir_stamp=_path_stamp(graph_dir),
        pointer_stamp=_path_stamp(graph_dir / CURRENT_POINTER_FILENAME),
        artifact_stamps=artifact_stamps,
        build_runs_digest=_build_runs_digest(graph_dir),
    )


_BUILD_LOG_READ_CHUNK_BYTES = 64 * 1024


def _tail_lines(path: Path, limit: int) -> list[str]:
    """Read at most the last ``limit`` lines without loading the whole file."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        chunks: list[bytes] = []
        newline_count = 0
        # Keep one extra newline so the first decoded line can be dropped when
        # the chunk started in the middle of a line.
        while position > 0 and newline_count <= limit:
            read_size = min(_BUILD_LOG_READ_CHUNK_BYTES, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
        data = b"".join(reversed(chunks))
        if position > 0:
            first_newline = data.find(b"\n")
            if first_newline >= 0:
                data = data[first_newline + 1:]
        return data.decode("utf-8").splitlines()


def _compose_graph_status_payload(
    graph_payload: dict[str, Any],
    graph_dir: Path,
) -> dict[str, Any]:
    """Merge cached graph facts with live build progress."""
    payload = deepcopy(graph_payload)
    payload.update(_build_log_payload(graph_dir))
    _prefer_build_failure_detail(payload)
    return payload


def _build_log_payload(graph_dir: Path | str, *, limit: int = 80) -> dict[str, Any]:
    resolved_graph_dir = Path(graph_dir)
    entries = _read_build_log(resolved_graph_dir, limit=limit)
    token_usage = _build_token_usage_payload(resolved_graph_dir, entries)
    build_progress = _build_progress(entries)
    if token_usage:
        build_progress["llm_token_usage"] = token_usage
    payload = {
        "build_log": entries,
        "build_progress": build_progress,
        "llm_token_usage": token_usage,
    }
    build_error = str(build_progress.get("detail") or "").strip()
    if build_progress.get("status") == "error" and build_error:
        payload["build_error"] = build_error
    return payload


def _read_build_log(graph_dir: Path, *, limit: int = 80) -> list[dict[str, Any]]:
    log_path = graph_dir / "build_log.jsonl"
    if not log_path.is_file():
        return []
    line_limit = max(1, limit)
    try:
        lines = _tail_lines(log_path, line_limit)
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines[-line_limit:]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("stage"):
            entries.append(_normalize_build_log_entry(payload))
    return entries


def _repair_interrupted_build_log(graph_dir: Path) -> bool:
    """Close an orphaned running log after its owning process has disappeared.

    Callers must hold the extension build guard and first rule out a live task.
    """
    if _build_progress(_read_build_log(graph_dir)).get("status") != "running":
        return False
    _BuildProcessLogger(graph_dir / "build_log.jsonl").record(
        "update.cancelled",
        reason="process_interrupted",
    )
    return True


def _normalize_build_log_entry(payload: dict[str, Any]) -> dict[str, Any]:
    stage = str(payload.get("stage") or "")
    entry = dict(payload)
    entry["stage"] = stage
    entry["label"] = _BUILD_STAGE_LABELS.get(stage, stage)
    _clamp_build_log_count(entry)
    return entry


def _clamp_build_log_count(entry: dict[str, Any]) -> None:
    if "current" not in entry or "total" not in entry:
        return
    try:
        total = int(entry.get("total") or 0)
        current = int(entry.get("current") or 0)
    except (TypeError, ValueError):
        return
    if total <= 0:
        return
    entry["total"] = total
    entry["current"] = max(0, min(current, total))


def _build_progress(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        return {
            "stage": "idle",
            "label": "暂无构建日志",
            "percent": 0,
            "status": "idle",
        }
    latest = _latest_effective_build_log_entry(entries)
    stage = str(latest.get("stage") or "")
    status = "running"
    if stage == "update.done":
        status = "error" if latest.get("success") is False else "success"
    elif stage == "update.failed":
        status = "error"
    elif stage == "update.cancelled":
        status = "cancelled"
    current = latest.get("current")
    total = latest.get("total")
    if stage == "graph.resolve.progress":
        candidate_counts = _graph_resolve_candidate_counts(latest)
        if candidate_counts is not None:
            current, total = candidate_counts
    progress = {
        "stage": stage,
        "label": str(latest.get("label") or _BUILD_STAGE_LABELS.get(stage, stage)),
        "percent": _build_stage_percent(stage, latest, entries=entries),
        "status": status,
        "current": current,
        "total": total,
        "ts": latest.get("ts"),
    }
    if status == "error":
        detail = _build_failure_detail(latest)
        if detail:
            progress["detail"] = detail
        error = str(latest.get("error") or "").strip()
        if error:
            progress["error"] = error
    return progress


def _build_failure_detail(entry: dict[str, Any]) -> str:
    detail = str(entry.get("detail") or "").strip()
    if detail:
        return detail
    error = str(entry.get("error") or "").strip()
    if error:
        return f"Symphony 总谱构建失败: {error}"
    return _BUILD_STAGE_LABELS["update.failed"]


def _prefer_build_failure_detail(payload: dict[str, Any]) -> None:
    progress = payload.get("build_progress")
    if not isinstance(progress, dict) or progress.get("status") != "error":
        return
    detail = str(payload.get("build_error") or progress.get("detail") or "").strip()
    if detail:
        payload["detail"] = detail


def _latest_effective_build_log_entry(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        return {}
    for entry in reversed(entries):
        if str(entry.get("stage") or "") in {
            "update.done",
            "update.failed",
            "update.cancelled",
        }:
            return entry
    return entries[-1]


def _build_token_usage_payload(
    graph_dir: Path, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    status = _build_progress(entries).get("status")
    if status == "running":
        current = _current_token_usage_summary()
        if _has_token_usage(current):
            return current
        return {}

    for usage in (
        _read_manifest_token_usage(graph_dir),
        _read_json_token_usage(
            resolve_graph_artifact_dir(graph_dir) / "llm_token_usage.json"
        ),
        _read_json_token_usage(graph_dir / "llm_token_usage.json"),
    ):
        if _has_token_usage(usage):
            return usage

    return {}


def _current_token_usage_summary() -> dict[str, Any]:
    try:
        from jiuwenswarm.symphony.llm import get_llm_token_usage_summary

        usage = get_llm_token_usage_summary()
    except Exception:  # noqa: BLE001
        return {}
    return usage if isinstance(usage, dict) else {}


def _read_manifest_token_usage(graph_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            (resolve_graph_artifact_dir(graph_dir) / "graph_manifest.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    llm = payload.get("llm")
    if not isinstance(llm, dict):
        return {}
    usage = llm.get("token_usage")
    return usage if isinstance(usage, dict) else {}


def _read_json_token_usage(path: Path) -> dict[str, Any]:
    try:
        usage = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return usage if isinstance(usage, dict) else {}


def _has_token_usage(usage: dict[str, Any]) -> bool:
    total = usage.get("total")
    if not isinstance(total, dict):
        return False
    try:
        return int(total.get("total_tokens") or 0) > 0
    except (TypeError, ValueError):
        return False


def _build_stage_percent(
    stage: str,
    entry: dict[str, Any],
    *,
    entries: list[dict[str, Any]] | None = None,
) -> int:
    if stage == "graph.resolve.progress":
        return _graph_resolve_percent(entries or ())
    if stage == "fingerprint.done":
        return 48
    if stage.startswith("fingerprint."):
        return _progress_between(entry, start=24, end=48)
    return int(_BUILD_STAGE_PROGRESS.get(stage, 0))


def _graph_resolve_percent(
    entries: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> int:
    """Advance relation progress only after a matcher batch has completed."""

    candidate_progress = [
        counts
        for candidate in entries
        if candidate.get("stage") == "graph.resolve.progress"
        if (counts := _graph_resolve_candidate_counts(candidate)) is not None
    ]
    if candidate_progress:
        current, total = max(candidate_progress, key=lambda item: item[0] / item[1])
        return _progress_between(
            {"current": current, "total": total},
            start=72,
            end=84,
        )

    completed: list[tuple[int, int]] = []
    for candidate in entries:
        if candidate.get("stage") != "graph.resolve.progress":
            continue
        if candidate.get("matcher_event") not in {"batch_done", "matching_done"}:
            continue
        try:
            current = int(candidate.get("current") or 0)
            total = int(candidate.get("total") or 0)
        except (TypeError, ValueError):
            continue
        if total > 0:
            completed.append((max(0, min(current, total)), total))

    if not completed:
        return 72
    current, total = max(completed, key=lambda item: item[0] / item[1])
    return _progress_between(
        {"current": current, "total": total},
        start=72,
        end=84,
    )


def _graph_resolve_candidate_counts(entry: dict[str, Any]) -> tuple[int, int] | None:
    try:
        current = int(entry.get("completed_candidate_count") or 0)
        total = int(entry.get("total_candidate_count") or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return max(0, min(current, total)), total


def _progress_between(entry: dict[str, Any], *, start: int, end: int) -> int:
    try:
        current = int(entry.get("current") or 0)
        total = int(entry.get("total") or 0)
    except (TypeError, ValueError):
        return start
    if total <= 0:
        return start
    ratio = max(0.0, min(1.0, current / total))
    return int(round(start + (end - start) * ratio))


class _OrderedProgressDispatcher:
    """Serialize progress delivery and drain it before the tool returns."""

    def __init__(self, callback: ProgressCallback | None) -> None:
        self.callback = callback
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.worker: asyncio.Task[None] | None = None
        self._close_lock = asyncio.Lock()
        self._state_lock = Lock()
        self._accepting = False
        self._threadsafe_puts: set[ConcurrentFuture[None]] = set()

    def start(self) -> None:
        with self._state_lock:
            if self.callback is None or self.worker is not None:
                return
            self.loop = asyncio.get_running_loop()
            self.worker = self.loop.create_task(
                self._run(),
                name="symphony-build-progress",
            )
            self._accepting = True

    def enqueue(self, event: dict[str, Any]) -> None:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        with self._state_lock:
            loop = self.loop
            if not self._accepting or self.worker is None or loop is None:
                return
            if running_loop is loop:
                self.queue.put_nowait(event)
                return
            put = self.queue.put(event)
            try:
                future = asyncio.run_coroutine_threadsafe(put, loop)
            except RuntimeError:
                put.close()
                logger.warning("Symphony progress event arrived after loop shutdown")
                return
            self._threadsafe_puts.add(future)
        future.add_done_callback(self._forget_threadsafe_put)

    async def close(self) -> None:
        async with self._close_lock:
            try:
                await self._close_locked()
            except BaseException:
                await self._abort_locked()
                raise

    async def abort(self) -> None:
        """Stop progress delivery immediately and reclaim all owned work."""

        async with self._close_lock:
            await self._abort_locked()

    async def shutdown_now(self) -> None:
        """Alias for callers that prefer explicit shutdown terminology."""

        await self.abort()

    async def _close_locked(self) -> None:
        with self._state_lock:
            worker = self.worker
            self._accepting = False
            pending_puts = tuple(self._threadsafe_puts)
        if worker is None:
            return
        for future in pending_puts:
            try:
                await asyncio.wrap_future(future)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                logger.warning("Symphony threaded progress delivery cancelled")
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Symphony threaded progress delivery failed",
                    exc_info=True,
                )
        await self.queue.join()
        self.queue.put_nowait(None)
        await worker
        with self._state_lock:
            self.worker = None
            self._threadsafe_puts.clear()

    async def _abort_locked(self) -> None:
        with self._state_lock:
            worker = self.worker
            self._accepting = False
            pending_puts = tuple(self._threadsafe_puts)
        for future in pending_puts:
            future.cancel()
        if worker is not None and not worker.done():
            worker.cancel("symphony.progress.abort")
        for future in pending_puts:
            try:
                await asyncio.wrap_future(future)
            except BaseException:
                pass
        if worker is not None:
            try:
                await worker
            except BaseException:
                pass
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self.queue.task_done()
        with self._state_lock:
            self.worker = None
            self._threadsafe_puts.clear()

    def _forget_threadsafe_put(self, future: ConcurrentFuture[None]) -> None:
        with self._state_lock:
            self._threadsafe_puts.discard(future)

    async def _run(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                if event is None:
                    return
                result = self.callback(event) if self.callback is not None else None
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                logger.warning("Symphony progress callback cancelled")
            except Exception:  # noqa: BLE001
                logger.warning("Symphony progress callback failed", exc_info=True)
            finally:
                self.queue.task_done()


class _BuildProcessLogger:
    def __init__(
        self,
        path: Path,
        *,
        progress: _OrderedProgressDispatcher | None = None,
    ) -> None:
        self.path = path
        self.progress = progress

    def reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def record(self, stage: str, **details: Any) -> None:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            **details,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        logger.info("[SymphonyBuild] %s: %s", stage, _compact_details(details))
        if self.progress is not None:
            self.progress.enqueue({"event": stage, **details})


def _compact_details(details: dict[str, Any]) -> str:
    if not details:
        return "{}"
    rendered = json.dumps(details, ensure_ascii=False, default=str)
    return rendered if len(rendered) <= 500 else rendered[:497] + "..."
