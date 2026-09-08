"""生产 HarnessProvider：包装 agent-core 单 Harness 迭代优化编排器。

对应设计文档 ``rsi_zequ_jiuwenswarm_harness_completion_v1.md`` §2.1：

- ``run/resume`` → ``SingleHarnessIterativeOptimizationOrchestrator.run``，
  并透传 ``on_event``（引擎在持久化后发 ``EventStatus/EventProgress/EventNode``）。
- ``read_state/read_report`` → 解析引擎落盘的 ``single_harness_state.yaml`` /
  ``single_harness_report.yaml``，归一化为服务侧 ``EngineState`` / ``EngineReport``。
- ``get_tree`` → 复用引擎的 Epoch 节点投影；历史候选格式仅用于兼容旧状态。
- ``pause/terminate`` → 引擎不支持中途取消，如实抛 ``RsiNotReady``。
- ``validate_input`` → 引擎 ``load_cases`` 真校验（case 形状/case_id 空或重复）。
"""  # noqa: W291

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from openjiuwen.rsi import (
    AutoCoordinatingHarnessConfig,
    IterativeSingleHarnessRequest,
    SingleHarnessIterativeOptimizationOrchestrator,
)
from openjiuwen.rsi.harness_rsi.single_harness.events_translate import (
    active_epoch_node_event,
    epoch_node_event,
    root_node_event,
)
from openjiuwen.rsi.schema import (
    ArtifactRef,
    EngineReport,
    EngineResult,
    EngineState,
    RsiTreeNode,
    TreeResponse,
)

from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiBadRequest,
    RsiDatasetInvalid,
    RsiNotReady,
    RsiPathInvalid,
    RsiPathNotAllowed,
    RsiResumeInputChanged,
    RsiResumeMismatch,
)
from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineRequest
from jiuwenswarm.agents.harness.common.rsi.validation_dataset import (
    normalize_validation_suite,
)

logger = logging.getLogger(__name__)

_STATE_FILE = "single_harness_state.yaml"
_REPORT_FILE = "single_harness_report.yaml"
_RUN_DIR = "run"

_DEFAULT_CONFIG_PATH = Path("config") / "harness_orchestrator.yaml"


