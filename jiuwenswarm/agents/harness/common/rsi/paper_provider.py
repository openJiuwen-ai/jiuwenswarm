"""AgentServer Provider bridge for the OpenJiuwen paper optimizer.

``openjiuwen`` currently publishes the paper Provider as a protocol.  Its
``auto_research`` package does, however, contain the concrete ManagerRuntime
that performs the research/design/code/experiment/reporting workflow.  This
module adapts that runtime to the Provider contract owned by AgentServer.

The bridge deliberately keeps WebSocket and task-store concerns out of the
upstream runtime.  It owns only the Provider snapshots required by RSI and
polls the runtime's durable manager state so completed module reports become
visible as tree nodes while the real run is still in progress.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.resources
import json
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from openjiuwen.rsi.artifact_rsi.request import ArtifactEngineRequest
from openjiuwen.rsi.events import EventNode, EventProgress, EventStatus, OnEvent
from openjiuwen.rsi.schema import (
    ArtifactRef,
    ArtifactValidationResult,
    EngineReport,
    EngineResult,
    EngineState,
    RsiChange,
    RsiTreeNode,
    TreeResponse,
)

logger = logging.getLogger(__name__)

_STATE_FILE = "paper_provider_state.json"
_REPORT_FILE = "paper_provider_report.json"
_TREE_FILE = "paper_provider_tree.json"
_ARTIFACTS_DIR = "artifacts"
_NODE_ARTIFACTS_DIR = "node_artifacts"
_ITERATION_RE = re.compile(r"-iteration-(\d+)$")
_PACKAGE_ITERATION_RE = re.compile(r"paper-optimization-(\d+)$")
_MAX_PROVIDER_FILES = 128
_MISSING = object()
_REPORTING_TEXT_SUFFIXES = frozenset(
    {
        ".csv",
        ".json",
        ".jsonl",
        ".markdown",
        ".md",
        ".tex",
        ".text",
        ".txt",
        ".yaml",
        ".yml",
    }
)


def _safe_reporting_resource_paths(paths: Any) -> list[str]:
    """Put the reporting module's text summary before binary inputs.

    The current agent-core ReportingAgent treats ``survey.resource_paths[0]``
    as its generated ``research_summary.md`` and reads it as UTF-8.  RSI
    prepends the caller's original artifact to that list, which is commonly a
    ZIP/PDF for paper tasks.  Keep the full resource list for compatibility,
    but make the first resource the generated summary (or another text file)
    so a binary artifact is never decoded as UTF-8.

    This is an integration-boundary guard, not a paper-format validation rule:
    the Provider still owns input validation and staging.
    """

    normalized = [
        str(path).strip().replace("\\", "/")
        for path in (paths or [])
        if str(path).strip()
    ]
    if not normalized:
        return []

    summaries = [
        path
        for path in normalized
        if PurePosixPath(path).name.lower() == "research_summary.md"
    ]
    if summaries:
        return summaries + [path for path in normalized if path not in summaries]

    text_paths = [
        path
        for path in normalized
        if PurePosixPath(path).suffix.lower() in _REPORTING_TEXT_SUFFIXES
    ]
    if text_paths:
        return text_paths + [path for path in normalized if path not in text_paths]

    first = PurePosixPath(normalized[0])
    if not first.suffix:
        # A paper source is now a real directory. Preserve that directory as
        # the resource root instead of replacing it with its parent folder.
        return normalized

    # No text resource exists.  Give ReportingAgent a directory as the first
    # resource; its ``is_file()`` guard will skip background extraction rather
    # than attempting to decode a PDF/ZIP.  The original binary paths remain
    # available to later integrations through the rest of the list.
    parent = PurePosixPath(normalized[0]).parent.as_posix() or "."
    return [parent] + [path for path in normalized[1:] if path != parent]


class _ReportingAgentBoundaryAdapter:
    """Keep current agent-core reporting compatible with RSI path ordering."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    async def arun(self, inputs: Any) -> Any:
        original_paths = list(inputs.survey.resource_paths)
        safe_paths = _safe_reporting_resource_paths(original_paths)
        if safe_paths != original_paths:
            survey = inputs.survey.model_copy(update={"resource_paths": safe_paths})
            inputs = inputs.model_copy(update={"survey": survey})
        return await self._delegate.arun(inputs)

    def run(self, inputs: Any) -> Any:
        original_paths = list(inputs.survey.resource_paths)
        safe_paths = _safe_reporting_resource_paths(original_paths)
        if safe_paths != original_paths:
            survey = inputs.survey.model_copy(update={"resource_paths": safe_paths})
            inputs = inputs.model_copy(update={"survey": survey})
        return self._delegate.run(inputs)


@dataclass(frozen=True, slots=True)
class _ExecutionOutcome:
    status: str
    summary: str
    manager_run_ids: tuple[str, ...]


