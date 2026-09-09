"""Deterministic Harness Provider for RSI service and Web E2E.

This is a real asynchronous execution path: every iteration writes durable
state, report, tree, and a task-scoped harness artifact before emitting the
corresponding Provider events.  It deliberately does not call a model or
mutate the active harness, which makes it safe to run from a local AgentServer
without model credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openjiuwen.rsi.events import EventNode, EventProgress, EventStatus, NodeStageEvent
from openjiuwen.rsi.schema import (
    ArtifactRef,
    EngineReport,
    EngineResult,
    EngineState,
    RsiChange,
    RsiTreeNode,
    RsiUsage,
    RsiUsageTokens,
    TreeResponse,
)

from jiuwenswarm.agents.harness.common.rsi.adapter import default_validate_input
from jiuwenswarm.agents.harness.common.rsi.errors import RsiPathInvalid
from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineRequest


_MOCK_EVAL_CASES = 3


def _mock_case_stage(case_index: int, total_cases: int, status: str) -> dict[str, Any]:
    """Build a deterministic harness evaluation case stage (id/name 契约)。"""
    if status == "passed":
        name = f"Case {case_index}/{total_cases} passed"
    elif status == "failed":
        name = f"Case {case_index}/{total_cases} failed"
    else:
        name = f"Case {case_index}/{total_cases}"
    return {
        "id": f"evaluate.case.{case_index}",
        "name": name,
        "status": status,
        "case_index": case_index,
        "total_cases": total_cases,
    }


class MockHarnessProvider:
    """A resumable, disk-backed mock implementation of HarnessProvider."""

    supports_pause = True
    supports_resume = True
    supports_terminate = True

    def __init__(
        self,
        tasks_root: str | Path,
        *,
        iteration_delay: float = 0.1,
        case_delay: float | None = None,
    ) -> None:
        self.tasks_root = Path(tasks_root)
        self.iteration_delay = max(0.0, float(iteration_delay))
        self.case_delay = (
            max(0.0, float(case_delay))
            if case_delay is not None
            else max(0.0, self.iteration_delay * 0.25)
        )

    def validate_input(self, dataset_path: str | None) -> Any:
        if not dataset_path:
            return {
                "valid": False,
                "sample_count": None,
                "errors": [{"code": "DATASET_REQUIRED", "message": "input_file is required"}],
            }
        try:
            return default_validate_input(dataset_path)
        except RsiPathInvalid as exc:
            return {
                "valid": False,
                "sample_count": None,
                "errors": [{"code": "PATH_INVALID", "message": str(exc)}],
            }

    async def run(self, request: HarnessEngineRequest, *, on_event: Any = None) -> EngineResult:
        return await self._run(request, on_event=on_event, start_iteration=1, create_root=True)

    async def resume(self, request: HarnessEngineRequest, *, on_event: Any = None) -> EngineResult:
        state = self._load_json(request.task_id, "mock_harness_state.json")
        if state is None:
            return await self._run(request, on_event=on_event, start_iteration=1, create_root=True)
        status = str(state.get("status") or "created").lower()
        if status in {"completed", "failed", "terminated"}:
            return self._result(request.task_id, status, final_node_id=state.get("best_node_id"))
        start_iteration = max(1, int(state.get("iteration", 0) or 0) + 1)
        if start_iteration > request.max_iterations:
            state["status"] = "completed"
            self._save_json(request.task_id, "mock_harness_state.json", state)
            report = self._load_json(request.task_id, "mock_harness_report.json")
            if report is not None:
                report["status"] = "completed"
                self._save_json(request.task_id, "mock_harness_report.json", report)
            return self._result(request.task_id, "completed", final_node_id=state.get("best_node_id"))
        return await self._run(
            request,
            on_event=on_event,
            start_iteration=start_iteration,
            create_root=False,
        )

    async def pause(self, task_id: str) -> EngineResult:
        state = self._load_json(task_id, "mock_harness_state.json") or self._new_state(task_id, 0)
        status = str(state.get("status") or "created").lower()
        if status in {"completed", "failed", "terminated", "paused"}:
            return self._result(task_id, status, final_node_id=state.get("best_node_id"))
        state["status"] = "paused"
        self._save_json(task_id, "mock_harness_state.json", state)
        self._update_report_status(task_id, "paused", state.get("best_node_id"))
        return self._result(task_id, "paused", final_node_id=state.get("best_node_id"))

    async def terminate(self, task_id: str) -> EngineResult:
        state = self._load_json(task_id, "mock_harness_state.json") or self._new_state(task_id, 0)
        status = str(state.get("status") or "created").lower()
        if status in {"completed", "failed", "terminated"}:
            return self._result(task_id, status, final_node_id=state.get("best_node_id"))
        state["status"] = "terminated"
        self._save_json(task_id, "mock_harness_state.json", state)
        self._update_report_status(task_id, "terminated", state.get("best_node_id"))
        return self._result(task_id, "terminated", final_node_id=state.get("best_node_id"))

    def read_state(self, task_id: str) -> EngineState:
        state = self._load_json(task_id, "mock_harness_state.json")
        if state is None:
            raise FileNotFoundError(f"mock harness state not found: {task_id}")
        return EngineState(
            task_id=task_id,
            status=str(state.get("status") or "created"),
            iteration=int(state.get("iteration", 0) or 0),
            total_iterations=int(state.get("total_iterations", 0) or 0),
            best_node_id=state.get("best_node_id"),
            score=state.get("score"),
            baseline=state.get("baseline"),
            usage=_usage_from_dict(state.get("usage")),
            updated_at=str(state.get("updated_at") or ""),
            error_code=state.get("error_code"),
            error_message=state.get("error_message"),
        )

    def read_report(self, task_id: str) -> EngineReport:
        report = self._load_json(task_id, "mock_harness_report.json")
        if report is None:
            raise FileNotFoundError(f"mock harness report not found: {task_id}")
        refs = [ArtifactRef(**item) for item in report.get("artifact_index") or []]
        return EngineReport(
            task_id=task_id,
            status=str(report.get("status") or "created"),
            best_node_id=report.get("best_node_id"),
            usage=_usage_from_dict(report.get("usage")),
            artifact_index=refs,
            summary=report.get("summary"),
        )

    def get_tree(self, task_id: str) -> TreeResponse:
        tree = self._load_json(task_id, "mock_harness_tree.json")
        if tree is None:
            raise FileNotFoundError(f"mock harness tree not found: {task_id}")
        return TreeResponse(
            nodes=[_node_from_dict(item) for item in tree.get("nodes") or []],
            depth=int(tree.get("depth", 0) or 0),
            iteration=int(tree.get("iteration", 0) or 0),
        )

    def locate_artifact(self, task_id: str, artifact_id: str | None = None) -> ArtifactRef:
        report = self.read_report(task_id)
        if artifact_id is None:
            for ref in reversed(report.artifact_index):
                if ref.node_id == report.best_node_id:
                    return ref
            if report.artifact_index:
                return report.artifact_index[-1]
        else:
            for ref in report.artifact_index:
                if ref.artifact_id == artifact_id:
                    return ref
        raise FileNotFoundError(f"mock harness artifact not found: {task_id}/{artifact_id or '<best>'}")

    async def _run(
        self,
        request: HarnessEngineRequest,
        *,
        on_event: Any,
        start_iteration: int,
        create_root: bool,
    ) -> EngineResult:
        task_id = request.task_id
        run_dir = Path(request.output_dir).expanduser().resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

        state = self._load_json(task_id, "mock_harness_state.json") or self._new_state(
            task_id, request.max_iterations
        )
        tree = self._load_json(task_id, "mock_harness_tree.json") or {
            "nodes": [],
            "iteration": 0,
            "depth": 0,
        }
        report = self._load_json(task_id, "mock_harness_report.json") or {
            "task_id": task_id,
            "status": "created",
            "best_node_id": None,
            "usage": None,
            "artifact_index": [],
            "summary": None,
        }

        # A pause/terminate command can arrive in the small window between
        # worker RUNNING and the first Provider checkpoint.  Preserve that
        # command instead of overwriting the durable control state with
        # ``running``.  Resume explicitly opts back into execution below.
        prior_status = str(state.get("status") or "created").lower()
        if not request.resume and prior_status in {"paused", "terminated"}:
            await _emit(on_event, EventStatus(status=prior_status))
            return self._result(task_id, prior_status, final_node_id=state.get("best_node_id"))

        state.update(
            {
                "status": "running",
                "total_iterations": request.max_iterations,
                "search_width": request.search_width,
                "dataset_files": list(request.dataset_files),
                "harness_refs_path": request.harness_refs_path,
                "error_code": None,
                "error_message": None,
            }
        )
        if create_root and not tree["nodes"]:
            tree["nodes"].append(asdict(self._root_node()))
        self._save_json(task_id, "mock_harness_state.json", state)
        self._save_json(task_id, "mock_harness_tree.json", tree)
        self._save_json(task_id, "mock_harness_report.json", report)

        previous_node_id = state.get("best_node_id") or "ROOT"
        artifact_index = [dict(item) for item in report.get("artifact_index") or []]
        await _emit(on_event, EventStatus(status="running"))

        for iteration in range(start_iteration, request.max_iterations + 1):
            # The yield is the deterministic Provider safe point for controls.
            if self.iteration_delay:
                await asyncio.sleep(self.iteration_delay)
            await asyncio.sleep(0)
            control_state = self._load_json(task_id, "mock_harness_state.json") or {}
            if str(control_state.get("status") or "").lower() in {"paused", "terminated"}:
                status = str(control_state["status"]).lower()
                await _emit(on_event, EventStatus(status=status))
                return self._result(task_id, status, final_node_id=control_state.get("best_node_id"))

            accepted_score = round(
                0.55 + 0.4 * iteration / max(request.max_iterations, 1),
                4,
            )
            accepted_node_id = f"{task_id}:node:{iteration}:1"
            iteration_refs: list[dict[str, Any]] = []
            iteration_nodes: list[RsiTreeNode] = []
            for candidate in range(1, request.search_width + 1):
                node_id = f"{task_id}:node:{iteration}:{candidate}"
                adopted = candidate == 1
                score = accepted_score if adopted else round(max(0.0, accepted_score - 0.04 * candidate), 4)
                node_type = "adopted" if adopted else ("provisional" if candidate % 3 == 0 else "rejected")
                change = RsiChange(
                    group="prompt",
                    operation="tune",
                    function="prompt",
                    target=f"candidate:{candidate}",
                    summary=f"mock harness candidate {candidate}",
                )
                # Emit a provisional node first so the per-case evaluation
                # stages below have a live node_ref.  The final EventNode at
                # the end of this iteration replaces it with the final score.
                provisional_node = RsiTreeNode(
                    node_id=node_id,
                    iteration=iteration,
                    parent_id=previous_node_id,
                    type="provisional",
                    adopted=False,
                    score=None,
                    summary=f"mock harness candidate {candidate} evaluating",
                    snapshot_artifact_id=None,
                    reason=None,
                    failure_class=None,
                    changes=[change],
                    extra={
                        "candidate": candidate,
                        "search_width": request.search_width,
                        "content": {
                            "kind": "mock_harness_candidate",
                            "status": "provisional",
                            "iteration": iteration,
                            "candidate": candidate,
                            "node_id": node_id,
                            "parent_node_id": previous_node_id,
                        },
                    },
                )
                await _emit(on_event, EventNode(node=provisional_node))
                for case_index in range(1, _MOCK_EVAL_CASES + 1):
                    await _emit(
                        on_event,
                        NodeStageEvent(
                            node_ref=node_id,
                            stage=_mock_case_stage(case_index, _MOCK_EVAL_CASES, "running"),
                        ),
                    )
                    await asyncio.sleep(self.case_delay)
                    await _emit(
                        on_event,
                        NodeStageEvent(
                            node_ref=node_id,
                            stage=_mock_case_stage(case_index, _MOCK_EVAL_CASES, "passed"),
                        ),
                    )
                    await asyncio.sleep(self.case_delay)
                artifact_path = run_dir / "artifacts" / f"harness-{iteration:03d}-{candidate:02d}.zip"
                artifact_id = f"{task_id}:artifact:{iteration}:{candidate}"
                self._write_artifact(
                    artifact_path,
                    request,
                    iteration,
                    candidate=candidate,
                    artifact_id=artifact_id,
                    node_id=node_id,
                    parent_node_id=previous_node_id,
                    node_type=node_type,
                    score=score,
                    baseline=0.55,
                    change=change,
                )
                artifact_ref = ArtifactRef(
                    artifact_id=artifact_id,
                    node_id=node_id,
                    name=artifact_path.name,
                    kind="harness_plugin" if adopted else "harness_candidate",
                    path=str(artifact_path),
                    sha256=_sha256(artifact_path),
                    download_url=None,
                )
                iteration_refs.append(asdict(artifact_ref))
                gain = round((score - 0.55) / 0.55, 4)
                iteration_nodes.append(
                    RsiTreeNode(
                        node_id=node_id,
                        iteration=iteration,
                        parent_id=previous_node_id,
                        type=node_type,
                        adopted=adopted,
                        score=score,
                        summary=(
                            f"mock harness candidate {candidate} accepted"
                            if adopted
                            else f"mock harness candidate {candidate} rejected"
                        ),
                        snapshot_artifact_id=artifact_ref.artifact_id,
                        reason=None if adopted else "deterministic mock candidate gate",
                        failure_class=None if adopted else "MOCK_GATE",
                        changes=[change],
                        extra={
                            "candidate": candidate,
                            "search_width": request.search_width,
                            "model_refs": dict(request.model_refs),
                            "artifact": asdict(artifact_ref),
                            "content": {
                                "kind": "mock_harness_candidate",
                                "status": node_type,
                                "iteration": iteration,
                                "candidate": candidate,
                                "node_id": node_id,
                                "parent_node_id": previous_node_id,
                                "dataset_files": list(request.dataset_files),
                                "dataset_read": False,
                                "model_refs": dict(request.model_refs),
                                "harness_refs_path": request.harness_refs_path,
                                "harness_read": False,
                                "change": asdict(change),
                                "evaluation": {
                                    "score": score,
                                    "baseline": 0.55,
                                    "gain": gain,
                                    "accepted": adopted,
                                },
                                "artifact": asdict(artifact_ref),
                            },
                        },
                    )
                )

            tree["nodes"] = [
                item for item in tree["nodes"] if item.get("iteration") != iteration
            ]
            tree["nodes"].extend(asdict(node) for node in iteration_nodes)
            tree["iteration"] = iteration
            tree["depth"] = max(int(tree.get("depth", 0) or 0), iteration)
            artifact_index = [item for item in artifact_index if item.get("iteration") != iteration]
            artifact_index.extend(iteration_refs)
            usage = _usage_for_iteration(iteration)
            state.update(
                {
                    "status": "running",
                    "iteration": iteration,
                    "best_node_id": accepted_node_id,
                    "score": accepted_score,
                    "baseline": 0.55,
                    "usage": asdict(usage),
                }
            )
            report.update(
                {
                    "status": "running",
                    "best_node_id": accepted_node_id,
                    "usage": state["usage"],
                    "artifact_index": artifact_index,
                    "summary": "mock harness optimization is running",
                }
            )
            self._save_json(task_id, "mock_harness_state.json", state)
            self._save_json(task_id, "mock_harness_tree.json", tree)
            self._save_json(task_id, "mock_harness_report.json", report)
            for node in iteration_nodes:
                await _emit(on_event, EventNode(node=node))
            await _emit(
                on_event,
                EventProgress(
                    iteration=iteration,
                    total_iterations=request.max_iterations,
                    score=accepted_score,
                    baseline=state["baseline"],
                    usage=usage,
                ),
            )
            previous_node_id = accepted_node_id

        await asyncio.sleep(0)
        control_state = self._load_json(task_id, "mock_harness_state.json") or state
        if str(control_state.get("status") or "").lower() in {"paused", "terminated"}:
            status = str(control_state["status"]).lower()
            await _emit(on_event, EventStatus(status=status))
            return self._result(task_id, status, final_node_id=control_state.get("best_node_id"))
        state["status"] = "completed"
        report["status"] = "completed"
        report["summary"] = "mock harness optimization completed"
        self._save_json(task_id, "mock_harness_state.json", state)
        self._save_json(task_id, "mock_harness_report.json", report)
        await _emit(on_event, EventStatus(status="completed"))
        return self._result(task_id, "completed", final_node_id=state.get("best_node_id"))

    def _write_artifact(
        self,
        target: Path,
        request: HarnessEngineRequest,
        iteration: int,
        *,
        candidate: int,
        artifact_id: str,
        node_id: str,
        parent_node_id: str,
        node_type: str,
        score: float,
        baseline: float,
        change: RsiChange,
    ) -> None:
        """Write a small downloadable snapshot without reading user files."""
        target.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "provider": "MockHarnessProvider",
            "mock": True,
            "artifact_id": artifact_id,
            "task_id": request.task_id,
            "iteration": iteration,
            "candidate": candidate,
            "node_id": node_id,
            "parent_node_id": parent_node_id,
            "node_type": node_type,
            "score": score,
            "baseline": baseline,
            "dataset_files": list(request.dataset_files),
            "model_refs": dict(request.model_refs),
            "harness_refs": {
                "path": request.harness_refs_path,
                "read": False,
                "materialization": "metadata-only",
            },
            "change": asdict(change),
        }
        node_detail = {
            "node_id": node_id,
            "parent_id": parent_node_id,
            "iteration": iteration,
            "candidate": candidate,
            "type": node_type,
            "adopted": node_type == "adopted",
            "score": score,
            "summary": f"mock harness candidate {candidate}",
            "snapshot_artifact_id": artifact_id,
            "changes": [asdict(change)],
        }
        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(
                "README.md",
                (
                    "This is a deterministic mock RSI harness snapshot.\n"
                    "The active harness and dataset were intentionally not read or mutated.\n"
                    f"Iteration: {iteration}; candidate: {candidate}\n"
                ),
            )
            archive.writestr(
                "mock-optimization.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            archive.writestr("node.json", json.dumps(node_detail, ensure_ascii=False, indent=2))
            archive.writestr(
                f"changes/iteration-{iteration:03d}-candidate-{candidate:02d}.diff",
                f"# mock harness candidate {candidate} change\n{change.summary}\n",
            )

    def _update_report_status(self, task_id: str, status: str, best_node_id: str | None) -> None:
        report = self._load_json(task_id, "mock_harness_report.json") or {
            "task_id": task_id,
            "best_node_id": best_node_id,
            "usage": None,
            "artifact_index": [],
            "summary": None,
        }
        report["status"] = status
        report["best_node_id"] = best_node_id
        self._save_json(task_id, "mock_harness_report.json", report)

    @staticmethod
    def _new_state(task_id: str, total_iterations: int) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "status": "created",
            "iteration": 0,
            "total_iterations": total_iterations,
            "best_node_id": None,
            "score": None,
            "baseline": 0.55,
            "usage": None,
            "updated_at": "",
            "error_code": None,
            "error_message": None,
        }

    def _run_dir(self, task_id: str) -> Path:
        return self.tasks_root / task_id / "run"

    def _load_json(self, task_id: str, filename: str) -> dict[str, Any] | None:
        path = self._run_dir(task_id) / filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _save_json(self, task_id: str, filename: str, data: dict[str, Any]) -> None:
        path = self._run_dir(task_id) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    @staticmethod
    def _root_node() -> RsiTreeNode:
        return RsiTreeNode(
            node_id="ROOT",
            iteration=0,
            parent_id=None,
            type="root",
            adopted=True,
            score=0.55,
            summary="harness optimization baseline",
            snapshot_artifact_id=None,
            reason=None,
            failure_class=None,
            changes=[],
            extra={
                "logical_kind": "root",
                "content": {
                    "kind": "mock_harness_baseline",
                    "status": "baseline",
                    "node_id": "ROOT",
                    "iteration": 0,
                    "description": "Initial harness state before mock candidate search",
                },
            },
        )

    @staticmethod
    def _result(
        task_id: str,
        status: str,
        *,
        final_node_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> EngineResult:
        return EngineResult(
            task_id=task_id,
            status=status,
            final_node_id=final_node_id,
            error_code=error_code,
            error_message=error_message,
        )


def build_mock_harness_adapter(tasks_root: str | Path) -> Any:
    """Build the mock Harness adapter at the same seam as the real Provider."""
    from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineAdapter

    return HarnessEngineAdapter(MockHarnessProvider(tasks_root))


async def _emit(on_event: Any, event: Any) -> None:
    if on_event is not None:
        await on_event(event)


def _usage_for_iteration(iteration: int) -> RsiUsage:
    return RsiUsage(
        tokens=RsiUsageTokens(
            input=120 * iteration,
            output=60 * iteration,
            cache_hit=15 * max(iteration - 1, 0),
        ),
        cost_estimate=0.0,
        call_count=iteration,
    )


def _usage_from_dict(raw: Any) -> RsiUsage | None:
    if not isinstance(raw, dict):
        return None
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
    return RsiUsage(
        tokens=RsiUsageTokens(
            input=int(tokens.get("input", 0) or 0),
            output=int(tokens.get("output", 0) or 0),
            cache_hit=int(tokens.get("cache_hit", 0) or 0),
        ),
        cost_estimate=float(raw.get("cost_estimate", 0.0) or 0.0),
        call_count=int(raw.get("call_count", 0) or 0),
    )


def _node_from_dict(raw: Any) -> RsiTreeNode:
    data = raw if isinstance(raw, dict) else {}
    changes = [RsiChange(**item) for item in data.get("changes") or [] if isinstance(item, dict)]
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
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["MockHarnessProvider", "build_mock_harness_adapter"]
