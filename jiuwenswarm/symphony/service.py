"""Process-local JiuwenSwarm integration for public Symphony orchestration."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
import inspect
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from openjiuwen.symphony import SymphonyRuntime

from jiuwenswarm.common.config import get_config
from jiuwenswarm.server.runtime.skill import load_execution_disabled_skills
from jiuwenswarm.symphony.adapter import (
    candidate_ids_from_skill_ids,
    graph_config_from_swarm,
    llm_config_signature,
    model_from_config,
    model_response_observer_from_config,
    orchestration_config_from_swarm,
    swarm_plan_from_public,
)
from jiuwenswarm.symphony.llm import LLMConfig
from jiuwenswarm.symphony.config import load_symphony_config
from jiuwenswarm.symphony.build import build_graph as service_build_graph
from jiuwenswarm.symphony.build import graph_status
from jiuwenswarm.symphony.evolution.service import load_dynamic_overlay
from jiuwenswarm.symphony.graph_storage import resolve_graph_artifact_dir

logger = logging.getLogger(__name__)


ProgressCallback = Callable[[dict[str, Any]], Any]


class SwarmSymphonyService:
    """Own the process-local Symphony runtime used by all Agent tools."""

    def __init__(self) -> None:
        self._build_guard = asyncio.Lock()
        self._active_build_task: asyncio.Task | None = None
        self._runtime: SymphonyRuntime | None = None
        self._runtime_key: tuple[Any, ...] | None = None

    async def graph_status(
        self,
    ) -> dict[str, Any]:
        config = load_symphony_config()
        skills_root = config.paths.skills_root
        graph_dir = config.paths.graph_dir
        await self._repair_interrupted_build_state(graph_dir)

        def status() -> dict[str, Any]:
            payload = graph_status(
                skills_root,
                graph_dir,
                # Freshness follows the model identity captured by the published
                # graph. The current default model is an online planning concern
                # and must not make that graph stale merely because it changed.
                llm_config=None,
                symphony_config=config,
            ).to_dict()
            payload.update(_build_log_payload(graph_dir))
            return payload

        return await asyncio.to_thread(status)

    async def refresh_graph(
        self,
        *,
        force: bool = False,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        return await self._build_graph(force=force, progress=progress)

    async def start_refresh_graph(
        self,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Start or reuse a process-local background graph build."""

        config = load_symphony_config()
        graph_dir = config.paths.graph_dir
        async with self._build_guard:
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
                self._build_graph(force=force, progress=None, prestarted=True),
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
                dynamic_overlay=(
                    load_dynamic_overlay(graph_dir)
                    if config.evolution.enabled
                    else None
                ),
            )
        payload.update(_build_log_payload(graph_dir))
        return payload

    async def plan(
        self,
        query: str,
        mode: str | None = None,
        candidate_skill_ids: list[str] | None = None,
        *,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            return {"success": False, "detail": "query is required"}
        candidate_ids = candidate_ids_from_skill_ids(candidate_skill_ids)
        language = _resolve_orchestration_language(
            get_config().get("preferred_language", "zh")
        )
        config = load_symphony_config()
        graph_dir = config.paths.graph_dir
        requested_mode = str(mode or config.orchestration.mode).strip()
        try:
            orchestration_config_from_swarm(config, mode=requested_mode)
        except ValueError as exc:
            return {
                "success": False,
                "graph_dir": str(graph_dir),
                "query": query,
                "mode": config.orchestration.mode,
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
            graph_build = await self.refresh_graph(progress=progress)
            graph_build["rebuilt"] = True
            if not graph_build.get("success"):
                return {
                    "success": False,
                    "detail": "Skill Score build failed before planning",
                    "graph_status": status,
                    "graph_build": graph_build,
                }
        else:
            graph_build = {
                "success": True,
                "rebuilt": False,
                "reason": "not_required",
            }
        try:
            public_payload = await self._runtime_for(config).orchestration.plan(
                query,
                candidate_ids=candidate_ids,
                language=language,
                progress=progress,
                disabled_capability_ids=load_execution_disabled_skills(),
                dynamic_overlay=(
                    load_dynamic_overlay(graph_dir)
                    if config.evolution.enabled
                    else None
                ),
                mode=requested_mode,
            )
            payload = swarm_plan_from_public(public_payload.to_dict())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Symphony planning failed")
            payload = {"success": False, "detail": str(exc)}
        if payload.get("success") is False:
            return {
                "success": False,
                "graph_dir": str(graph_dir),
                "query": query,
                "mode": requested_mode,
                "language": language,
                "graph_status": status,
                "graph_build": graph_build,
                **payload,
            }
        presentation = _build_presentation(payload, language=language)
        return {
            "success": True,
            "graph_dir": str(graph_dir),
            "query": query,
            "mode": requested_mode,
            "language": language,
            "content": presentation["markdown"],
            "direct_display": True,
            "graph_status": status,
            "graph_build": graph_build,
            "result": payload,
        }

    async def _build_graph(
        self,
        *,
        force: bool,
        progress: ProgressCallback | None,
        prestarted: bool = False,
    ) -> dict[str, Any]:
        config = load_symphony_config()
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
                    "detail": "已有技能总谱构建正在运行，请等待完成或先取消当前构建。",
                }
                payload.update(_build_log_payload(graph_dir))
                return payload
            self._active_build_task = current_task
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
                result = (
                    await service_build_graph(
                        skills_root,
                        graph_dir,
                        LLMConfig.from_default_model(),
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
                build_logger.record("update.failed", error=str(exc))
                payload = {
                    "success": False,
                    "graph_dir": str(graph_dir),
                    "detail": f"Symphony 总谱构建失败: {exc}",
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
            try:
                if abort_progress:
                    await progress_dispatcher.abort()
                else:
                    await progress_dispatcher.close()
            finally:
                await self._clear_active_build_task(current_task)

    def _runtime_for(self, config) -> SymphonyRuntime:
        llm_config = LLMConfig.from_default_model()
        llm_signature = llm_config_signature(llm_config)
        key = (
            str(config.paths.graph_dir),
            config.orchestration.mode,
            config.orchestration.top_k,
            config.orchestration.max_depth,
            config.orchestration.min_edge_confidence,
            config.evolution.enabled,
            llm_signature,
        )
        if self._runtime is None or self._runtime_key != key:
            self._runtime = SymphonyRuntime(
                graph_artifact_root=config.paths.graph_dir,
                capability_provider=(),
                model=model_from_config(llm_config),
                model_response_observer=model_response_observer_from_config(llm_config),
                orchestration_config=orchestration_config_from_swarm(config),
                graph_config=graph_config_from_swarm(config),
            )
            self._runtime_key = key
        return self._runtime

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
            return await asyncio.to_thread(_repair_interrupted_build_log, graph_dir)


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
    if not bool(status.get("exists", False)) or bool(status.get("stale", False)):
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
    return {
        "success": True,
        "graph_dir": str(graph_dir),
        "graph_manifest": dict(artifact.get("config") or {}),
        "orchestration_min_edge_confidence": min_edge_confidence,
        "skills": skills,
        "graph": _graph_with_runtime_weights(graph, dynamic_overlay),
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
    return re.sub(r"[^a-z0-9]+", "-", _capability_id(value).strip().lower()).strip("-")


def _resolve_orchestration_language(value: Any = None) -> str:
    language = str(value or "").strip().lower()
    if language == "zh":
        language = "cn"
    return language if language in {"cn", "en"} else "cn"


def _select_primary_plan(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("recommended_plans", "plans"):
        plans = payload.get(key)
        if not isinstance(plans, list):
            continue
        for plan in plans:
            if isinstance(plan, dict):
                return plan
    return {}


def _default_plan_title(language: str) -> str:
    if _resolve_orchestration_language(language) == "en":
        return "Symphony plan"
    return "Symphony 编排计划"


_BUILD_STAGE_LABELS = {
    "update.start": "开始构建技能总谱",
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


def _build_log_payload(graph_dir: Path | str, *, limit: int = 80) -> dict[str, Any]:
    resolved_graph_dir = Path(graph_dir)
    entries = _read_build_log(resolved_graph_dir, limit=limit)
    token_usage = _build_token_usage_payload(resolved_graph_dir, entries)
    build_progress = _build_progress(entries)
    if token_usage:
        build_progress["llm_token_usage"] = token_usage
    return {
        "build_log": entries,
        "build_progress": build_progress,
        "llm_token_usage": token_usage,
    }


def _read_build_log(graph_dir: Path, *, limit: int = 80) -> list[dict[str, Any]]:
    log_path = graph_dir / "build_log.jsonl"
    if not log_path.is_file():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    line_limit = max(1, limit)
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
    return {
        "stage": stage,
        "label": str(latest.get("label") or _BUILD_STAGE_LABELS.get(stage, stage)),
        "percent": _build_stage_percent(stage, latest, entries=entries),
        "status": status,
        "current": current,
        "total": total,
        "ts": latest.get("ts"),
    }


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


def _build_presentation(
    payload: dict[str, Any],
    *,
    language: str = "cn",
) -> dict[str, str]:
    plan = _select_primary_plan(payload)
    title = str(plan.get("title") or _default_plan_title(language)).strip()
    mermaid = _plan_to_mermaid(plan, payload.get("execution_graph") or {})
    lines = [
        f"## {title}",
        "",
        "```mermaid",
        mermaid,
        "```",
    ]
    reason = str(plan.get("reason") or payload.get("reason") or "").strip()
    if reason:
        lines.extend(["", reason])
    steps = plan.get("steps") if isinstance(plan, dict) else []
    if isinstance(steps, list) and steps:
        confirmation = (
            "Would you like to proceed with the orchestration plan above?"
            if language == "en"
            else "是否按照上述编排结果执行？"
        )
        lines.extend(["", confirmation])
    return {"markdown": "\n".join(lines), "mermaid": mermaid}


def _plan_to_mermaid(plan: dict[str, Any], graph: dict[str, Any]) -> str:
    steps = plan.get("steps") if isinstance(plan, dict) else []
    edges = graph.get("edges") if isinstance(graph, dict) else []
    labels = {
        str(step.get("skill_id") or ""): str(
            step.get("skill_name") or step.get("name") or step.get("skill_id") or ""
        )
        for step in steps or []
        if isinstance(step, dict)
    }
    node_ids = [skill_id for skill_id in labels if skill_id]
    for edge in edges or []:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source and source not in node_ids:
            node_ids.append(source)
        if target and target not in node_ids:
            node_ids.append(target)
    if not node_ids:
        return 'flowchart LR\n  none["No Symphony plan"]'

    node_keys = {
        node_id: f"N{index}" for index, node_id in enumerate(node_ids, start=1)
    }
    lines = ["flowchart LR"]
    for node_id in node_ids:
        lines.append(
            f'  {node_keys[node_id]}["{_mermaid_escape(labels.get(node_id) or node_id)}"]'
        )
    for edge in edges or []:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in node_keys and target in node_keys:
            lines.append(f"  {node_keys[source]} --> {node_keys[target]}")
    return "\n".join(lines)


def _mermaid_escape(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')[:80]