class PaperProvider:
    """Adapt autoResearch's real ManagerRuntime to the RSI Provider API."""

    artifact_type = "paper"
    supports_pause = False
    supports_resume = False

    # autoResearch's module agents still read model credentials from environment
    # variables.  This compatibility lock intentionally serializes paper runs
    # in one AgentServer process; _temporary_model_environment logs when a
    # concurrent run has to wait.  It can be removed once all modules accept
    # an explicit model configuration.
    _MODEL_ENV_LOCK = threading.RLock()

    def __init__(
        self,
        tasks_root: str | Path,
        *,
        poll_interval: float = 0.5,
        config_path: str | Path | None = None,
    ) -> None:
        self.tasks_root = Path(tasks_root).expanduser().resolve()
        self.poll_interval = max(0.05, float(poll_interval))
        self.config_path = Path(config_path).expanduser() if config_path else None
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_lock = threading.RLock()

    # -- Provider contract -------------------------------------------------

    def validate_input(self, artifact_path: str | None) -> ArtifactValidationResult:
        """Validate an optional paper file or source directory."""

        if not artifact_path:
            return ArtifactValidationResult(valid=True, errors=[])
        path = Path(artifact_path).expanduser()
        if not path.exists():
            return ArtifactValidationResult(
                valid=False,
                errors=[
                    {
                        "code": "PATH_INVALID",
                        "message": f"paper artifact does not exist: {path}",
                    }
                ],
            )
        if not path.is_file() and not path.is_dir():
            return ArtifactValidationResult(
                valid=False,
                errors=[
                    {
                        "code": "PATH_INVALID",
                        "message": f"paper artifact must be a file or directory: {path}",
                    }
                ],
            )
        return ArtifactValidationResult(valid=True, errors=[])

    async def run(
        self,
        request: ArtifactEngineRequest,
        on_event: OnEvent | None = None,
    ) -> EngineResult:
        task_id = str(request.task_id)
        run_dir = self._resolve_run_dir(request.run_dir, task_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / _ARTIFACTS_DIR).mkdir(parents=True, exist_ok=True)
        cancel_event = self._cancel_event(task_id)
        cancel_event.clear()

        self._initialize_snapshots(task_id, run_dir, request.max_iterations)
        await _emit(on_event, EventStatus(status="running"))

        execution = asyncio.create_task(
            asyncio.to_thread(self._execute_request, request, cancel_event)
        )
        monitor = asyncio.create_task(
            self._monitor_runtime(
                task_id,
                run_dir,
                request.max_iterations,
                execution,
                on_event,
            )
        )
        try:
            outcome = await execution
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        except Exception as exc:  # noqa: BLE001 - Provider owns terminalization
            logger.exception("[RSI] paper Provider failed task=%s", task_id)
            outcome = _ExecutionOutcome(
                status="failed",
                summary=f"paper pipeline raised {type(exc).__name__}: {exc}",
                manager_run_ids=(),
            )
        finally:
            if not monitor.done():
                if execution.cancelled():
                    monitor.cancel()
                else:
                    # The final manager report can be written immediately
                    # before the worker future completes; drain it first.
                    try:
                        await monitor
                    except asyncio.CancelledError:
                        pass

        if cancel_event.is_set() or outcome.status == "terminated":
            terminal_status = "terminated"
            error_code = None
            error_message = None
        elif outcome.status == "completed":
            terminal_status = "completed"
            error_code = None
            error_message = None
        else:
            terminal_status = "failed"
            error_code = "PAPER_PIPELINE_FAILED"
            error_message = outcome.summary[:500]

        self._finalize_snapshots(
            task_id,
            run_dir,
            terminal_status,
            outcome,
            error_code=error_code,
            error_message=error_message,
        )
        await _emit(on_event, EventStatus(status=terminal_status))
        return EngineResult(
            task_id=task_id,
            status=terminal_status,
            final_node_id=self._read_json(task_id, _REPORT_FILE).get("best_node_id"),
            error_code=error_code,
            error_message=error_message,
        )

    async def pause(
        self,
        task_id: str,
        on_event: OnEvent | None = None,
    ) -> EngineResult:
        del on_event
        return EngineResult(
            task_id=task_id,
            status="created",
            final_node_id=None,
            error_code="SCENARIO_NOT_SUPPORTED",
            error_message="paper artifact optimization does not support pause",
        )

    async def resume(
        self,
        request: ArtifactEngineRequest,
        on_event: OnEvent | None = None,
    ) -> EngineResult:
        del on_event
        return EngineResult(
            task_id=request.task_id,
            status="created",
            final_node_id=None,
            error_code="SCENARIO_NOT_SUPPORTED",
            error_message="paper artifact optimization does not support resume",
        )

    async def terminate(
        self,
        task_id: str,
        on_event: OnEvent | None = None,
    ) -> EngineResult:
        state = self._read_json(task_id, _STATE_FILE)
        if not state:
            return EngineResult(
                task_id=task_id,
                status="failed",
                final_node_id=None,
                error_code="TASK_NOT_FOUND",
                error_message="paper Provider snapshot missing",
            )
        self._cancel_event(task_id).set()
        state["status"] = "terminated"
        state["updated_at"] = _utc_now()
        report = self._read_json(task_id, _REPORT_FILE)
        report["status"] = "terminated"
        report["summary"] = "paper optimization terminated by operator"
        self._write_task_snapshots(task_id, state=state, report=report)
        await _emit(on_event, EventStatus(status="terminated"))
        return EngineResult(
            task_id=task_id,
            status="terminated",
            final_node_id=state.get("best_node_id"),
            error_code=None,
            error_message=None,
        )

    def read_state(self, task_id: str) -> EngineState:
        state = self._require_json(task_id, _STATE_FILE)
        return EngineState(
            task_id=task_id,
            status=str(state.get("status") or "created"),
            iteration=int(state.get("iteration", 0) or 0),
            total_iterations=int(state.get("total_iterations", 0) or 0),
            best_node_id=state.get("best_node_id"),
            score=state.get("score"),
            baseline=state.get("baseline"),
            usage=None,
            updated_at=str(state.get("updated_at") or ""),
            error_code=state.get("error_code"),
            error_message=state.get("error_message"),
        )

    def read_report(self, task_id: str) -> EngineReport:
        report = self._require_json(task_id, _REPORT_FILE)
        refs = [ArtifactRef(**item) for item in report.get("artifact_index") or []]
        return EngineReport(
            task_id=task_id,
            status=str(report.get("status") or "created"),
            best_node_id=report.get("best_node_id"),
            usage=None,
            artifact_index=refs,
            summary=report.get("summary"),
        )

    def get_tree(self, task_id: str) -> TreeResponse:
        tree = self._require_json(task_id, _TREE_FILE)
        nodes = [_node_from_dict(item) for item in tree.get("nodes") or []]
        return TreeResponse(
            nodes=nodes,
            depth=int(tree.get("depth", 0) or 0),
            iteration=int(tree.get("iteration", 0) or 0),
        )

    def locate_artifact(self, task_id: str, artifact_id: str | None = None) -> ArtifactRef:
        report = self.read_report(task_id)
        if artifact_id:
            for ref in report.artifact_index:
                if ref.artifact_id == artifact_id:
                    return ref
        else:
            for ref in reversed(report.artifact_index):
                if ref.node_id == report.best_node_id:
                    return ref
            if report.artifact_index:
                return report.artifact_index[-1]
        raise FileNotFoundError(
            f"paper artifact not found: {task_id}/{artifact_id or '<best>'}"
        )

    # -- Real autoResearch execution --------------------------------------

    def _execute_request(
        self,
        request: ArtifactEngineRequest,
        cancel_event: threading.Event,
    ) -> _ExecutionOutcome:
        run_dir = self._resolve_run_dir(request.run_dir, request.task_id)
        staged_paths = self._stage_input_file(request.artifact_path, run_dir)
        staged_artifact_path = str(staged_paths[0]) if staged_paths else None
        base_research_paths = [self._relative_to(run_dir, path) for path in staged_paths]
        topic = (request.optimization_instruction or "").strip()
        if not topic:
            topic = "Improve the supplied paper artifact and validate the improvement."
        initial_prompt = (
            "This task was submitted through RSI. Follow the optimization instruction "
            "as the research objective and persist all useful evidence in the run workspace.\n\n"
            f"OPTIMIZATION INSTRUCTION:\n{request.optimization_instruction or topic}\n"
        )

        manager_run_ids: list[str] = []
        previous_pdf: str | None = None
        for iteration in range(1, max(1, int(request.max_iterations)) + 1):
            if cancel_event.is_set():
                return _ExecutionOutcome("terminated", "terminated by operator", tuple(manager_run_ids))
            manager_run_id = f"{request.task_id}-iteration-{iteration:03d}"
            manager_run_ids.append(manager_run_id)
            research_paths = list(base_research_paths)
            if previous_pdf:
                previous_rel = self._relative_to(run_dir, Path(previous_pdf))
                if previous_rel and previous_rel not in research_paths:
                    research_paths.append(previous_rel)
            terminal = self._run_manager(
                request,
                run_dir=run_dir,
                manager_run_id=manager_run_id,
                topic=topic,
                initial_prompt=initial_prompt,
                research_paths=research_paths,
                artifact_path=staged_artifact_path,
            )
            summary = str(getattr(terminal, "summary", "") or "")
            self._make_iteration_package(run_dir, manager_run_id, iteration)
            if cancel_event.is_set():
                return _ExecutionOutcome("terminated", "terminated by operator", tuple(manager_run_ids))
            terminal_status = str(getattr(getattr(terminal, "status", None), "value", getattr(terminal, "status", "")))
            if terminal_status != "complete":
                return _ExecutionOutcome(
                    "failed",
                    summary or f"autoResearch terminal status: {terminal_status or 'unknown'}",
                    tuple(manager_run_ids),
                )
            previous_pdf = self._find_main_pdf(run_dir, manager_run_id)

        return _ExecutionOutcome("completed", "paper optimization completed", tuple(manager_run_ids))

    def _run_manager(
        self,
        request: ArtifactEngineRequest,
        *,
        run_dir: Path,
        manager_run_id: str,
        topic: str,
        initial_prompt: str,
        research_paths: list[str],
        artifact_path: str | None = None,
    ) -> Any:
        from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.config.settings import (
            load_config,
        )
        from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common.workspace import (
            set_project_root,
        )
        from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_design.agent import (
            ExperimentDesignAgent,
        )
        from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.manager.agent import (
            ManagerAgent,
        )
        from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reflection.agent import (
            ReflectionAgent,
        )
        from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.agent import (
            ReportingAgent,
        )
        from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.topic_survey.agent import (
            TopicSurveyAgent,
        )
        from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.pipeline.manager import (
            ManagerRuntime,
        )

        config = self._load_config(load_config)
        config = self._configure_for_model(config, request.model)
        topic_config = dict(config.get("topic_survey") or {})
        topic_config["web_proxy"] = str(request.web_proxy or "").strip() or None
        configured_scope = str(topic_config.get("search_scope") or "").strip().lower()
        if configured_scope not in {"domestic", "global"}:
            topic_config["search_scope"] = "global"
        config["topic_survey"] = topic_config
        set_project_root(run_dir)
        manager = ManagerAgent(
            config,
            model=request.model,
            project_root_path=run_dir,
        )
        runtime = ManagerRuntime(
            config,
            manager=manager,
            topic_survey=TopicSurveyAgent(config, model=request.model),
            experiment_design=ExperimentDesignAgent(
                config,
                model=request.model,
                project_root_path=run_dir,
            ),
            reflection=ReflectionAgent(config),
            reporting=_ReportingAgentBoundaryAdapter(ReportingAgent(config)),
            artifact_path=artifact_path if artifact_path is not None else request.artifact_path,
        )
        with self._temporary_model_environment(request.model, task_id=request.task_id):
            set_project_root(run_dir)
            return asyncio.run(
                runtime.arun(
                    topic=topic,
                    research_paths=research_paths,
                    run_id=manager_run_id,
                    objective=request.optimization_instruction or topic,
                    initial_prompt=initial_prompt,
                    task_mode="modify_paper" if request.artifact_path else "create_new_paper",
                )
            )

    def _load_config(self, load_config: Any) -> dict[str, Any]:
        if self.config_path is not None:
            return load_config(self.config_path)
        resource = importlib.resources.files(
            "openjiuwen.rsi.artifact_rsi.paper_opt"
        ).joinpath("configs/pipeline.default.yaml")
        with importlib.resources.as_file(resource) as path:
            return load_config(path)

    @staticmethod
    def _configure_for_model(config: dict[str, Any], model: Any) -> dict[str, Any]:
        client = getattr(model, "model_client_config", None)
        request_config = getattr(model, "model_config", None)
        settings = dict(config.get("openjiuwen") or {})
        provider = _model_value(client, "client_provider")
        model_name = _model_value(request_config, "model_name")
        base_url = _model_value(client, "api_base")
        timeout = _model_value(client, "timeout")
        if provider:
            settings["provider"] = str(provider)
        if model_name:
            settings["model"] = str(model_name)
        if base_url:
            settings["base_url"] = str(base_url)
        if timeout:
            settings["timeout"] = timeout
        config["openjiuwen"] = settings
        return config

    @classmethod
    @contextlib.contextmanager
    def _temporary_model_environment(
        cls,
        model: Any,
        *,
        task_id: str | None = None,
    ) -> Iterator[None]:
        client = getattr(model, "model_client_config", None)
        request_config = getattr(model, "model_config", None)
        values = {
            "API_KEY": _model_value(client, "api_key"),
            "API_BASE": _model_value(client, "api_base"),
            "MODEL_PROVIDER": _model_value(client, "client_provider"),
            "MODEL_NAME": _model_value(request_config, "model_name"),
            "MODEL_TIMEOUT": _model_value(client, "timeout"),
        }
        values = {key: str(value) for key, value in values.items() if value not in (None, "")}
        acquired_without_wait = cls._MODEL_ENV_LOCK.acquire(blocking=False)
        if not acquired_without_wait:
            logger.warning(
                "[RSI] paper Provider 等待进程级模型环境锁；"
                "并发 paper 任务将在当前任务完成后执行: task=%s",
                task_id or "<unknown>",
            )
            cls._MODEL_ENV_LOCK.acquire()
        try:
            previous: dict[str, object] = {}
            for key, value in values.items():
                previous[key] = os.environ.get(key, _MISSING)
                os.environ[key] = value
            try:
                yield
            finally:
                for key, value in previous.items():
                    if value is _MISSING:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = str(value)
        finally:
            cls._MODEL_ENV_LOCK.release()

    # -- Runtime observation and snapshots --------------------------------

    async def _monitor_runtime(
        self,
        task_id: str,
        run_dir: Path,
        total_iterations: int,
        execution: asyncio.Task[Any],
        on_event: OnEvent | None,
    ) -> None:
        seen: set[tuple[str, int]] = set()
        while True:
            await self._scan_manager_reports(
                task_id,
                run_dir,
                total_iterations,
                seen,
                on_event,
            )
            if execution.done():
                # One last scan closes the race between manager persistence and
                # the worker thread returning its terminal report.
                await self._scan_manager_reports(
                    task_id,
                    run_dir,
                    total_iterations,
                    seen,
                    on_event,
                )
                return
            await asyncio.sleep(self.poll_interval)

    async def _scan_manager_reports(
        self,
        task_id: str,
        run_dir: Path,
        total_iterations: int,
        seen: set[tuple[str, int]],
        on_event: OnEvent | None,
    ) -> None:
        experiments = run_dir / "experiments"
        if not experiments.is_dir():
            return
        for state_path in sorted(experiments.glob("*/manager/state.json")):
            manager_run_id = state_path.parent.parent.name
            match = _ITERATION_RE.search(manager_run_id)
            iteration = int(match.group(1)) if match else 1
            payload = _read_json_file(state_path)
            reports = payload.get("reports") if isinstance(payload, dict) else None
            if not isinstance(reports, list):
                continue
            for index, report in enumerate(reports):
                key = (manager_run_id, index)
                if key in seen or not isinstance(report, dict):
                    continue
                try:
                    await self._record_manager_report(
                        task_id,
                        run_dir,
                        manager_run_id,
                        iteration,
                        index,
                        report,
                        total_iterations,
                        on_event,
                    )
                except Exception:  # noqa: BLE001 - retry the report next poll
                    logger.exception(
                        "[RSI] paper Provider could not project manager report task=%s run=%s index=%s",
                        task_id,
                        manager_run_id,
                        index,
                    )
                    continue
                seen.add(key)

    async def _record_manager_report(
        self,
        task_id: str,
        run_dir: Path,
        manager_run_id: str,
        iteration: int,
        index: int,
        manager_report: dict[str, Any],
        total_iterations: int,
        on_event: OnEvent | None,
    ) -> None:
        state = self._read_json(task_id, _STATE_FILE)
        report = self._read_json(task_id, _REPORT_FILE)
        tree = self._read_json(task_id, _TREE_FILE)
        module = str(manager_report.get("module") or "module")
        attempt = int(manager_report.get("attempt", index + 1) or index + 1)
        outcome = str(manager_report.get("outcome") or "failed")
        node_id = f"{task_id}:iteration:{iteration}:module:{module}:attempt:{attempt}:{index}"
        parent_id = _last_node_id(tree) or "ROOT"
        node_artifact = await asyncio.to_thread(
            self._build_node_artifact_ref,
            task_id,
            run_dir,
            iteration,
            index,
            module,
            node_id,
            manager_report.get("artifact_paths"),
        )
        artifact_refs = [node_artifact] if node_artifact is not None else []
        report_index = list(report.get("artifact_index") or [])
        existing_ids = {item.get("artifact_id") for item in report_index if isinstance(item, dict)}
        for ref in artifact_refs:
            if ref["artifact_id"] not in existing_ids:
                report_index.append(ref)
                existing_ids.add(ref["artifact_id"])
        summary = str(manager_report.get("summary") or f"{module} {outcome}")
        adopted = outcome == "succeeded"
        node_extra: dict[str, Any] = {
            "module": module,
            "attempt": attempt,
            "manager_run_id": manager_run_id,
            "content": _compact_manager_report(manager_report),
            "artifacts": artifact_refs,
        }
        if node_artifact is not None:
            # This is the canonical node-level artifact used by both the
            # detail preview and the node download action.  Keep the raw
            # manager paths inside ``content`` for diagnostics only.
            node_extra["artifact_path"] = node_artifact["path"]
        change = RsiChange(
            group="paper",
            operation="module",
            function=module,
            target=manager_run_id,
            summary=summary[:500],
        )
        node = RsiTreeNode(
            node_id=node_id,
            iteration=iteration,
            parent_id=parent_id,
            type="adopted" if adopted else "rejected",
            adopted=adopted,
            score=None,
            summary=summary[:1000],
            snapshot_artifact_id=(node_artifact["artifact_id"] if node_artifact else None),
            reason=None if adopted else summary[:500],
            failure_class=None if adopted else str(manager_report.get("runtime_failure") or "PAPER_MODULE_FAILED"),
            changes=[change],
            extra=node_extra,
        )
        nodes = [item for item in tree.get("nodes") or [] if item.get("node_id") != node_id]
        nodes.append(asdict(node))
        tree.update(
            {
                "nodes": nodes,
                "iteration": max(int(tree.get("iteration", 0) or 0), iteration),
                "depth": max(int(tree.get("depth", 0) or 0), iteration),
            }
        )
        state.update(
            {
                "status": "terminated" if self._cancel_event(task_id).is_set() else "running",
                "iteration": max(int(state.get("iteration", 0) or 0), iteration),
                "total_iterations": total_iterations,
                "best_node_id": node_id if adopted else state.get("best_node_id"),
                "updated_at": _utc_now(),
                "current_stage": module,
            }
        )
        report.update(
            {
                "status": state["status"],
                "best_node_id": state.get("best_node_id"),
                "artifact_index": report_index,
                "summary": summary[:1000],
            }
        )
        self._write_task_snapshots(task_id, state=state, report=report, tree=tree)
        await _emit(on_event, EventNode(node=node))
        await _emit(
            on_event,
            EventProgress(
                iteration=iteration,
                total_iterations=total_iterations,
                score=None,
                baseline=None,
                usage=None,
            ),
        )

    def _initialize_snapshots(self, task_id: str, run_dir: Path, total_iterations: int) -> None:
        root = RsiTreeNode(
            node_id="ROOT",
            iteration=0,
            parent_id=None,
            type="root",
            adopted=True,
            score=None,
            summary="paper optimization root",
            snapshot_artifact_id=None,
            reason=None,
            failure_class=None,
            changes=[],
            extra={"content": {"kind": "paper_optimization", "task_id": task_id}},
        )
        state = {
            "task_id": task_id,
            "status": "running",
            "iteration": 0,
            "total_iterations": max(1, int(total_iterations)),
            "best_node_id": "ROOT",
            "score": None,
            "baseline": None,
            "usage": None,
            "updated_at": _utc_now(),
            "error_code": None,
            "error_message": None,
            "current_stage": None,
        }
        report = {
            "task_id": task_id,
            "status": "running",
            "best_node_id": "ROOT",
            "usage": None,
            "artifact_index": [],
            "summary": "paper optimization is starting",
        }
        tree = {"nodes": [asdict(root)], "depth": 0, "iteration": 0}
        self._write_task_snapshots(task_id, state=state, report=report, tree=tree, run_dir=run_dir)

    def _finalize_snapshots(
        self,
        task_id: str,
        run_dir: Path,
        status: str,
        outcome: _ExecutionOutcome,
        *,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        state = self._read_json(task_id, _STATE_FILE)
        report = self._read_json(task_id, _REPORT_FILE)
        tree = self._read_json(task_id, _TREE_FILE)
        artifact_index = list(report.get("artifact_index") or [])
        for package in sorted((run_dir / _ARTIFACTS_DIR).glob("paper-optimization-*")):
            if not package.is_dir() and not (
                package.is_file() and package.suffix.lower() == ".zip"
            ):
                continue
            node = _find_best_node(tree, package)
            ref = self._artifact_ref(
                task_id,
                package,
                node_id=(node.get("node_id") if node is not None else state.get("best_node_id")),
                artifact_id=f"{task_id}:package:{package.stem}",
                kind="paper_snapshot",
            )
            if ref is None:
                continue
            # A node may first be projected with its module-level package and
            # later receive the complete iteration package.  The latter is
            # the downloadable/previewable artifact for that node.
            artifact_index = [
                item
                for item in artifact_index
                if not (isinstance(item, dict) and item.get("node_id") == ref.get("node_id"))
            ]
            if not any(item.get("artifact_id") == ref["artifact_id"] for item in artifact_index):
                artifact_index.append(ref)
            if node is not None:
                node["snapshot_artifact_id"] = ref["artifact_id"]
                extra = node.setdefault("extra", {})
                extra["artifact_path"] = ref["path"]
                extra["artifacts"] = [ref]
                if state.get("best_node_id") in (None, "ROOT"):
                    state["best_node_id"] = node.get("node_id")
                    report["best_node_id"] = node.get("node_id")
        state.update(
            {
                "status": status,
                "iteration": max(
                    int(state.get("iteration", 0) or 0),
                    len(outcome.manager_run_ids),
                ),
                "updated_at": _utc_now(),
                "error_code": error_code,
                "error_message": error_message,
            }
        )
        report.update(
            {
                "status": status,
                "best_node_id": state.get("best_node_id"),
                "artifact_index": artifact_index,
                "summary": outcome.summary[:2000],
            }
        )
        self._write_task_snapshots(task_id, state=state, report=report, tree=tree, run_dir=run_dir)

    # -- Input/artifact filesystem helpers --------------------------------

    def _stage_input_file(self, artifact_path: str | None, run_dir: Path) -> list[Path]:
        if not artifact_path:
            return []
        source = Path(artifact_path).expanduser().resolve()
        if not source.is_file() and not source.is_dir():
            raise ValueError(f"paper artifact must be a file or directory: {source}")
        destination = run_dir / "input" / "paper"
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / source.name
        if target.exists() or target.is_symlink():
            self._remove_staging_path(target)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        return [target]

    @staticmethod
    def _remove_staging_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    def _make_iteration_package(self, run_dir: Path, manager_run_id: str, iteration: int) -> Path:
        destination = run_dir / _ARTIFACTS_DIR / f"paper-optimization-{iteration:03d}"
        source_root = run_dir / "experiments" / manager_run_id
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._remove_staging_path(destination)
        # Remove the old representation if this task directory is reused after
        # an earlier Provider version. New paper artifacts are directories.
        self._remove_staging_path(destination.with_suffix(".zip"))
        destination.mkdir(parents=True, exist_ok=True)
        if source_root.is_dir():
            shutil.copytree(source_root, destination, dirs_exist_ok=True)
        else:
            (destination / "README.txt").write_text(
                "autoResearch did not create a workspace\n", encoding="utf-8"
            )
        return destination

    @staticmethod
    def _find_main_pdf(run_dir: Path, manager_run_id: str) -> str | None:
        candidate = run_dir / "experiments" / manager_run_id / "paper" / "main.pdf"
        return str(candidate) if candidate.is_file() else None

    def _build_node_artifact_ref(
        self,
        task_id: str,
        run_dir: Path,
        iteration: int,
        report_index: int,
        module: str,
        node_id: str,
        raw_paths: Any,
    ) -> dict[str, Any] | None:
        package = self._make_node_package(
            task_id,
            run_dir,
            iteration,
            report_index,
            module,
            node_id,
            raw_paths,
        )
        if package is None:
            return None
        return self._artifact_ref(
            task_id,
            package,
            node_id=node_id,
            artifact_id=f"{task_id}:artifact:node:{iteration}:{report_index}",
            kind="paper_node",
        )

    def _make_node_package(
        self,
        task_id: str,
        run_dir: Path,
        iteration: int,
        report_index: int,
        module: str,
        node_id: str,
        raw_paths: Any,
    ) -> Path | None:
        """Package all files reported by one manager module into one node artifact.

        Manager reports often contain several paths from different parts of
        the run workspace.  Indexing the first path made the UI download an
        ``agent_trace.jsonl`` or instruction file instead of the node result.
        A node package gives the frontend one stable, self-describing target
        while retaining every file produced by that module.
        """

        candidates = self._collect_provider_files(run_dir, raw_paths, node_id=node_id)
        if not candidates:
            return None

        safe_module = re.sub(r"[^A-Za-z0-9_.-]+", "-", module).strip("-.") or "module"
        node_digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:12]
        destination = (
            run_dir
            / _ARTIFACTS_DIR
            / _NODE_ARTIFACTS_DIR
            / f"paper-{safe_module}-{iteration:03d}-{report_index:03d}-{node_digest}"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._remove_staging_path(destination)
        self._remove_staging_path(destination.with_suffix(".zip"))
        manifest = {
            "provider": "PaperProvider",
            "artifact_kind": "paper_node",
            "task_id": task_id,
            "node_id": node_id,
            "iteration": iteration,
            "module": module,
            "source_files": [self._relative_to(run_dir, path) for path in candidates],
        }
        destination.mkdir(parents=True, exist_ok=True)
        for path in candidates:
            relative = self._relative_to(run_dir, path)
            if not relative:
                continue
            target = destination / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        manifest_path = destination / "__rsi_artifact__" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return destination

    def _collect_provider_files(
        self,
        run_dir: Path,
        raw_paths: Any,
        *,
        node_id: str | None = None,
    ) -> list[Path]:
        candidates: list[Path] = []
        for raw in raw_paths or []:
            path = self._provider_path(run_dir, raw)
            if path is None:
                continue
            if path.is_file():
                candidates.append(path)
            elif path.is_dir():
                candidates.extend(item for item in sorted(path.rglob("*")) if item.is_file())
        unique: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        if len(unique) > _MAX_PROVIDER_FILES:
            logger.warning(
                "[RSI] paper Provider node %s reported %d files; truncating %d files to %d",
                node_id or "<unknown>",
                len(unique),
                len(unique) - _MAX_PROVIDER_FILES,
                _MAX_PROVIDER_FILES,
            )
            return unique[:_MAX_PROVIDER_FILES]
        return unique

    def _artifact_ref(
        self,
        task_id: str,
        path: Path,
        *,
        node_id: str | None,
        artifact_id: str,
        kind: str,
    ) -> dict[str, Any] | None:
        if not path.is_file() and not path.is_dir():
            return None
        return asdict(
            ArtifactRef(
                artifact_id=artifact_id,
                node_id=node_id,
                name=path.name,
                kind=kind,
                path=str(path.resolve()),
                sha256=_sha256(path),
                download_url=None,
            )
        )

    def _provider_path(self, run_dir: Path, raw: Any) -> Path | None:
        if not raw:
            return None
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = run_dir / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(run_dir.resolve())
        except (OSError, ValueError):
            return None
        return resolved if resolved.exists() else None

    @staticmethod
    def _relative_to(root: Path, path: Path) -> str:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return ""

    def _resolve_run_dir(self, run_dir: str | Path, task_id: str) -> Path:
        resolved = Path(run_dir).expanduser().resolve()
        expected = (self.tasks_root / task_id).resolve()
        try:
            resolved.relative_to(expected)
        except ValueError as exc:
            raise ValueError("paper Provider run_dir must stay inside the task directory") from exc
        return resolved

    # -- Snapshot persistence ----------------------------------------------

    def _cancel_event(self, task_id: str) -> threading.Event:
        with self._cancel_lock:
            return self._cancel_events.setdefault(task_id, threading.Event())

    def _write_task_snapshots(
        self,
        task_id: str,
        *,
        state: dict[str, Any],
        report: dict[str, Any],
        tree: dict[str, Any] | None = None,
        run_dir: Path | None = None,
    ) -> None:
        directory = run_dir or self._read_run_dir(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(directory / _STATE_FILE, state)
        _atomic_write_json(directory / _REPORT_FILE, report)
        if tree is not None:
            _atomic_write_json(directory / _TREE_FILE, tree)

    def _read_run_dir(self, task_id: str) -> Path:
        return self.tasks_root / task_id / "run"

    def _read_json(self, task_id: str, filename: str) -> dict[str, Any]:
        return _read_json_file(self._read_run_dir(task_id) / filename)

    def _require_json(self, task_id: str, filename: str) -> dict[str, Any]:
        path = self._read_run_dir(task_id) / filename
        if not path.is_file():
            raise FileNotFoundError(f"paper Provider snapshot not found: {task_id}/{filename}")
        return _read_json_file(path)


def _model_value(value: Any, name: str) -> Any:
    if value is None:
        return None
    raw = getattr(value, name, None)
    return getattr(raw, "value", raw)


async def _emit(on_event: OnEvent | None, event: Any) -> None:
    if on_event is not None:
        await on_event(event)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _compact_manager_report(report: dict[str, Any]) -> dict[str, Any]:
    view = {
        key: report.get(key)
        for key in (
            "report_id",
            "module",
            "mode",
            "attempt",
            "outcome",
            "retryable",
            "runtime_failure",
            "summary",
            "artifact_paths",
            "handoff",
        )
        if key in report
    }
    encoded = json.dumps(view, ensure_ascii=False, default=str)
    if len(encoded) <= 12000:
        return view
    view.pop("handoff", None)
    view["summary"] = str(view.get("summary") or "")[:4000]
    return view


def _last_node_id(tree: dict[str, Any]) -> str | None:
    nodes = [item for item in tree.get("nodes") or [] if isinstance(item, dict)]
    for item in reversed(nodes):
        node_id = item.get("node_id")
        if node_id and node_id != "ROOT":
            return str(node_id)
    return "ROOT" if nodes else None


def _find_best_node(tree: dict[str, Any], package: Path) -> dict[str, Any] | None:
    match = _PACKAGE_ITERATION_RE.search(package.stem)
    iteration = int(match.group(1)) if match else None
    nodes = [item for item in tree.get("nodes") or [] if isinstance(item, dict)]
    candidates = [
        item
        for item in nodes
        if item.get("node_id") != "ROOT"
        and (iteration is None or int(item.get("iteration", 0) or 0) == iteration)
        and bool(item.get("adopted"))
    ]
    return candidates[-1] if candidates else (nodes[-1] if nodes else None)


def _node_from_dict(raw: Any) -> RsiTreeNode:
    data = raw if isinstance(raw, dict) else {}
    changes = [
        RsiChange(**item)
        for item in data.get("changes") or []
        if isinstance(item, dict)
    ]
    return RsiTreeNode(
        node_id=str(data.get("node_id") or ""),
        iteration=int(data.get("iteration", 0) or 0),
        parent_id=data.get("parent_id"),
        type=str(data.get("type") or "rejected"),
        adopted=bool(data.get("adopted")),
        score=data.get("score"),
        summary=data.get("summary"),
        snapshot_artifact_id=data.get("snapshot_artifact_id"),
        reason=data.get("reason"),
        failure_class=data.get("failure_class"),
        changes=changes,
        extra=data.get("extra") if isinstance(data.get("extra"), dict) else {},
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    elif path.is_dir():
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            digest.update(child.relative_to(path).as_posix().encode("utf-8"))
            digest.update(b"\0")
            with child.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


__all__ = ["PaperProvider"]