def engine_validate_input(dataset_path: str | None) -> Any:
    """引擎 load_cases 真校验（web §5.2 / 设计 §2.2 的共享入口）。"""
    if not dataset_path:
        return {
            "valid": False,
            "sample_count": None,
            "errors": [{"code": "DATASET_REQUIRED", "message": "input_file is required"}],
        }
    path = Path(str(dataset_path)).expanduser()
    if not path.is_file():
        return {
            "valid": False,
            "sample_count": None,
            "errors": [{"code": "PATH_INVALID", "message": f"Dataset not found: {path}"}],
        }
    try:
        normalized = normalize_validation_suite(path)
        if normalized is None:
            cases = _engine_load_cases([str(path.resolve())])
        else:
            with tempfile.TemporaryDirectory(prefix="rsi-dataset-") as temp_dir:
                normalized_path = Path(temp_dir) / "cases.json"
                normalized_path.write_text(
                    json.dumps(normalized, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                from openjiuwen.rsi.harness_rsi.data_loader.case_files import (
                    copy_dataset_files,
                )

                copy_dataset_files(normalized["cases"], path.resolve(), normalized_path)
                cases = _engine_load_cases([str(normalized_path)])
        from openjiuwen.rsi.harness_rsi.data_loader.case_files import (
            validate_case_fields,
        )

        for case in cases:
            validate_case_fields(case)
    except RsiDatasetInvalid as exc:
        return {
            "valid": False,
            "sample_count": None,
            "errors": [{"code": "DATASET_INVALID", "message": str(exc)}],
        }
    except FileNotFoundError as exc:
        return {
            "valid": False,
            "sample_count": None,
            "errors": [{"code": "PATH_INVALID", "message": str(exc)}],
        }
    except (ValueError, TypeError) as exc:
        return {
            "valid": False,
            "sample_count": None,
            "errors": [{"code": "DATASET_INVALID", "message": str(exc)}],
        }
    except Exception as exc:  # noqa: BLE001 - 引擎未归类错误统一映射 DATASET_INVALID
        logger.warning("[RSI] harness validate_input 未归类异常: %s", exc)
        return {
            "valid": False,
            "sample_count": None,
            "errors": [{"code": "DATASET_INVALID", "message": str(exc)}],
        }
    return {"valid": True, "sample_count": len(cases), "errors": []}


def _engine_load_cases(dataset_files: list[str]) -> list[dict[str, Any]]:
    """调用引擎数据集校验/加载（新版本公开导出，旧版本回退私有实现）。"""
    try:
        from openjiuwen.rsi import load_cases as _load_cases  # noqa: PLC0415
    except ImportError:  # pragma: no cover - 兼容旧版 openjiuwen
        from openjiuwen.rsi.harness_rsi.single_harness.iterative import (  # noqa: PLC0415
            _load_cases,
        )
    return list(_load_cases(dataset_files))


def _run_accepts_on_event(orchestrator: Any) -> bool:
    try:
        return "on_event" in inspect.signature(orchestrator.run).parameters
    except (TypeError, ValueError):  # pragma: no cover - 签名不可读时保守不透传
        return False


class HarnessProvider:
    """HarnessProviderContract 的生产实现（包装 agent-core 编排器）。

    Args:
        tasks_root: ``.jiuwenswarm/rsi/tasks``，引擎 ``output_dir`` 与状态/报告读取根。
        orchestrator_config: 顶层 ``AutoCoordinatingHarnessConfig`` dict（最小装配入口，
            经 ``from_dict`` 校验）。与 ``orchestrator_config_path`` 二选一。
        orchestrator_config_path: 编排器 YAML 配置路径（缺省引导内置模板）。
            默认 ``<tasks_root>/config/harness_orchestrator.yaml``。
        orchestrator: 预构建编排器（测试注入缝，优先级最高）。
    """

    supports_pause = False
    supports_resume = True

    def __init__(
        self,
        tasks_root: str | Path,
        *,
        orchestrator_config: dict[str, Any] | None = None,
        orchestrator_config_path: str | Path | None = None,
        orchestrator: Any = None,
        model_resolver: Any = None,
    ) -> None:
        self._tasks_root = Path(tasks_root)
        self._orchestrator = orchestrator
        self._orchestrator_config = orchestrator_config
        self._orchestrator_config_path = orchestrator_config_path
        self._model_resolver = model_resolver

    # -- 输入校验（引擎 load_cases 真校验） --

    def validate_input(self, dataset_path: str | None) -> Any:
        return engine_validate_input(dataset_path)

    # -- 执行控制 --

    async def run(self, request: HarnessEngineRequest, *, on_event: Any = None) -> EngineResult:
        return await self._run(request, on_event=on_event, resume=False)

    async def resume(self, request: HarnessEngineRequest, *, on_event: Any = None) -> EngineResult:
        return await self._run(request, on_event=on_event, resume=True)

    async def _run(
        self,
        request: HarnessEngineRequest,
        *,
        on_event: Any,
        resume: bool,
    ) -> EngineResult:
        self._validate_materialized_paths(request)
        self._verify_task_materials(request, resume=resume)
        orchestrator = self._resolve_orchestrator(request)
        # New tasks measure H0 first; resumes retain their original protocol.
        previous_state = self._read_state_dict(request.task_id) if resume else {}
        auto_full_baseline = (
            bool(previous_state.get("fingerprint", {}).get("auto_full_baseline", False))
            if previous_state else True
        )
        engine_request_kwargs: dict[str, Any] = {
            "dataset_files": [str(item) for item in request.dataset_files],
            "harness_refs_path": request.harness_refs_path,
            "output_dir": str(Path(request.output_dir).expanduser()),
            "dataset_id": request.dataset_id or "single_harness_benchmark",
            "resume": resume,
            "auto_full_baseline": auto_full_baseline,
        }
        if "task_id" in inspect.signature(IterativeSingleHarnessRequest).parameters:
            engine_request_kwargs["task_id"] = request.task_id
        engine_request = IterativeSingleHarnessRequest(**engine_request_kwargs)
        kwargs: dict[str, Any] = {}
        if on_event is not None and _run_accepts_on_event(orchestrator):
            kwargs["on_event"] = on_event
        elif on_event is not None:
            logger.warning(
                "[RSI] 引擎编排器不支持 on_event，事件流降级为空: task=%s", request.task_id
            )
        try:
            await orchestrator.run(engine_request, **kwargs)
        except ValueError as exc:
            # 引擎侧 resume fingerprint/task_id 冲突以 ValueError 上抛，映射 4xx 语义。
            raise RsiResumeMismatch(str(exc)) if resume else RsiBadRequest(str(exc)) from exc
        return self._result_from_state(request.task_id)

    async def pause(self, task_id: str) -> EngineResult:
        raise RsiNotReady("harness 引擎暂不支持中途暂停")

    async def terminate(self, task_id: str) -> EngineResult:
        raise RsiNotReady("harness 引擎暂不支持中途终止")

    # -- 状态 / 报告 --

    def read_state(self, task_id: str) -> EngineState:
        state = self._load_yaml(task_id, _STATE_FILE) or {}
        gates = _gates(state)
        status = str(state.get("status") or "created").lower()
        iteration = len(state["epoch_checkpoints"]) if "epoch_checkpoints" in state else len(gates)
        total_iterations = int(state.get("max_iteration") or max(1, iteration))
        baseline = self._baseline_score(task_id, state)
        return EngineState(
            task_id=task_id,
            status=status,
            iteration=iteration,
            total_iterations=total_iterations,
            best_node_id=_best_node_id(state),
            score=_number(state.get("best_score")),
            baseline=baseline,
            usage=None,
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            error_code=None,
            error_message=None,
        )

    def read_publication_state(self, task_id: str) -> dict[str, Any]:
        """Return the raw engine state used by the RSI Harness installer."""

        return self._read_state_dict(task_id)

    def read_report(self, task_id: str) -> EngineReport:
        state = self._load_yaml(task_id, _STATE_FILE) or {}
        report = self._load_yaml(task_id, _REPORT_FILE) or {}
        return EngineReport(
            task_id=task_id,
            status=str(report.get("status") or state.get("status") or "created").lower(),
            best_node_id=_best_node_id(state or report),
            usage=None,
            artifact_index=self._artifact_index(task_id, state or report),
            summary=_report_summary(report or state),
        )

    def get_tree(self, task_id: str) -> TreeResponse:
        state = self._load_yaml(task_id, _STATE_FILE) or {}
        if "epoch_checkpoints" in state:
            events = _epoch_events(state)
            nodes = [event.node for event in events]
            depths = {"h0": 0}
            for node in nodes[1:]:
                depths[node.node_id] = depths.get(node.parent_id, 0) + 1
            return TreeResponse(
                nodes=nodes, depth=max(depths.values()), iteration=len(state["epoch_checkpoints"])
            )
        gates = _gates(state)
        baseline = self._baseline_score(task_id, state)
        nodes: list[RsiTreeNode] = [
            RsiTreeNode(
                node_id="ROOT",
                iteration=0,
                parent_id=None,
                type="ROOT",
                adopted=True,
                score=baseline,
                summary="基线",
                snapshot_artifact_id=None,
                reason=None,
                failure_class=None,
                changes=[],
                extra={},
            )
        ]
        refs_to_node: dict[str, str] = {}
        for index, gate in enumerate(gates, start=1):
            node_id = _gate_node_id(gate) or f"G{index:03d}"
            before_refs = str(gate.get("before_harness_refs_path", "") or "")
            candidate_refs = str(gate.get("candidate_harness_refs_path", "") or "")
            if candidate_refs:
                refs_to_node.setdefault(candidate_refs, node_id)
            parent_id = refs_to_node.get(before_refs, "ROOT") if before_refs else "ROOT"
            if parent_id == node_id:
                parent_id = None
            gate_status = str(gate.get("status") or "rejected").lower()
            adopted = bool(gate.get("accepted")) and gate_status == "accepted"
            nodes.append(
                RsiTreeNode(
                    node_id=node_id,
                    iteration=int(gate.get("predicted_rank", 0) or 0) or index,
                    parent_id=parent_id,
                    type=gate_status,
                    adopted=adopted,
                    score=_number(gate.get("candidate_score")),
                    summary=str(gate.get("reason", "") or ""),
                    snapshot_artifact_id=None,
                    reason=str(gate.get("reason", "") or ""),
                    failure_class=None,
                    changes=[],
                    extra={
                        "before_harness_refs_path": before_refs,
                        "candidate_harness_refs_path": candidate_refs,
                    },
                )
            )
        depth = 0
        depths: dict[str | None, int] = {None: 0, "ROOT": 0}
        for node in nodes:
            if node.node_id == "ROOT":
                depths[node.node_id] = 0
                continue
            parent_depth = depths.get(node.parent_id)
            if parent_depth is None:
                # 乱序/孤儿 gate：保守按前序最大深度挂载，避免崩溃。
                parent_depth = max(depths.values()) if depths else 0
                node = RsiTreeNode(**{**_node_dict(node), "parent_id": None})
            node_depth = parent_depth + 1
            depths[node.node_id] = node_depth
            depth = max(depth, node_depth)
        return TreeResponse(
            nodes=nodes,
            depth=depth,
            iteration=len(gates),
        )

    def locate_artifact(self, task_id: str, artifact_id: str | None = None) -> ArtifactRef:
        state = self._load_yaml(task_id, _STATE_FILE) or {}
        index = self._artifact_index(task_id, state)
        wanted = str(artifact_id or "").strip()
        for ref in index:
            if wanted and ref.artifact_id == wanted:
                return ref
        if wanted:
            raise RsiPathInvalid(f"artifact 不存在: {wanted}")
        if index:
            return index[-1]
        published = str(state.get("published_harness_refs_path", "") or "")
        if published:
            return ArtifactRef(
                artifact_id="published",
                node_id=None,
                name="published_harness_refs",
                kind="harness_refs",
                path=published,
                sha256=None,
                download_url=None,
            )
        raise RsiPathInvalid(f"任务 {task_id} 尚无可定位产物")

    # -- 内部 --

    def _resolve_orchestrator(self, request: HarnessEngineRequest) -> Any:
        if self._orchestrator is not None:
            return self._orchestrator
        config = self._build_config(request)
        return SingleHarnessIterativeOptimizationOrchestrator(config)

    def _build_config(self, request: HarnessEngineRequest) -> AutoCoordinatingHarnessConfig:
        config_path = request.orchestrator_config_path or self._orchestrator_config_path
        if config_path:
            from openjiuwen.rsi.harness_rsi.config import (  # noqa: PLC0415
                load_auto_coordinating_harness_config,
            )

            config = load_auto_coordinating_harness_config(str(config_path))
        elif self._orchestrator_config is not None:
            config = AutoCoordinatingHarnessConfig.from_dict(dict(self._orchestrator_config))
        else:
            default_path = Path(self._tasks_root) / _DEFAULT_CONFIG_PATH
            from openjiuwen.rsi.harness_rsi.config import (  # noqa: PLC0415
                load_auto_coordinating_harness_config,
            )

            config = load_auto_coordinating_harness_config(str(default_path))
        # A task profile already contains materialized model file paths.  Do
        # not overwrite them with the public model IDs when the Provider is
        # invoked by the production service.
        if config_path:
            return config
        return self._apply_model_refs(config, request.model_refs)

    @staticmethod
    def _apply_model_refs(
        config: AutoCoordinatingHarnessConfig,
        model_refs: dict[str, Any] | None,
    ) -> AutoCoordinatingHarnessConfig:
        """把任务 model_refs（tester=评估 / optimizer=优化）翻译进 ModelConfigs。"""
        refs = {str(k): str(v).strip() for k, v in (model_refs or {}).items() if str(v or "").strip()}
        if not refs:
            return config
        from dataclasses import replace  # noqa: PLC0415

        from openjiuwen.rsi import ModelConfigs  # noqa: PLC0415

        model_configs = ModelConfigs(
            evaluation=refs.get("tester") or config.model_configs.evaluation,
            analysis=refs.get("optimizer") or config.model_configs.analysis,
            member_optimization=refs.get("optimizer") or config.model_configs.member_optimization,
        )
        return replace(
            config,
            model_configs=model_configs,
            evaluator=replace(config.evaluator, model_config_ref=model_configs.evaluation),
            evaluation_result_analyzer=replace(
                config.evaluation_result_analyzer, model_config_ref=model_configs.analysis
            ),
            member_optimizer=replace(
                config.member_optimizer, model_config_ref=model_configs.member_optimization
            ),
        )

    def _validate_materialized_paths(self, request: HarnessEngineRequest) -> None:
        """Fail closed for task-private paths while retaining mock compatibility."""

        if not request.orchestrator_config_path:
            return
        task_root = (self._tasks_root / request.task_id).expanduser().resolve()
        try:
            task_root.relative_to(self._tasks_root.resolve())
        except ValueError as exc:
            raise RsiPathNotAllowed(f"任务路径超出 RSI 根目录: {task_root}") from exc
        for label, raw_path in (
            ("dataset", None),
            ("harness_refs", request.harness_refs_path),
            ("output", request.output_dir),
            ("profile", request.orchestrator_config_path),
        ):
            if raw_path is None:
                continue
            path = Path(str(raw_path)).expanduser().resolve()
            try:
                path.relative_to(task_root)
            except ValueError as exc:
                raise RsiPathNotAllowed(f"{label} 路径超出任务目录: {path}") from exc
            if label != "output" and not path.is_file():
                raise RsiPathInvalid(f"{label} 文件不存在: {path}")
        for raw_path in request.dataset_files:
            path = Path(str(raw_path)).expanduser().resolve()
            try:
                path.relative_to(task_root)
            except ValueError as exc:
                raise RsiPathNotAllowed(f"dataset 路径超出任务目录: {path}") from exc
            if not path.is_file():
                raise RsiPathInvalid(f"dataset 文件不存在: {path}")

    def _verify_resume_materials(self, request: HarnessEngineRequest) -> None:
        """Compare task-private hashes before delegating resume to openjiuwen."""

        self._verify_task_materials(request, resume=True)

    def _verify_task_materials(
        self,
        request: HarnessEngineRequest,
        *,
        resume: bool,
    ) -> None:
        """Verify task snapshots and the target referenced by ``harness_refs``.

        The wrapper file can remain byte-for-byte identical while its target
        Harness YAML is replaced.  Checking the recorded source hash closes
        that gap both before the first run and on resume; resume maps every
        mismatch to the dedicated public error code.
        """

        task_file = self._tasks_root / request.task_id / "task.json"
        if not task_file.is_file():
            return
        try:
            import json

            payload = json.loads(task_file.read_text(encoding="utf-8"))
            materials = ((payload.get("config") or {}).get("rsi_materials") or {})
        except (OSError, UnicodeError, ValueError, TypeError):
            return
        if not isinstance(materials, dict):
            return
        expected = {
            "dataset": (((materials.get("input_snapshot") or {}).get("sha256"))),
            "harness": (((materials.get("harness_snapshot") or {}).get("sha256"))),
            "profile": (((materials.get("profile") or {}).get("sha256"))),
        }
        actual_paths = {
            "dataset": request.dataset_files[0] if request.dataset_files else "",
            "harness": request.harness_refs_path,
            "profile": request.orchestrator_config_path or "",
        }
        for role, expected_hash in expected.items():
            expected_hash = str(expected_hash or "").strip()
            path = Path(actual_paths[role]).expanduser()
            if not expected_hash or not path.is_file():
                self._raise_material_error(resume, f"{role} 材料不可用")
            actual_hash = _sha256(path)
            if actual_hash != expected_hash:
                self._raise_material_error(resume, f"{role} 材料已变化")

        file_hashes = (materials.get("input_snapshot") or {}).get("files", {})
        if file_hashes:
            from openjiuwen.rsi.harness_rsi.data_loader.case_files import (
                resolve_dataset_file,
            )

            dataset_root = Path(actual_paths["dataset"]).resolve().parent
            if not isinstance(file_hashes, dict):
                self._raise_material_error(resume, "dataset file manifest is invalid")
            for relative, expected_hash in file_hashes.items():
                try:
                    path = resolve_dataset_file(dataset_root, relative)
                    matches = _sha256(path) == expected_hash
                except (OSError, ValueError, TypeError):
                    matches = False
                if not matches:
                    self._raise_material_error(resume, f"dataset file changed or missing: {relative}")

        harness_snapshot = materials.get("harness_snapshot")
        if isinstance(harness_snapshot, dict):
            source_hash = str(harness_snapshot.get("source_sha256") or "").strip()
            source_path = Path(str(harness_snapshot.get("source_path") or "")).expanduser()
            if source_hash:
                if (
                    not source_path.is_file()
                    and not source_path.is_dir()
                ) or _path_sha256(source_path) != source_hash:
                    self._raise_material_error(resume, "harness 源配置已变化")
            expected_target = source_path if source_hash else None
            target_hash = str(harness_snapshot.get("target_sha256") or "").strip()
            target_path = Path(
                str(harness_snapshot.get("package_path") or "")).expanduser()
            if target_hash:
                if (
                    not target_path.is_file()
                    and not target_path.is_dir()
                ) or _path_sha256(target_path) != target_hash:
                    self._raise_material_error(resume, "harness 目标配置已变化")
                expected_target = target_path
            self._verify_harness_refs_target(
                Path(actual_paths["harness"]).expanduser(),
                expected_source=expected_target,
                resume=resume,
            )
        model_manifests = materials.get("models") or {}
        if isinstance(model_manifests, dict):
            for role, manifest in model_manifests.items():
                if not isinstance(manifest, dict):
                    continue
                expected_hash = str(manifest.get("config_sha256") or "").strip()
                path = Path(str(manifest.get("path") or "")).expanduser()
                if expected_hash and (not path.is_file() or _sha256(path) != expected_hash):
                    self._raise_material_error(resume, f"model({role}) 材料已变化")

    def _verify_harness_refs_target(
        self,
        refs_path: Path,
        *,
        expected_source: Path | None,
        resume: bool,
    ) -> None:
        try:
            payload = yaml.safe_load(refs_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            self._raise_material_error(resume, f"harness refs 不可读: {refs_path}")
            raise AssertionError("unreachable") from exc
        refs = payload.get("harness_refs") if isinstance(payload, dict) else None
        if not isinstance(refs, dict) or len(refs) != 1:
            self._raise_material_error(resume, f"harness refs 必须恰好包含一条引用: {refs_path}")
        raw_target = Path(str(next(iter(refs.values())) or "")).expanduser()
        if not raw_target.is_absolute():
            raw_target = refs_path.parent / raw_target
        target = raw_target.resolve()
        if not target.is_file() and not target.is_dir():
            self._raise_material_error(resume, f"harness 源配置不存在: {target}")
        if expected_source is not None and target != expected_source.resolve():
            self._raise_material_error(resume, "harness refs 目标与创建快照不一致")

    @staticmethod
    def _raise_material_error(resume: bool, message: str) -> None:
        if resume:
            raise RsiResumeInputChanged(f"resume {message}")
        raise RsiPathNotAllowed(message)

    def _result_from_state(self, task_id: str) -> EngineResult:
        state = self._read_state_dict(task_id)
        status = str(state.get("status") or "completed").lower()
        if status not in {"completed", "failed"}:
            # 引擎 run() 正常返回即本轮执行结束。
            status = "completed"
        return EngineResult(
            task_id=task_id,
            status=status,
            final_node_id=_best_node_id(state),
            error_code=None if status == "completed" else "ENGINE_FAILED",
            error_message=None if status == "completed" else "harness 引擎状态异常结束",
        )

    def _read_state_dict(self, task_id: str) -> dict[str, Any]:
        return self._load_yaml(task_id, _STATE_FILE) or {}

    def _baseline_score(self, task_id: str, state: dict[str, Any]) -> float | None:
        baseline = _number(state.get("baseline_score"))
        if baseline is not None:
            return baseline
        dataset = state.get("dataset")
        expected_cases = dataset.get("cases") if isinstance(dataset, dict) else None
        if expected_cases is None:
            return None
        summary_path = (
            Path(self._tasks_root)
            / task_id
            / _RUN_DIR
            / "evaluations"
            / "e001"
            / "b001"
            / "source"
            / "summary.json"
        )
        if not summary_path.is_file():
            return None
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(summary, dict):
            return None
        if _number(summary.get("total_cases")) != _number(expected_cases):
            return None
        return _number(summary.get("average_score"))

    def _load_yaml(self, task_id: str, name: str) -> dict[str, Any] | None:
        path = Path(self._tasks_root) / task_id / _RUN_DIR / name
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else None

    def _artifact_index(self, task_id: str, state: dict[str, Any]) -> list[ArtifactRef]:
        refs: list[ArtifactRef] = []
        if "epoch_checkpoints" in state:
            for event in _epoch_events(state):
                path = str(event.node.extra.get("artifact_path", "") or "")
                if path:
                    refs.append(ArtifactRef(
                        artifact_id=event.node.node_id, node_id=event.node.node_id,
                        name=Path(path).name, kind="harness_refs", path=path, sha256=None, download_url=None,
                    ))
            return refs
        for index, gate in enumerate(_gates(state), start=1):
            candidate_refs = str(gate.get("candidate_harness_refs_path", "") or "")
            if not candidate_refs:
                continue
            node_id = _gate_node_id(gate) or f"G{index:03d}"
            refs.append(
                ArtifactRef(
                    artifact_id=node_id,
                    node_id=node_id,
                    name=Path(candidate_refs).name,
                    kind="harness_refs",
                    path=candidate_refs,
                    sha256=None,
                    download_url=None,
                )
            )
        return refs


def _epoch_events(state: dict[str, Any]) -> list[Any]:
    events = [root_node_event(state)]
    events.extend(epoch_node_event(state, item) for item in state.get("epoch_checkpoints", []))
    active = active_epoch_node_event(state)
    if active is not None:
        events.append(active)
    return events


def _best_node_id(state: dict[str, Any]) -> str | None:
    if "epoch_checkpoints" not in state:
        return _gate_node_id(_best_gate(_gates(state)))
    best_refs = state.get("best_harness_refs_path")
    for checkpoint in reversed(state["epoch_checkpoints"]):
        if checkpoint.get("promotion_applied") and checkpoint.get("selected_harness_refs_path") == best_refs:
            return f"epoch-{int(checkpoint['epoch']):03d}"
    return "h0"


def _gates(raw: dict[str, Any]) -> list[dict[str, Any]]:
    gates = raw.get("candidate_gates") if isinstance(raw, dict) else None
    if not isinstance(gates, list):
        return []
    return [gate for gate in gates if isinstance(gate, dict)]


def _best_gate(gates: list[dict[str, Any]]) -> dict[str, Any] | None:
    accepted = [
        gate for gate in gates if gate.get("accepted") and str(gate.get("status") or "") == "accepted"
    ]
    if not accepted:
        return None
    return max(
        accepted,
        key=lambda gate: (_number(gate.get("candidate_score")) or float("-inf")),
    )


def _gate_node_id(gate: dict[str, Any] | None) -> str | None:
    if not gate:
        return None
    candidate_id = str(gate.get("candidate_id", "") or "").strip()
    return candidate_id or None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _report_summary(report: dict[str, Any]) -> str:
    return (
        f"candidate_count={len(_gates(report))} "
        f"best_score={report.get('best_score')} "
        f"baseline_score={report.get('baseline_score')}"
    )


def _node_dict(node: RsiTreeNode) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "iteration": node.iteration,
        "parent_id": node.parent_id,
        "type": node.type,
        "adopted": node.adopted,
        "score": node.score,
        "summary": node.summary,
        "snapshot_artifact_id": node.snapshot_artifact_id,
        "reason": node.reason,
        "failure_class": node.failure_class,
        "changes": node.changes,
        "extra": node.extra,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_sha256(path: Path) -> str:
    """Hash the file or package directory referenced by Harness refs."""

    if path.is_file():
        return _sha256(path)
    digest = hashlib.sha256()
    for item in sorted(
        (candidate for candidate in path.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(path).as_posix(),
    ):
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_sha256(item)))
    return digest.hexdigest()


__all__ = ["HarnessProvider", "engine_validate_input"]
