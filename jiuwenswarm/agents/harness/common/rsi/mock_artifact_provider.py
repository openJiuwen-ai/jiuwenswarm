"""Small in-process artifact Providers for service-layer integration.

The real ScienceDiscovery and autoResearch implementations are owned by
other repositories.  These Providers intentionally exercise only the
AgentServer contract: durable state/report/tree/artifact snapshots and the
four public event shapes.  Replacing them later must not require changes to
the common RSI service or Web/Gateway routing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openjiuwen.rsi.artifact_rsi.request import ArtifactEngineRequest
from openjiuwen.rsi.events import EventNode, EventProgress, EventStatus, OnEvent
from openjiuwen.rsi.schema import (
    ArtifactRef,
    ArtifactValidationResult,
    EngineReport,
    EngineResult,
    EngineState,
    RsiChange,
    RsiUsage,
    RsiUsageTokens,
    RsiTreeNode,
    TreeResponse,
)


class MockArtifactProvider:
    """Deterministic Provider used to close the AgentServer service loop."""

    supports_pause = True

    def __init__(
        self,
        tasks_root: str | Path,
        artifact_type: str,
        *,
        iteration_delay: float = 0.1,
        node_delay: float = 30.0,
        branching_factor: int = 3,
    ) -> None:
        normalized = str(artifact_type or "").strip().lower()
        if normalized not in {"program", "paper"}:
            raise ValueError(f"unsupported mock artifact type: {artifact_type}")
        self.artifact_type = normalized
        self.tasks_root = Path(tasks_root)
        # Keep one visible checkpoint between iterations so the Web UI can
        # observe the mock run without making the local E2E test slow.
        self.iteration_delay = max(0.0, float(iteration_delay))
        # The interactive mock intentionally pauses before materializing every
        # candidate artifact.  Tests can inject 0 to stay fast.
        self.node_delay = max(0.0, float(node_delay))
        self.branching_factor = max(1, int(branching_factor))

    # -- public Provider contract -----------------------------------------

    def validate_input(self, artifact_path: str | None) -> ArtifactValidationResult:
        if self.artifact_type == "paper" and not artifact_path:
            # Paper optimization may be instruction-only; the AgentServer
            # validates that the instruction exists on task creation.
            return ArtifactValidationResult(valid=True, errors=[])
        if not artifact_path:
            return ArtifactValidationResult(
                valid=False,
                errors=[{"code": "ARTIFACT_PATH_REQUIRED", "message": "artifact_path is required"}],
            )
        path = Path(artifact_path).expanduser()
        if not path.exists():
            return ArtifactValidationResult(
                valid=False,
                errors=[{"code": "PATH_INVALID", "message": f"artifact path does not exist: {path}"}],
            )
        if self.artifact_type == "paper" and not path.is_file() and not path.is_dir():
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

    async def run(self, request: ArtifactEngineRequest, on_event: OnEvent | None = None) -> EngineResult:
        return await self._run(request, on_event=on_event, start_iteration=1, create_root=True)

    async def pause(self, task_id: str, on_event: OnEvent | None = None) -> EngineResult:
        state = self._load_state(task_id)
        if state is None:
            return self._result(task_id, "failed", error_code="TASK_NOT_FOUND", error_message="task snapshot missing")
        if state.get("status") not in {"running", "created"}:
            return self._result(task_id, str(state.get("status") or "created"), final_node_id=state.get("best_node_id"))
        state["status"] = "paused"
        self._save_state(task_id, state)
        report = self._load_json(task_id, "mock_artifact_report.json")
        if report is not None:
            report["status"] = "paused"
            self._save_json(task_id, "mock_artifact_report.json", report)
        await _emit(on_event, EventStatus(status="paused"))
        return self._result(task_id, "paused", final_node_id=state.get("best_node_id"))

    async def resume(self, request: ArtifactEngineRequest, on_event: OnEvent | None = None) -> EngineResult:
        if self.artifact_type == "paper":
            return self._unsupported(request.task_id, "paper artifact optimization does not support resume")
        state = self._load_state(request.task_id)
        if state is None:
            # A queued task can be paused before the Provider has created its
            # first checkpoint.  Resuming that task is equivalent to a fresh
            # run and still keeps the same AgentServer task identity.
            return await self._run(
                request,
                on_event=on_event,
                start_iteration=1,
                create_root=True,
            )
        last_iteration = int(state.get("iteration", 0) or 0)
        if not bool(state.get("iteration_complete", True)) and last_iteration > 0:
            # A pause/restart can happen after one branch artifact has been
            # persisted but before the whole iteration finishes.  Re-enter
            # that iteration; _run skips the already durable candidates.
            start_iteration = last_iteration
        else:
            start_iteration = max(1, last_iteration + 1)
        if start_iteration > request.max_iterations:
            state["status"] = "completed"
            self._save_state(request.task_id, state)
            report = self._load_json(request.task_id, "mock_artifact_report.json")
            if report is not None:
                report["status"] = "completed"
                self._save_json(request.task_id, "mock_artifact_report.json", report)
            return self._result(request.task_id, "completed", final_node_id=state.get("best_node_id"))
        return await self._run(
            request,
            on_event=on_event,
            start_iteration=start_iteration,
            create_root=False,
        )

    def read_state(self, task_id: str) -> EngineState:
        state = self._load_state(task_id)
        if state is None:
            raise FileNotFoundError(f"mock artifact state not found: {task_id}")
        usage = _usage_from_dict(state.get("usage"))
        return EngineState(
            task_id=task_id,
            status=str(state.get("status") or "created"),
            iteration=int(state.get("iteration", 0) or 0),
            total_iterations=int(state.get("total_iterations", 0) or 0),
            best_node_id=state.get("best_node_id"),
            score=state.get("score"),
            baseline=state.get("baseline"),
            usage=usage,
            updated_at=str(state.get("updated_at") or ""),
            error_code=state.get("error_code"),
            error_message=state.get("error_message"),
        )

    def read_report(self, task_id: str) -> EngineReport:
        report = self._load_json(task_id, "mock_artifact_report.json")
        if report is None:
            raise FileNotFoundError(f"mock artifact report not found: {task_id}")
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
        tree = self._load_json(task_id, "mock_artifact_tree.json")
        if tree is None:
            raise FileNotFoundError(f"mock artifact tree not found: {task_id}")
        nodes = [_node_from_dict(item) for item in tree.get("nodes") or []]
        return TreeResponse(
            nodes=nodes,
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
        raise FileNotFoundError(f"mock artifact not found: {task_id}/{artifact_id or '<best>'}")

    async def terminate(self, task_id: str, on_event: OnEvent | None = None) -> EngineResult:
        state = self._load_state(task_id)
        if state is None:
            # A queued task may be paused before the Provider has created its
            # own durable snapshot; the worker still owns the public task and
            # can safely commit this terminal control result.
            return self._result(task_id, "terminated")
        if state.get("status") in {"completed", "failed", "terminated"}:
            return self._result(task_id, str(state["status"]), final_node_id=state.get("best_node_id"))
        state["status"] = "terminated"
        self._save_state(task_id, state)
        report = self._load_json(task_id, "mock_artifact_report.json") or {
            "task_id": task_id,
            "best_node_id": state.get("best_node_id"),
            "usage": state.get("usage"),
            "artifact_index": [],
            "summary": None,
        }
        report["status"] = "terminated"
        self._save_json(task_id, "mock_artifact_report.json", report)
        await _emit(on_event, EventStatus(status="terminated"))
        return self._result(task_id, "terminated", final_node_id=state.get("best_node_id"))

    # -- mock execution ----------------------------------------------------

    async def _run(
        self,
        request: ArtifactEngineRequest,
        *,
        on_event: OnEvent | None,
        start_iteration: int,
        create_root: bool,
    ) -> EngineResult:
        task_id = request.task_id
        run_dir = Path(request.run_dir).expanduser().resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

        state = self._load_state(task_id) or self._new_state(task_id, request.max_iterations)
        tree = self._load_json(task_id, "mock_artifact_tree.json") or {"nodes": [], "iteration": 0, "depth": 0}
        report = self._load_json(task_id, "mock_artifact_report.json") or {
            "task_id": task_id,
            "status": "created",
            "best_node_id": None,
            "usage": None,
            "artifact_index": [],
            "summary": None,
        }

        state.update(
            {
                "status": "running",
                "total_iterations": request.max_iterations,
                "error_code": None,
                "error_message": None,
            }
        )
        if create_root and not tree["nodes"]:
            root = self._root_node()
            tree["nodes"].append(asdict(root))
            tree["depth"] = 0
            tree["iteration"] = 0
        self._save_state(task_id, state)
        self._save_json(task_id, "mock_artifact_tree.json", tree)
        self._save_json(task_id, "mock_artifact_report.json", report)
        await _emit(on_event, EventStatus(status="running"))

        previous_node_id = state.get("best_node_id") or "ROOT"
        artifact_index = [dict(item) for item in report.get("artifact_index") or []]
        for iteration in range(start_iteration, request.max_iterations + 1):
            # Give a concurrently scheduled AgentServer pause/terminate
            # command a checkpoint at which it can take effect.  The real
            # Provider owns the equivalent safe-point semantics.
            await asyncio.sleep(0)
            if self.iteration_delay:
                await asyncio.sleep(self.iteration_delay)
            control_state = self._load_state(task_id) or {}
            if control_state.get("status") in {"paused", "terminated"}:
                status = str(control_state["status"])
                await _emit(on_event, EventStatus(status=status))
                return self._result(task_id, status, final_node_id=control_state.get("best_node_id"))

            baseline = None if self.artifact_type == "paper" else 0.5
            # Build a stable parent pool from all nodes that already existed
            # before this iteration.  Candidate 1 starts from the current
            # best node; the remaining candidates deliberately fan out from
            # other historical nodes, so a later iteration can continue a
            # different branch instead of forming one linear chain.
            prior_nodes = [
                item
                for item in tree.get("nodes") or []
                if isinstance(item, dict)
                and str(item.get("node_id") or "") != "ROOT"
                and int(item.get("iteration", 0) or 0) < iteration
            ]
            parent_pool: list[dict[str, Any]] = []
            preferred = str(previous_node_id or "").strip()
            if preferred:
                parent_pool.extend(
                    item for item in prior_nodes
                    if str(item.get("node_id") or "") == preferred
                )
            parent_pool.extend(
                item for item in prior_nodes
                if str(item.get("node_id") or "") != preferred
            )
            if not parent_pool:
                parent_pool = [{"node_id": "ROOT", "score": baseline}]

            accepted_candidate = ((iteration - 1) % self.branching_factor) + 1
            accepted_node_id = str(state.get("best_node_id") or "ROOT")
            accepted_score = state.get("score")
            existing_candidates = {
                candidate
                for item in tree.get("nodes") or []
                if (candidate := _node_candidate(item, iteration)) is not None
            }

            for candidate in range(1, self.branching_factor + 1):
                # A resumed partial iteration already has a durable node for
                # this candidate.  Keep it and continue with the missing
                # branches without sleeping or emitting a duplicate.
                if candidate in existing_candidates:
                    continue

                if self.node_delay:
                    await asyncio.sleep(self.node_delay)
                control_state = self._load_state(task_id) or {}
                if control_state.get("status") in {"paused", "terminated"}:
                    status = str(control_state["status"])
                    await _emit(on_event, EventStatus(status=status))
                    return self._result(
                        task_id,
                        status,
                        final_node_id=control_state.get("best_node_id"),
                    )

                parent = parent_pool[(candidate - 1) % len(parent_pool)]
                parent_node_id = str(parent.get("node_id") or "ROOT")
                parent_score = parent.get("score")
                adopted = candidate == accepted_candidate
                node_id = (
                    f"{task_id}:node:{iteration}"
                    if adopted
                    else f"{task_id}:node:{iteration}:candidate:{candidate}"
                )
                artifact_id = (
                    f"{task_id}:artifact:{iteration}"
                    if adopted
                    else f"{task_id}:artifact:{iteration}:candidate:{candidate}"
                )
                artifact_name = (
                    f"{self.artifact_type}-{iteration:03d}"
                    if adopted
                    else f"{self.artifact_type}-{iteration:03d}-candidate-{candidate:02d}"
                )
                if self.artifact_type != "paper":
                    artifact_name += ".zip"
                artifact_path = run_dir / "artifacts" / artifact_name
                accepted_iteration_score = None if self.artifact_type == "paper" else round(
                    0.5 + 0.05 * iteration,
                    4,
                )
                score = (
                    None
                    if accepted_iteration_score is None
                    else round(
                        max(
                            0.0,
                            accepted_iteration_score
                            if adopted
                            else accepted_iteration_score - 0.01 * candidate,
                        ),
                        4,
                    )
                )
                change = RsiChange(
                    group=self.artifact_type,
                    operation="mock",
                    function="branch_candidate",
                    target=str(request.artifact_path or "instruction"),
                    summary=(
                        f"mock {self.artifact_type} branch {candidate} "
                        f"from {parent_node_id}"
                    ),
                )
                node_type = "adopted" if adopted else (
                    "provisional" if candidate % 3 == 0 else "rejected"
                )
                self._write_artifact(
                    artifact_path,
                    request,
                    iteration,
                    candidate=candidate,
                    accepted=adopted,
                    artifact_id=artifact_id,
                    node_id=node_id,
                    parent_node_id=parent_node_id,
                    parent_score=parent_score,
                    node_type=node_type,
                    score=score,
                    baseline=baseline,
                    change=change,
                )
                artifact_ref = ArtifactRef(
                    artifact_id=artifact_id,
                    node_id=node_id,
                    name=artifact_path.name,
                    kind=(
                        f"{self.artifact_type}_snapshot"
                        if adopted
                        else f"{self.artifact_type}_candidate"
                    ),
                    path=str(artifact_path),
                    sha256=_sha256(artifact_path),
                    download_url=None,
                )
                group = self.artifact_type
                gain = (
                    round((score - baseline) / baseline, 4)
                    if score is not None and baseline
                    else None
                )
                node = RsiTreeNode(
                    node_id=node_id,
                    iteration=iteration,
                    parent_id=parent_node_id,
                    type=node_type,
                    adopted=adopted,
                    score=score,
                    summary=(
                        f"mock {self.artifact_type} branch {candidate} "
                        f"from {parent_node_id}"
                    ),
                    snapshot_artifact_id=artifact_id,
                    reason=None if adopted else "deterministic mock branch gate",
                    failure_class=None if adopted else "MOCK_BRANCH_GATE",
                    changes=[change],
                    extra={
                        # Keep one canonical node-level path for the detail
                        # preview.  The nested artifact ref remains the
                        # download contract consumed by the service layer.
                        "artifact_path": str(artifact_path),
                        group: {
                            "logical_kind": node_type,
                            "iteration": iteration,
                            "candidate": candidate,
                            "parent_node_id": parent_node_id,
                            "artifacts": [asdict(artifact_ref)],
                        },
                        "candidate": candidate,
                        "branching_factor": self.branching_factor,
                        "content": {
                            "kind": "mock_artifact_candidate",
                            "status": node_type,
                            "iteration": iteration,
                            "candidate": candidate,
                            "node_id": node_id,
                            "parent_node_id": parent_node_id,
                            "parent_score": parent_score,
                            "source_path": request.artifact_path,
                            "source_read": False,
                            "branch": {
                                "candidate": candidate,
                                "accepted_candidate": accepted_candidate,
                                "selection": "current_best" if adopted else "historical_branch",
                            },
                            "mock_content": {
                                "artifact_type": self.artifact_type,
                                "description": (
                                    f"mock {self.artifact_type} output generated from "
                                    f"{parent_node_id}"
                                ),
                                "instruction": request.optimization_instruction,
                                "source_materialization": "metadata-only",
                            },
                            "change": asdict(change),
                            "evaluation": {
                                "score": score,
                                "baseline": baseline,
                                "gain": gain,
                                "accepted": adopted,
                            },
                            "artifact": asdict(artifact_ref),
                        },
                    },
                )

                # Persist and emit each node immediately after its own
                # artifact is materialized.  This is what makes the 30-second
                # delay visible as incremental updates in the browser.
                tree["nodes"] = [
                    item for item in tree["nodes"]
                    if item.get("node_id") != node_id
                ]
                tree["nodes"].append(asdict(node))
                tree["iteration"] = iteration
                tree["depth"] = max(int(tree.get("depth", 0) or 0), iteration)
                artifact_index = [
                    item for item in artifact_index
                    if item.get("artifact_id") != artifact_id
                ]
                artifact_index.append(asdict(artifact_ref))
                if adopted:
                    accepted_node_id = node_id
                    accepted_score = score
                state.update(
                    {
                        "status": "running",
                        "iteration": iteration,
                        "iteration_complete": False,
                        "candidate": candidate,
                        "best_node_id": accepted_node_id,
                        "score": accepted_score,
                        "baseline": baseline,
                        "usage": asdict(_usage_for_iteration(iteration)),
                    }
                )
                report.update(
                    {
                        "status": "running",
                        "best_node_id": accepted_node_id,
                        "usage": state["usage"],
                        "artifact_index": artifact_index,
                        "summary": f"mock {self.artifact_type} optimization is running",
                    }
                )
                self._save_state(task_id, state)
                self._save_json(task_id, "mock_artifact_tree.json", tree)
                self._save_json(task_id, "mock_artifact_report.json", report)
                await _emit(on_event, EventNode(node=node))

            usage = _usage_for_iteration(iteration)
            state.update(
                {
                    "status": "running",
                    "iteration": iteration,
                    "iteration_complete": True,
                    "candidate": self.branching_factor,
                    "best_node_id": accepted_node_id,
                    "score": accepted_score,
                    "baseline": baseline,
                    "usage": asdict(usage),
                }
            )
            report.update(
                {
                    "status": "running",
                    "best_node_id": accepted_node_id,
                    "usage": state["usage"],
                    "artifact_index": artifact_index,
                    "summary": f"mock {self.artifact_type} optimization is running",
                }
            )
            self._save_state(task_id, state)
            self._save_json(task_id, "mock_artifact_tree.json", tree)
            self._save_json(task_id, "mock_artifact_report.json", report)
            await _emit(
                on_event,
                EventProgress(
                    iteration=iteration,
                    total_iterations=request.max_iterations,
                    score=accepted_score,
                    baseline=baseline,
                    usage=usage,
                ),
            )
            previous_node_id = accepted_node_id

        await asyncio.sleep(0)
        control_state = self._load_state(task_id) or state
        if control_state.get("status") in {"paused", "terminated"}:
            status = str(control_state["status"])
            await _emit(on_event, EventStatus(status=status))
            return self._result(task_id, status, final_node_id=control_state.get("best_node_id"))
        state["status"] = "completed"
        report["status"] = "completed"
        report["summary"] = f"mock {self.artifact_type} optimization completed"
        self._save_state(task_id, state)
        self._save_json(task_id, "mock_artifact_report.json", report)
        await _emit(on_event, EventStatus(status="completed"))
        return self._result(task_id, "completed", final_node_id=state.get("best_node_id"))

    # -- durable snapshots -------------------------------------------------

    def _root_node(self) -> RsiTreeNode:
        return RsiTreeNode(
            node_id="ROOT",
            iteration=0,
            parent_id=None,
            type="root",
            adopted=True,
            score=None,
            summary="artifact optimization baseline",
            snapshot_artifact_id=None,
            reason=None,
            failure_class=None,
            changes=[],
            extra={
                self.artifact_type: {"logical_kind": "root", "artifacts": []},
                "content": {
                    "kind": "mock_artifact_baseline",
                    "status": "baseline",
                    "node_id": "ROOT",
                    "iteration": 0,
                    "description": "Initial artifact state before mock optimization",
                },
            },
        )

    @staticmethod
    def _new_state(task_id: str, total_iterations: int) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "status": "created",
            "iteration": 0,
            "iteration_complete": True,
            "candidate": 0,
            "total_iterations": total_iterations,
            "best_node_id": None,
            "score": None,
            "baseline": None,
            "usage": None,
            "updated_at": "",
            "error_code": None,
            "error_message": None,
        }

    def _run_dir(self, task_id: str) -> Path:
        return self.tasks_root / task_id / "run"

    def _load_state(self, task_id: str) -> dict[str, Any] | None:
        return self._load_json(task_id, "mock_artifact_state.json")

    def _load_json(self, task_id: str, filename: str) -> dict[str, Any] | None:
        path = self._run_dir(task_id) / filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _save_state(self, task_id: str, state: dict[str, Any]) -> None:
        from datetime import datetime, timezone

        state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._save_json(task_id, "mock_artifact_state.json", state)

    def _save_json(self, task_id: str, filename: str, data: dict[str, Any]) -> None:
        path = self._run_dir(task_id) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def _write_artifact(
        self,
        target: Path,
        request: ArtifactEngineRequest,
        iteration: int,
        *,
        candidate: int,
        accepted: bool,
        artifact_id: str,
        node_id: str,
        parent_node_id: str,
        parent_score: float | None,
        node_type: str,
        score: float | None,
        baseline: float | None,
        change: RsiChange,
    ) -> None:
        """Write a small, self-describing mock artifact.

        A mock run must never copy or hash the user's source tree.  The real
        Provider owns source materialization; this Provider only creates a
        deterministic artifact directory (or a program ZIP) that exercises
        the complete service/Gateway/preview path.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "provider": "MockArtifactProvider",
            "mock": True,
            "artifact_id": artifact_id,
            "artifact_type": self.artifact_type,
            "task_id": request.task_id,
            "iteration": iteration,
            "candidate": candidate,
            "accepted": accepted,
            "node_id": node_id,
            "parent_node_id": parent_node_id,
            "parent_score": parent_score,
            "node_type": node_type,
            "score": score,
            "baseline": baseline,
            "source": {
                "path": request.artifact_path,
                "read": False,
                "materialization": "metadata-only",
            },
            "change": asdict(change),
            "mock_content": {
                "description": (
                    f"mock {self.artifact_type} output generated from "
                    f"{parent_node_id}"
                ),
                "instruction": request.optimization_instruction,
                "source_materialization": "metadata-only",
            },
        }
        node_detail = {
            "node_id": node_id,
            "parent_id": parent_node_id,
            "iteration": iteration,
            "candidate": candidate,
            "type": node_type,
            "adopted": accepted,
            "score": score,
            "summary": (
                f"mock {self.artifact_type} branch {candidate} "
                f"from {parent_node_id}"
            ),
            "snapshot_artifact_id": artifact_id,
            "changes": [asdict(change)],
        }
        if self.artifact_type == "paper":
            if target.exists() or target.is_symlink():
                _remove_path(target)
            target.mkdir(parents=True, exist_ok=True)
            (target / "README.md").write_text(
                (
                    "This is a deterministic mock RSI artifact directory.\n"
                    "The source artifact was intentionally not copied.\n"
                    f"Iteration: {iteration}\n"
                ),
                encoding="utf-8",
            )
            (target / "mock-optimization.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (target / "node.json").write_text(
                json.dumps(node_detail, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            diff_path = target / "changes" / f"iteration-{iteration:03d}.diff"
            diff_path.parent.mkdir(parents=True, exist_ok=True)
            diff_path.write_text(
                (
                    f"# mock {self.artifact_type} branch {candidate} change\n"
                    f"parent: {parent_node_id}\n{change.summary}\n"
                ),
                encoding="utf-8",
            )
            return

        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(
                "README.md",
                (
                    "This is a deterministic mock RSI artifact.\n"
                    "The source artifact was intentionally not copied.\n"
                    f"Iteration: {iteration}\n"
                ),
            )
            archive.writestr(
                "mock-optimization.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            archive.writestr("node.json", json.dumps(node_detail, ensure_ascii=False, indent=2))
            archive.writestr(
                f"changes/iteration-{iteration:03d}.diff",
                (
                    f"# mock {self.artifact_type} branch {candidate} change\n"
                    f"parent: {parent_node_id}\n{change.summary}\n"
                ),
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

    def _unsupported(self, task_id: str, message: str) -> EngineResult:
        return self._result(
            task_id,
            "created",
            error_code="SCENARIO_NOT_SUPPORTED",
            error_message=message,
        )


def build_mock_artifact_adapters(
    tasks_root: str | Path,
    *,
    model_resolver: Any = None,
    requires_model: bool = False,
    iteration_delay: float = 0.1,
    node_delay: float = 30.0,
    branching_factor: int = 3,
) -> dict[str, Any]:
    """Build the replaceable program/paper adapter pair for AgentServer.

    The 30-second node delay is the interactive default.  Service-layer tests
    pass ``node_delay=0`` so they remain fast and deterministic.
    """

    from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import ArtifactEngineAdapter

    return {
        "ARTIFACT:PROGRAM": ArtifactEngineAdapter(
            "PROGRAM",
            MockArtifactProvider(
                tasks_root,
                "program",
                iteration_delay=iteration_delay,
                node_delay=node_delay,
                branching_factor=branching_factor,
            ),
            model_resolver=model_resolver,
            requires_model=requires_model,
        ),
        "ARTIFACT:PAPER": ArtifactEngineAdapter(
            "PAPER",
            MockArtifactProvider(
                tasks_root,
                "paper",
                iteration_delay=iteration_delay,
                node_delay=node_delay,
                branching_factor=branching_factor,
            ),
            model_resolver=model_resolver,
            requires_model=requires_model,
        ),
    }


async def _emit(on_event: OnEvent | None, event: Any) -> None:
    if on_event is not None:
        await on_event(event)


def _usage_for_iteration(iteration: int) -> RsiUsage:
    return RsiUsage(
        tokens=RsiUsageTokens(
            input=100 * iteration,
            output=40 * iteration,
            cache_hit=10 * max(iteration - 1, 0),
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


def _node_candidate(raw: Any, iteration: int) -> int | None:
    """Read a candidate number from a durable node without trusting its shape."""

    if not isinstance(raw, dict):
        return None
    try:
        if int(raw.get("iteration", 0) or 0) != iteration:
            return None
    except (TypeError, ValueError):
        return None
    extra = raw.get("extra")
    if not isinstance(extra, dict):
        return None
    candidate = extra.get("candidate")
    if isinstance(candidate, bool):
        return None
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    elif path.is_dir():
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            digest.update(child.relative_to(path).as_posix().encode("utf-8"))
            digest.update(b"\0")
            with child.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


__all__ = ["MockArtifactProvider", "build_mock_artifact_adapters"]
