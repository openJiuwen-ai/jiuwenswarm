"""Symphony extension RPC handlers."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.common.config import get_config
from jiuwenswarm.extensions.sdk import BaseExtension
from jiuwenswarm.server.runtime.skill import load_execution_disabled_skills
from jiuwenswarm.symphony.llm import LLMConfig
from jiuwenswarm.symphony.config import load_symphony_config, symphony_config_from_dict
from jiuwenswarm.symphony.build import build_score as service_build_score
from jiuwenswarm.symphony.build import score_status
from jiuwenswarm.symphony.evolution.models import edge_key, skill_id
from jiuwenswarm.symphony.evolution.service import load_dynamic_overlay
from jiuwenswarm.symphony.orchestration import load_score_artifacts
from jiuwenswarm.symphony.orchestration.artifacts import filter_disabled_score_artifacts
from jiuwenswarm.symphony.orchestration.execution_graph import select_primary_plan
from jiuwenswarm.symphony.orchestration.language import (
    default_plan_title,
    resolve_orchestration_language,
)
from jiuwenswarm.symphony.orchestration.service import plan_from_score
from jiuwenswarm.symphony.score_storage import resolve_score_artifact_dir

SYMPHONY_BUILD_SCORE = "symphony.build_score"
SYMPHONY_PAUSE_BUILD = "symphony.pause_build"
SYMPHONY_SCORE_STATUS = "symphony.score_status"
SYMPHONY_GRAPH = "symphony.graph"
SYMPHONY_PLAN = "symphony.plan"

logger = logging.getLogger(__name__)


def _candidate_skill_ids_from_params(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        current_skill_id = str(item or "").strip()
        if not current_skill_id or current_skill_id in seen:
            continue
        seen.add(current_skill_id)
        output.append(current_skill_id)
    return output


class SymphonyExtension(BaseExtension):
    """Register explicit Symphony RPC methods."""

    def __init__(self) -> None:
        self._registry = None
        self._build_guard = asyncio.Lock()
        self._active_build_task: asyncio.Task | None = None

    async def initialize(self, config) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def register(self, registry) -> None:
        self._registry = registry
        registry.register_rpc_handler(SYMPHONY_BUILD_SCORE, self.build_score)
        registry.register_rpc_handler(SYMPHONY_PAUSE_BUILD, self.pause_build)
        registry.register_rpc_handler(SYMPHONY_SCORE_STATUS, self.score_status)
        registry.register_rpc_handler(SYMPHONY_GRAPH, self.graph)
        registry.register_rpc_handler(SYMPHONY_PLAN, self.plan)

    async def score_status(
        self,
        params: dict[str, Any] | None = None,
        request: Any = None,
    ) -> dict[str, Any]:
        del params, request
        config = load_symphony_config()
        skills_root = config.paths.skills_root
        score_dir = config.paths.score_dir
        await self._repair_interrupted_build_state(score_dir)

        def status() -> dict[str, Any]:
            payload = score_status(
                skills_root,
                score_dir,
                symphony_config=config,
            ).to_dict()
            payload.update(_build_log_payload(score_dir))
            return payload

        return await asyncio.to_thread(status)

    async def build_score(
        self,
        params: dict[str, Any] | None = None,
        request: Any = None,
    ) -> dict[str, Any]:
        params = params or {}
        return await self._build_score(
            params, request, force=_param_bool(params.get("force"))
        )

    async def pause_build(
        self,
        params: dict[str, Any] | None = None,
        request: Any = None,
    ) -> dict[str, Any]:
        del params, request
        config = load_symphony_config()
        score_dir = config.paths.score_dir
        build_logger = _BuildProcessLogger(score_dir / "build_log.jsonl")
        async with self._build_guard:
            task = self._active_build_task
            if task is None or task.done():
                if task is not None:
                    self._active_build_task = None
                _repair_interrupted_build_log(score_dir)
                build_log_payload = _build_log_payload(score_dir)
                is_paused = (
                    build_log_payload["build_progress"].get("status") == "paused"
                )
                payload = {
                    "success": True,
                    "score_dir": str(score_dir),
                    "paused": is_paused,
                    "detail": (
                        "技能总谱构建已暂停，可再次执行增量构建继续。"
                        if is_paused
                        else "当前没有正在运行的技能总谱构建。"
                    ),
                }
                payload.update(build_log_payload)
                return payload
            build_logger.record("update.pause_requested")
            task.cancel("symphony.pause_build")
            build_logger.record("update.paused")
        payload = {
            "success": True,
            "score_dir": str(score_dir),
            "paused": True,
            "detail": "已请求暂停技能总谱构建，已完成的缓存和 checkpoint 会保留。",
        }
        payload.update(_build_log_payload(score_dir))
        return payload

    async def graph(
        self, params: dict[str, Any] | None = None, request: Any = None
    ) -> dict[str, Any]:
        del params, request
        config = load_symphony_config()
        score_dir = config.paths.score_dir
        orchestration_min_edge_confidence = config.orchestration.min_edge_confidence
        await self._repair_interrupted_build_state(score_dir)

        def load() -> dict[str, Any]:
            try:
                artifacts = filter_disabled_score_artifacts(
                    load_score_artifacts(score_dir),
                    load_execution_disabled_skills(),
                )
            except FileNotFoundError as exc:
                payload = _missing_artifacts_payload(score_dir, exc)
                payload["orchestration_min_edge_confidence"] = (
                    orchestration_min_edge_confidence
                )
                payload.update(_build_log_payload(score_dir))
                return payload
            payload = {
                "success": True,
                "score_dir": str(artifacts.score_dir),
                "score_manifest": artifacts.manifest,
                "orchestration_min_edge_confidence": (
                    orchestration_min_edge_confidence
                ),
                "skills": artifacts.skills,
                "graph": _graph_with_runtime_weights(
                    artifacts.graph,
                    load_dynamic_overlay(artifacts.score_dir)
                    if config.evolution.enabled
                    else None,
                ),
                "score_lookup": artifacts.lookup,
            }
            payload.update(_build_log_payload(score_dir))
            return payload

        return await asyncio.to_thread(load)

    async def plan(
        self, params: dict[str, Any] | None = None, request: Any = None
    ) -> dict[str, Any]:
        params = params or {}
        query = str(params.get("query") or "").strip()
        if not query:
            return {"success": False, "detail": "query is required"}
        candidate_skill_ids = _candidate_skill_ids_from_params(
            params.get("candidate_skill_ids")
        )
        language = resolve_orchestration_language(
            get_config().get("preferred_language", "zh")
        )
        config = load_symphony_config()
        score_dir = config.paths.score_dir
        orchestration_config = config.orchestration
        requested_mode = str(params.get("mode") or "").strip()
        if requested_mode:
            try:
                requested_orchestration_config = symphony_config_from_dict(
                    {"orchestration": {"mode": requested_mode}}
                ).orchestration
            except ValueError as exc:
                return {
                    "success": False,
                    "score_dir": str(score_dir),
                    "query": query,
                    "mode": orchestration_config.mode,
                    "detail": str(exc),
                }
            orchestration_config = replace(
                orchestration_config,
                mode=requested_orchestration_config.mode,
            )

        status = await self.score_status(request=request)
        if not status.get("success"):
            return {
                "success": False,
                "detail": "symphony.score_status failed before planning",
                "score_status": status,
            }
        if _score_needs_build(status):
            score_build = await self.build_score(request=request)
            score_build["rebuilt"] = True
            if not score_build.get("success"):
                return {
                    "success": False,
                    "detail": "symphony.build_score failed before planning",
                    "score_status": status,
                    "score_build": score_build,
                }
        else:
            score_build = {
                "success": True,
                "rebuilt": False,
                "reason": "not_required",
            }
        progress_callback = _beam_progress_callback(request)
        payload = await plan_from_score(
            score_dir,
            query,
            LLMConfig.from_default_model(),
            orchestration_config=orchestration_config,
            dynamic_graph_enabled=config.evolution.enabled,
            candidate_skill_ids=candidate_skill_ids,
            disabled_skill_names=load_execution_disabled_skills(),
            progress_callback=progress_callback,
            language=language,
        )
        if payload.get("success") is False:
            return {
                "success": False,
                "score_dir": str(score_dir),
                "query": query,
                "mode": orchestration_config.mode,
                "language": language,
                "score_status": status,
                "score_build": score_build,
                **payload,
            }
        presentation = _build_presentation(payload, language=language)
        return {
            "success": True,
            "score_dir": str(score_dir),
            "query": query,
            "mode": orchestration_config.mode,
            "language": language,
            "content": presentation["markdown"],
            "direct_display": True,
            "score_status": status,
            "score_build": score_build,
            "result": payload,
        }

    async def _build_score(
        self,
        params: dict[str, Any] | None,
        request: Any,
        *,
        force: bool,
    ) -> dict[str, Any]:
        del request
        del params
        config = load_symphony_config()
        skills_root = config.paths.skills_root
        score_dir = config.paths.score_dir
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
                    "score_dir": str(score_dir),
                    "detail": "已有技能总谱构建正在运行，请等待完成或先暂停当前构建。",
                }
                payload.update(_build_log_payload(score_dir))
                return payload
            self._active_build_task = current_task
        build_logger = _BuildProcessLogger(score_dir / "build_log.jsonl")
        build_logger.reset()
        build_logger.record(
            "update.start",
            skills_root=str(skills_root),
            out_dir=str(score_dir),
            force=force,
        )
        try:
            result = (
                await service_build_score(
                    skills_root,
                    score_dir,
                    LLMConfig.from_default_model(),
                    force=force,
                    symphony_config=config,
                    build_log=build_logger.record,
                )
            ).to_dict()
        except asyncio.CancelledError:
            if _build_progress(_read_build_log(score_dir)).get("status") != "paused":
                build_logger.record("update.paused")
            payload = {
                "success": False,
                "score_dir": str(score_dir),
                "paused": True,
                "detail": "技能总谱构建已暂停，可再次执行增量构建继续。",
            }
            payload.update(_build_log_payload(score_dir))
            await self._clear_active_build_task(current_task)
            return payload
        except Exception as exc:  # noqa: BLE001
            build_logger.record("update.failed", error=str(exc))
            payload = {
                "success": False,
                "score_dir": str(score_dir),
                "detail": f"Symphony 总谱构建失败: {exc}",
            }
            payload.update(_build_log_payload(score_dir))
            await self._clear_active_build_task(current_task)
            return payload
        build_logger.record("update.done", **result)
        result["success"] = True
        result.update(_build_log_payload(score_dir))
        await self._clear_active_build_task(current_task)
        return result

    async def _clear_active_build_task(self, task: asyncio.Task | None) -> None:
        async with self._build_guard:
            if self._active_build_task is task:
                self._active_build_task = None

    async def _repair_interrupted_build_state(self, score_dir: Path) -> bool:
        async with self._build_guard:
            task = self._active_build_task
            if task is not None and not task.done():
                return False
            if task is not None:
                self._active_build_task = None
            return await asyncio.to_thread(_repair_interrupted_build_log, score_dir)


async def register_extensions(registry):
    extension = SymphonyExtension()
    extension.register(registry)
    return [extension]


def _score_needs_build(status: dict[str, Any]) -> bool:
    if not bool(status.get("exists", False)) or bool(status.get("stale", False)):
        return True
    for key in ("added_count", "changed_count", "removed_count"):
        try:
            if int(status.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _missing_artifacts_payload(
    score_dir: Path, exc: FileNotFoundError
) -> dict[str, Any]:
    return {
        "success": False,
        "score_dir": str(score_dir),
        "detail": "技能总谱不存在或不完整，请先构建总谱。",
        "error": str(exc),
    }


def _graph_with_runtime_weights(
    graph: dict[str, Any],
    overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    runtime_edges = overlay.get("edges") if isinstance(overlay, dict) else None
    if not isinstance(runtime_edges, dict) or not runtime_edges:
        return graph
    output_edges = []
    for edge in graph.get("edges", []):
        item = dict(edge)
        current_key = edge_key(
            skill_id(item.get("source")),
            skill_id(item.get("target")),
            str(item.get("type") or "can_feed"),
        )
        stats = runtime_edges.get(current_key)
        if isinstance(stats, dict):
            item["runtime_weight"] = float(stats.get("runtime_weight") or 1.0)
        output_edges.append(item)
    return {**graph, "edges": output_edges}


def _param_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _beam_progress_callback(request: Any):
    async def callback(event: dict[str, Any]) -> None:
        await _emit_beam_progress_event(request, event)

    return callback


async def _emit_beam_progress_event(request: Any, event: dict[str, Any]) -> None:
    if request is None:
        return
    payload = {
        "event_type": "symphony.beam_search.update",
        "beam_search_event": event,
    }
    metadata = getattr(request, "metadata", None)
    if isinstance(metadata, dict):
        metadata_callback = metadata.get("symphony_progress_callback")
        if callable(metadata_callback):
            result = metadata_callback(event)
            if asyncio.iscoroutine(result):
                await result
            return

    for name in ("write_stream", "send_event", "send_stream"):
        writer = getattr(request, name, None)
        if not callable(writer):
            continue
        result = writer(payload)
        if asyncio.iscoroutine(result):
            await result
        return


_BUILD_STAGE_LABELS = {
    "update.start": "开始构建技能总谱",
    "update.pause_requested": "正在暂停技能总谱构建",
    "update.paused": "技能总谱构建已暂停",
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
    "graph.score.start": "构建乐谱检索结构",
    "graph.score.done": "乐谱检索结构构建完成",
    "graph.build.done": "技能关系图构建完成",
    "artifact.graph.write.start": "写入总谱文件",
    "artifact.graph.write.done": "总谱文件写入完成",
    "state.write.start": "写入总谱状态",
    "state.write.done": "总谱状态写入完成",
    "update.failed": "总谱构建失败",
    "update.done": "总谱构建完成",
}

_BUILD_STAGE_PROGRESS = {
    "update.start": 3,
    "update.pause_requested": 100,
    "update.paused": 100,
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
    "graph.score.start": 90,
    "graph.score.done": 92,
    "graph.build.done": 94,
    "artifact.graph.write.start": 95,
    "artifact.graph.write.done": 96,
    "state.write.start": 98,
    "state.write.done": 99,
    "update.failed": 100,
    "update.done": 100,
}


def _build_log_payload(score_dir: Path | str, *, limit: int = 80) -> dict[str, Any]:
    resolved_score_dir = Path(score_dir)
    entries = _read_build_log(resolved_score_dir, limit=limit)
    token_usage = _build_token_usage_payload(resolved_score_dir, entries)
    build_progress = _build_progress(entries)
    if token_usage:
        build_progress["llm_token_usage"] = token_usage
    return {
        "build_log": entries,
        "build_progress": build_progress,
        "llm_token_usage": token_usage,
    }


def _read_build_log(score_dir: Path, *, limit: int = 80) -> list[dict[str, Any]]:
    log_path = score_dir / "build_log.jsonl"
    if not log_path.is_file():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines[-max(1, limit):]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("stage"):
            entries.append(_normalize_build_log_entry(payload))
    return entries


def _repair_interrupted_build_log(score_dir: Path) -> bool:
    """Close an orphaned running log after its owning process has disappeared.

    Callers must hold the extension build guard and first rule out a live task.
    """
    if _build_progress(_read_build_log(score_dir)).get("status") != "running":
        return False
    _BuildProcessLogger(score_dir / "build_log.jsonl").record(
        "update.paused",
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
        status = "success"
    elif stage == "update.failed":
        status = "error"
    elif stage == "update.paused":
        status = "paused"
    return {
        "stage": stage,
        "label": str(latest.get("label") or _BUILD_STAGE_LABELS.get(stage, stage)),
        "percent": _build_stage_percent(stage, latest),
        "status": status,
        "current": latest.get("current"),
        "total": latest.get("total"),
        "ts": latest.get("ts"),
    }


def _latest_effective_build_log_entry(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        return {}
    for entry in reversed(entries):
        if str(entry.get("stage") or "") in {
            "update.done",
            "update.failed",
            "update.paused",
        }:
            return entry
    return entries[-1]


def _build_token_usage_payload(
    score_dir: Path, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    status = _build_progress(entries).get("status")
    if status == "running":
        current = _current_token_usage_summary()
        if _has_token_usage(current):
            return current
        return {}

    for usage in (
        _read_manifest_token_usage(score_dir),
        _read_json_token_usage(score_dir / "llm_token_usage.json"),
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


def _read_manifest_token_usage(score_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            (resolve_score_artifact_dir(score_dir) / "score_manifest.json").read_text(
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


def _build_stage_percent(stage: str, entry: dict[str, Any]) -> int:
    if stage == "graph.resolve.progress":
        return _progress_between(entry, start=72, end=84)
    if stage.startswith("fingerprint."):
        return _progress_between(entry, start=24, end=48)
    return int(_BUILD_STAGE_PROGRESS.get(stage, 0))


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


class _BuildProcessLogger:
    def __init__(self, path: Path) -> None:
        self.path = path

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
    plan = select_primary_plan(payload)
    title = str(plan.get("title") or default_plan_title(language)).strip()
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
