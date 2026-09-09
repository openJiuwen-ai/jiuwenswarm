"""派生薄封装服务（内部 v3 §4.7）：RsiDatasetService / RsiTaskService / RsiReportService / RsiTreeService。

- 薄封装：委托 store / projector / usage / artifact / adapter。
- 场景校验、错误码映射在服务层（web §3.5 语义）。
- I7/I8/I9 + C1 为**中优先级预留**：接口签名与服务方法已落位，引擎衔接/单价算法不实现。
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, is_dataclass
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jiuwenswarm.agents.harness.common.tools.web_file_download import build_file_download_info
from jiuwenswarm.agents.harness.common.rsi.adapter import validate_scenario
from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import (
    provider_best_artifact,
    provider_report_to_web,
    provider_state_to_progress,
    provider_usage_to_dict,
    validate_provider_artifact_path,
)
from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiArtifactNotFound,
    RsiBadRequest,
    RsiDatasetInvalid,
    RsiInvalidHarness,
    RsiNotReady,
    RsiPathInvalid,
    RsiTaskNotFound,
    RsiTaskStateConflict,
    RsiUnsupportedParameter,
)
from jiuwenswarm.agents.harness.common.rsi.models import (
    ArtifactType,
    RsiDatasetResult,
    RsiTask,
    Scenario,
    TaskStatus,
    generate_task_id,
    utcnow_iso,
)


logger = logging.getLogger(__name__)


def _normalize_web_proxy(value: object) -> str | None:
    """Validate and normalize a task-scoped HTTP(S) web proxy URL.

    The proxy is intentionally kept in the task's private config rather than
    exposed by the public task projection.  SOCKS proxies are not accepted
    here because the web transport is aiohttp-based and the service does not
    bundle a SOCKS connector dependency.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise RsiBadRequest("web_proxy 必须是 http(s) 代理 URL")
    raw = value.strip()
    if not raw:
        return None
    if len(raw) > 512:
        raise RsiBadRequest("web_proxy 至多 512 个字符")
    try:
        parsed = urlparse(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RsiBadRequest("web_proxy URL 无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise RsiBadRequest("web_proxy 仅支持带主机和端口的 http(s) URL")
    if port is None or not 1 <= port <= 65535:
        raise RsiBadRequest("web_proxy 必须包含 1–65535 的端口")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise RsiBadRequest("web_proxy 不应包含路径、查询参数或片段")
    return raw.rstrip("/")


def _remove_uncommitted_task_dir(tasks_root: Path, task_id: str) -> None:
    """Remove only a freshly-created task materialization after create fails."""

    root = Path(tasks_root).expanduser().resolve()
    candidate = (root / str(task_id)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        logger.warning("[RSI] refused cleanup outside tasks root: %s", candidate)
        return
    if candidate.is_dir():
        shutil.rmtree(candidate)


class RsiTaskService:
    """任务命令/查询（I2/I3/I4/I5）。"""

    def __init__(
        self,
        store: Any,
        *,
        adapter: Any = None,
        adapter_resolver: Any = None,
        harness_refs_provider: Any = None,
        harness_materializer: Any = None,
        model_resolver: Any = None,
        harness_activation_store: Any = None,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.adapter_resolver = adapter_resolver
        self.harness_refs_provider = harness_refs_provider
        self.harness_materializer = harness_materializer
        self.model_resolver = model_resolver
        self.harness_activation_store = harness_activation_store

    # -- I2 create --

    def create(self, params: dict[str, Any]) -> dict[str, Any]:
        """``rsi.task.create``（web §6.1 三分支校验）。返回 {task_id, status=CREATED}。"""
        scenario_raw = str(params.get("scenario") or "").strip().upper()
        artifact_type_raw = str(params.get("artifact_type") or "").strip().upper() or None
        scenario, artifact_type = validate_scenario(
            scenario_raw, artifact_type_raw,
            artifact_type_required=(scenario_raw == Scenario.ARTIFACT.value),
        )
        name = str(params.get("name") or "").strip()
        if not name or len(name) > 100:
            raise RsiBadRequest("实验名称必填且 1–100 字符")
        model_refs = params.get("model_refs")
        if not isinstance(model_refs, dict):
            raise RsiBadRequest("model_refs 必填")
        optimizer = str(model_refs.get("optimizer") or "").strip()
        if not optimizer:
            raise RsiBadRequest("model_refs.optimizer 必填")
        if scenario is Scenario.HARNESS:
            tester = str(model_refs.get("tester") or "").strip()
            if not tester:
                raise RsiBadRequest("harness 优化必填 model_refs.tester")
        requested_max_iterations = params.get("max_iterations")
        # search_width is deprecated; retain the engine default.
        search_width = 1
        harness_profile_options = (
            _harness_profile_options(params) if scenario is Scenario.HARNESS else {}
        )
        optimization_instruction = params.get("optimization_instruction")
        if optimization_instruction is not None:
            optimization_instruction = str(optimization_instruction).strip() or None

        web_proxy = _normalize_web_proxy(params.get("web_proxy"))
        if web_proxy and not (scenario is Scenario.ARTIFACT and artifact_type is ArtifactType.PAPER):
            raise RsiBadRequest("web_proxy 仅支持论文优化")

        # 场景字段校验（web §6.1 规则②③）
        input_file: str | None = None
        artifact_path: str | None = None
        if scenario is Scenario.HARNESS:
            input_file = str(
                params.get("input_file") or params.get("dataset_path") or ""
            ).strip()
            if not input_file:
                raise RsiBadRequest("harness 优化必填 input_file（数据集）")
            if params.get("artifact_path"):
                raise RsiBadRequest("harness 分支不接受 artifact_path")
        else:
            artifact_path = str(
                params.get("artifact_path") or params.get("input_file") or ""
            ).strip() or None
            if artifact_type is ArtifactType.PROGRAM and not artifact_path:
                raise RsiBadRequest("PROGRAM 必填 artifact_path")
            if artifact_type is ArtifactType.PAPER:
                input_file = str(params.get("input_file") or "").strip() or None
                if not artifact_path and not optimization_instruction:
                    raise RsiBadRequest("PAPER 至少需要 artifact_path 或 optimization_instruction")

        # 优化指令仅产物·论文可填
        if scenario is Scenario.HARNESS and optimization_instruction:
            raise RsiBadRequest("harness 优化不接受 optimization_instruction")
        if scenario is Scenario.ARTIFACT and artifact_type is ArtifactType.PROGRAM and optimization_instruction:
            raise RsiBadRequest("PROGRAM 优化不接受 optimization_instruction")

        selected_adapter = self._resolve_adapter(scenario, artifact_type)
        if scenario is Scenario.ARTIFACT and selected_adapter is not None:
            _ensure_provider_valid(
                selected_adapter.validate_input(
                    artifact_path,
                    scenario=scenario.value,
                    artifact_type=artifact_type.value if artifact_type else None,
                )
            )

        # Program task bundles own their search budget in ``task.json``.  The
        # browser intentionally does not expose a second iteration control for
        # this branch, so use the bundle value when the public request leaves
        # it unspecified.  An explicit API value remains authoritative.
        if (
            requested_max_iterations is None
            and scenario is Scenario.ARTIFACT
            and artifact_type is ArtifactType.PROGRAM
        ):
            requested_max_iterations = _program_manifest_max_iterations(artifact_path)
        max_iterations = _positive_int(
            requested_max_iterations,
            default=1,
            field="max_iterations",
        )

        task_id = generate_task_id()
        run_dir = str(self.store.tasks_root / task_id / "run")
        harness_refs_path: str | None = None
        materialization = None
        if scenario is Scenario.HARNESS and self._harness_materialization_enabled:
            source_harness = self._resolve_harness_source(params)
            allow_missing_harness = bool(
                getattr(self.harness_materializer, "allow_missing_harness", False)
            )
            if not source_harness and not allow_missing_harness:
                raise RsiInvalidHarness("当前没有可用的活动 Harness 配置")

            validator = None
            if selected_adapter is not None and hasattr(selected_adapter, "validate_input"):
                validator = lambda path: selected_adapter.validate_input(  # noqa: E731
                    path,
                    scenario=Scenario.HARNESS.value,
                    artifact_type=None,
                )
            try:
                materialization = self.harness_materializer.materialize(
                    task_id,
                    input_file,
                    source_harness,
                    model_refs={
                        "optimizer": optimizer,
                        "tester": str(model_refs.get("tester") or ""),
                    },
                    model_resolver=self.model_resolver,
                    domain=harness_profile_options.get("domain"),
                    profile_options=harness_profile_options,
                    validator=validator,
                    max_iterations=max_iterations,
                    search_width=search_width,
                )
            except Exception:
                # Materialization happens before task.json is committed.  Do
                # not leave private model/config files behind on a rejected
                # create request.
                _remove_uncommitted_task_dir(self.store.tasks_root, task_id)
                raise
            input_file = str(materialization.dataset["path"])
            harness_refs_path = (
                str(materialization.harness.get("path") or "").strip() or None
            )
        elif scenario is Scenario.HARNESS and self.harness_refs_provider is not None:
            # Preserve the pre-materialization/mock contract: a lightweight
            # provider may expose the active refs path directly.  Production
            # mode takes the branch above and wraps the source into a task
            # private single-ref file before persisting the task.
            harness_refs_path = self._resolve_harness_source(params)
        config: dict[str, Any] = {
            "harness_refs_path": harness_refs_path,
            "artifact_path": artifact_path,
            "optimization_instruction": optimization_instruction,
            "artifact_type": artifact_type.value if artifact_type else None,
            "results": {},
            # A materialized Harness refs file is an immutable task-private
            # input, not a published/active artifact.  It must not block
            # deleting a terminal task (the task directory is the cleanup
            # boundary for all private model/config files).
            "active_ref_released": materialization is not None,
        }
        if web_proxy:
            # Keep the URL task-private.  The public ``task.get`` projection
            # exposes only a boolean so credentials cannot leak to the UI.
            config["web_proxy"] = web_proxy
        if materialization is not None:
            manifest = materialization.to_manifest()
            config.update(
                {
                    "orchestrator_config_path": materialization.profile["path"],
                    "dataset_id": (
                        materialization.dataset.get("dataset_id")
                        or "single_harness_benchmark"
                    ),
                    "rsi_training_options": harness_profile_options,
                    "rsi_materials": manifest,
                }
            )
        # The browser WebSocket session is transport metadata, but it must
        # survive task creation so asynchronous Provider events can be routed
        # back to the page that created the task.
        rsi_session_id = str(
            params.get("_rsi_session_id") or params.get("session_id") or ""
        ).strip()
        if rsi_session_id:
            config["rsi_session_id"] = rsi_session_id
        task = RsiTask(
            task_id=task_id,
            name=name,
            scenario=scenario.value,
            artifact_type=artifact_type.value if artifact_type else None,
            input_file=input_file,
            model_refs={
                "optimizer": optimizer,
                "tester": str(model_refs.get("tester") or "") or None,
            },
            max_iterations=max_iterations,
            search_width=search_width,
            optimization_instruction=optimization_instruction,
            artifact_path=artifact_path,
            config=config,
            run_dir=run_dir,
            status=TaskStatus.CREATED.value,
            created_at=utcnow_iso(),
        )
        # run_dir 建好（引擎产出落点）
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        self.store.create(task)
        # 进度树根注册（拉取 I4/tree + 推送 P2/P3 同源；基线缺省 None）
        return {"task_id": task_id, "status": TaskStatus.CREATED.value}

    @property
    def _harness_materialization_enabled(self) -> bool:
        return self.harness_materializer is not None and self.model_resolver is not None

    def _resolve_harness_source(self, params: dict[str, Any]) -> str | None:
        """Resolve a trusted active Harness source for a new task.

        ``harness_path`` is accepted only as an AgentServer-side escape hatch
        for controlled callers/tests; the normal browser path uses the
        composition-root provider and never exposes this field.
        """

        explicit = str(params.get("harness_path") or "").strip()
        if explicit:
            return explicit
        provider = self.harness_refs_provider
        if provider is None:
            return None
        try:
            value = provider(params)
        except TypeError:
            value = provider()
        if isinstance(value, Mapping):
            value = value.get("config_path") or value.get("harness_path")
        return str(value or "").strip() or None

    def _resolve_adapter(self, scenario: Scenario | None, artifact_type: ArtifactType | None) -> Any:
        if self.adapter_resolver is not None:
            return self.adapter_resolver(
                scenario.value if scenario is not None else None,
                artifact_type.value if artifact_type is not None else None,
            )
        return self.adapter

    # -- I3 list --

    def list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """``rsi.task.list``（web §6.2 六字段投影 + 可选过滤）。"""
        scenario_raw = params.get("scenario")
        artifact_type_raw = params.get("artifact_type")
        scenario: Scenario | None = None
        artifact_type: ArtifactType | None = None
        if scenario_raw:
            scenario_value = str(scenario_raw).strip().upper()
            artifact_value = str(artifact_type_raw).strip().upper() if artifact_type_raw else None
            scenario, artifact_type = validate_scenario(
                scenario_value, artifact_value,
                artifact_type_required=False,
            )
            if scenario is Scenario.HARNESS and artifact_type is not None:
                raise RsiBadRequest("harness 过滤不接受 artifact_type")
        elif artifact_type_raw:
            try:
                artifact_type = ArtifactType(str(artifact_type_raw).strip().upper())
            except ValueError as exc:
                raise RsiBadRequest(f"artifact_type 非法: {artifact_type_raw}") from exc
        tasks = self.store.list(
            scenario=scenario.value if scenario else None,
            artifact_type=artifact_type.value if artifact_type else None,
        )
        return [task.list_projection() for task in tasks]

    # -- I4 get --

    def get(
        self,
        params: dict[str, Any],
        *,
        projector: Any,
        usage_recorder: Any,
        artifact_service: Any,
        adapter: Any = None,
    ) -> dict[str, Any]:
        """``rsi.task.get``（web §6.3：config + progress + best_artifact + usage）。"""
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")
        task = self.store.get(task_id)
        if adapter is None and self.adapter_resolver is not None:
            adapter = self.adapter_resolver(task.scenario, task.artifact_type)
        if adapter is not None:
            state = _read_provider_snapshot(adapter, "read_state", task_id)
            report = _read_provider_snapshot(adapter, "read_report", task_id)
            if state is not None or report is not None:
                progress = provider_state_to_progress(state) if state is not None else None
                usage = (
                    provider_usage_to_dict(getattr(state, "usage", None))
                    if state is not None
                    else None
                )
                if usage is None and report is not None:
                    usage = provider_usage_to_dict(getattr(report, "usage", None))
                best_artifact = provider_best_artifact(report) if report is not None else None
                if best_artifact is None:
                    best_artifact = artifact_service.best_artifact(task_id)
                return task.get_projection(
                    progress=progress,
                    usage=usage,
                    best_artifact=best_artifact,
                )
            # A task can be CREATED before the Provider has made its first
            # snapshot; retain the normal service-side projection then.
        progress: dict[str, Any] | None = None
        try:
            progress = projector.derive_progress(task_id)
        except Exception:  # noqa: BLE001 - 进度投影失败不阻断详情
            progress = None
        usage = usage_recorder.usage_summary(task_id)
        best_artifact = artifact_service.best_artifact(task_id)
        return task.get_projection(progress=progress, usage=usage, best_artifact=best_artifact)

    # -- I5 delete --

    def delete(self, params: dict[str, Any]) -> dict[str, Any]:
        """``rsi.task.delete``（web §6.4，一致性规则 §8.2）。"""
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")
        if self.harness_activation_store is not None:
            for version in self.harness_activation_store.list_versions():
                if str(version.get("task_id") or "").strip() == task_id:
                    raise RsiTaskStateConflict(
                        f"任务 {task_id} 持有保留的 Harness 版本，不可删除"
                    )
        self.store.delete(task_id, forbid_running=True, forbid_active_artifact=True)
        return {"ok": True}

    # -- 训练控制（I6 start 主体；I7/I8/I9 中优先级预留） --

    def start(self, params: dict[str, Any], *, worker: Any) -> dict[str, Any]:
        """``rsi.training.start``（web §7.1）。状态机 + 入队；引擎装配看 adapter。"""
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")
        self.store.get(task_id)  # 存在性校验 → TASK_NOT_FOUND
        status = worker.enqueue(task_id)
        return {"status": status}

    def pause(self, params: dict[str, Any], *, worker: Any) -> dict[str, Any]:
        """``rsi.training.pause``（中优先级 I7；协程衔接 TODO）。"""
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")
        status = worker.cancel(task_id, "pause")
        return {"status": status}

    def resume(self, params: dict[str, Any], *, worker: Any) -> dict[str, Any]:
        """``rsi.training.resume``（中优先级 I8；fingerprint 校验在 C2）。"""
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")
        status = worker.resume(task_id, fingerprint_check=True)
        return {"status": status}

    def terminate(self, params: dict[str, Any], *, worker: Any) -> dict[str, Any]:
        """``rsi.training.terminate``（中优先级 I9）。"""
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")
        status = worker.cancel(task_id, "terminate")
        return {"status": status}


class RsiDatasetService:
    """``rsi.dataset.validate``（I1 主入口；真实校验委托 adapter，⚠️外部）。"""

    def __init__(self, adapter: Any = None, *, adapter_resolver: Any = None) -> None:
        self.adapter = adapter
        self.adapter_resolver = adapter_resolver

    def validate(self, params: dict[str, Any], *, adapter: Any = None) -> dict[str, Any]:
        """web §5.1 出参：{valid, sample_count, errors[]}。"""
        # ``input_file`` is the v0.3 wire name; ``dataset_path`` is accepted
        # as the product-facing alias used by task creation and older clients.
        input_file = str(
            params.get("input_file") or params.get("dataset_path") or ""
        ).strip()
        if not input_file:
            raise RsiBadRequest("input_file 必填")
        scenario = str(params.get("scenario") or "").strip().upper()
        artifact_type = str(params.get("artifact_type") or "").strip().upper() or None
        validate_scenario(
            scenario, artifact_type,
            artifact_type_required=(scenario == "ARTIFACT"),
        )
        result: RsiDatasetResult
        selected_adapter = adapter or self.adapter
        if selected_adapter is None and self.adapter_resolver is not None:
            selected_adapter = self.adapter_resolver(scenario, artifact_type)
        if selected_adapter is not None and hasattr(selected_adapter, "validate_input"):
            result = selected_adapter.validate_input(
                input_file, scenario=scenario, artifact_type=artifact_type
            )
        else:
            from jiuwenswarm.agents.harness.common.rsi.adapter import default_validate_input

            result = default_validate_input(input_file)
        return result.to_dict()


class RsiReportService:
    """``rsi.report.get``（I10，⚠️外部：真值来源 C3 read_report / 事件投影）。"""

    def __init__(
        self,
        store: Any,
        projector: Any,
        usage_recorder: Any,
        artifact_service: Any,
        *,
        adapter_resolver: Any = None,
    ) -> None:
        self.store = store
        self.projector = projector
        self.usage_recorder = usage_recorder
        self.artifact_service = artifact_service
        self.adapter_resolver = adapter_resolver

    def get(self, params: dict[str, Any], *, adapter: Any = None) -> dict[str, Any]:
        """web §8.1 出参。未运行（无引擎 report）时提供任务级兜底投影。"""
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")
        task = self.store.get(task_id)
        if adapter is None and self.adapter_resolver is not None:
            adapter = self.adapter_resolver(task.scenario, task.artifact_type)
        if adapter is not None:
            state = _read_provider_snapshot(adapter, "read_state", task_id)
            report = _read_provider_snapshot(adapter, "read_report", task_id)
            if report is not None or state is not None:
                return provider_report_to_web(report, state)
            # CREATED tasks legitimately have no Provider report yet.
        progress = self.projector.derive_progress(task_id)
        usage = self.usage_recorder.usage_summary(task_id)
        best_artifact = self.artifact_service.best_artifact(task_id)
        config = task.config or {}
        results = config.get("results") or {}
        return {
            "status": task.status,
            "best_score": results.get("best_score"),
            "baseline": progress.get("baseline"),
            "metrics": {
                "eval_passed": 0,
                "eval_total": 0,
                "pruned_count": 0,
                "iterations": progress.get("iteration") or 0,
            },
            "usage": usage,
            "best_artifact": best_artifact,
            "report_summary": "",
            "markdown": None,
        }


class RsiTreeService:
    """``rsi.tree.get``（I13，⚠️外部：真值来源事件投影 + C3 快照重建）。"""

    def __init__(self, projector: Any, *, store: Any = None, adapter_resolver: Any = None) -> None:
        self.projector = projector
        self.store = store
        self.adapter_resolver = adapter_resolver

    def get(self, params: dict[str, Any], *, adapter: Any = None) -> dict[str, Any]:
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")
        task = self.store.get(task_id) if self.store is not None else None
        if adapter is None and task is not None and self.adapter_resolver is not None:
            adapter = self.adapter_resolver(task.scenario, task.artifact_type)
        # The in-memory projector is disposable.  Restore the durable workspace
        # view before asking the Provider for optional recovery/backfill data.
        self.projector.load_from_disk(task_id)
        self.projector.register_root(task_id)
        if adapter is not None:
            tree = _read_provider_snapshot(adapter, "get_tree", task_id)
            if tree is not None:
                return self.projector.merge_provider_tree(task_id, tree)
        return self.projector.derive_tree(task_id)


class RsiUsageService:
    """``rsi.usage.get``（I11，⚠️外部：usage 插桩后收尾；C1 单价预留）。"""

    def __init__(self, usage_recorder: Any, *, store: Any = None, adapter_resolver: Any = None) -> None:
        self.usage_recorder = usage_recorder
        self.store = store
        self.adapter_resolver = adapter_resolver

    def get(self, params: dict[str, Any], *, adapter: Any = None) -> dict[str, Any]:
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")
        task = self.store.get(task_id) if self.store is not None else None
        if adapter is None and task is not None and self.adapter_resolver is not None:
            adapter = self.adapter_resolver(task.scenario, task.artifact_type)
        if adapter is not None:
            # During a live run the event consumer has the richest view
            # (including per-iteration cumulative snapshots).  Prefer it and
            # fall back to the durable Provider snapshot after a restart.
            try:
                return self.usage_recorder.get(task_id)
            except RsiTaskNotFound:
                # Provider snapshot is the recovery path after a restart.
                pass
            state = _read_provider_snapshot(adapter, "read_state", task_id)
            report = _read_provider_snapshot(adapter, "read_report", task_id)
            usage = None
            if report is not None:
                usage = provider_usage_to_dict(getattr(report, "usage", None))
            if usage is None and state is not None:
                usage = provider_usage_to_dict(getattr(state, "usage", None))
            if usage is not None:
                return {
                    "usage": usage,
                    "per_iteration": [],
                    "usage_by_node": {},
                }
        return self.usage_recorder.get(task_id)


class RsiArtifactDownloadService:
    """``rsi.artifact.download`` 定位文件或产物目录。

    文件继续返回 Gateway HTTP 下载链接；目录不伪造一个不可用的文件
    链接，前端通过 artifact.files.list/get 浏览目录并下载其中的文件。
    """

    def __init__(self, artifact_service: Any, store: Any, *, adapter_resolver: Any = None) -> None:
        self.artifact_service = artifact_service
        self.store = store
        self.adapter_resolver = adapter_resolver

    def locate(self, params: dict[str, Any], *, adapter: Any = None) -> dict[str, Any]:
        """返回文件/目录信息；HTTP 文件流由通道层完成。"""
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")
        task = self.store.get(task_id)
        if adapter is None and self.adapter_resolver is not None:
            adapter = self.adapter_resolver(task.scenario, task.artifact_type)
        artifact_id = str(params.get("artifact_id") or "").strip() or None
        if adapter is not None:
            return self._locate_provider(
                task_id,
                artifact_id,
                adapter,
                scenario=task.scenario,
                params=params,
            )
        artifact = self.artifact_service.locate(task_id, artifact_id)
        # 消费成功 → 放行后续 delete（在用产物语义：下载即消费）
        if artifact_id is None or artifact.is_best:
            try:
                self.store.mark_active_ref_released(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[RSI] 在用产物标记释放失败 task=%s: %s", task_id, exc)
        result = {
            "path": artifact.path,
            "kind": artifact.kind,
            "is_best": artifact.is_best,
            "filename": Path(artifact.path).name,
            "is_directory": Path(artifact.path).is_dir(),
        }
        if not result["is_directory"]:
            result.update(_download_fields(result["path"], params))
        return result

    def _locate_provider(
        self,
        task_id: str,
        artifact_id: str | None,
        adapter: Any,
        *,
        scenario: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            raw_artifact = adapter.locate_artifact(task_id, artifact_id)
        except OSError as exc:
            raise RsiArtifactNotFound(str(exc)) from exc
        raw = _plain_provider(raw_artifact)
        if not isinstance(raw, dict):
            raise RsiArtifactNotFound("Provider 返回了无法识别的产物引用")
        task = self.store.get(task_id)
        path_value = raw.get("path")
        if path_value:
            candidate = Path(path_value).expanduser()
            if not candidate.is_absolute():
                candidate = Path(task.run_dir) / candidate
            path_value = str(candidate)
        try:
            path = validate_provider_artifact_path(path_value)
        except RsiPathInvalid as exc:
            raise RsiArtifactNotFound(str(exc)) from exc
        task_dir = (Path(self.store.tasks_root) / task_id).resolve()
        try:
            path.relative_to(task_dir)
        except ValueError as exc:
            raise RsiPathInvalid("Provider 产物路径超出任务目录") from exc
        if not path.is_file() and not path.is_dir():
            raise RsiArtifactNotFound(f"产物路径不存在: {path}")
        best_node_id = None
        report = _read_provider_snapshot(adapter, "read_report", task_id)
        if report is not None:
            best_node_id = _plain_provider(report).get("best_node_id")
        is_best = artifact_id is None or (
            best_node_id is not None and raw.get("node_id") == best_node_id
        )
        if is_best:
            try:
                self.store.mark_active_ref_released(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[RSI] 在用产物标记释放失败 task=%s: %s", task_id, exc)
        result = {
            "path": str(path),
            "kind": (
                "harness_plugin"
                if str(scenario).upper() == Scenario.HARNESS.value
                else "artifact_package"
            ),
            "is_best": is_best,
            "filename": path.name,
            "is_directory": path.is_dir(),
        }
        if not result["is_directory"]:
            result.update(_download_fields(result["path"], params))
        return result


def _download_fields(path: str, params: dict[str, Any]) -> dict[str, str]:
    """Issue the same short-lived HTTP file token used by chat attachments."""

    session_id = str(params.get("session_id") or "").strip()
    # ``proxy_unary_request`` promotes the canonical ``user_id`` to the E2A
    # envelope and removes it from params.  Keep a dedicated internal copy for
    # the returned browser URL so AgentOS can route the later HTTP request to
    # the same user container.
    user_id = str(params.get("_download_user_id") or params.get("user_id") or "").strip()
    info = build_file_download_info(
        path,
        Path(path).name,
        session_id=session_id,
        user_id=user_id,
    )
    return {
        "download_url": str(info["download_url"]),
        "download_token": str(info["download_token"]),
    }


def _positive_int(raw: Any, *, default: int, field: str) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RsiBadRequest(f"{field} 必须是正整数") from exc
    if value < 1:
        raise RsiBadRequest(f"{field} 必须是正整数")
    return value


def _program_manifest_max_iterations(path: str | None) -> int | None:
    """Read a program bundle's optional search budget from ``task.json``.

    The bundle manifest is an input owned by the program Provider.  Reading
    only this scalar here lets the service persist the same budget that the
    Provider will execute, while leaving all seed/scorecard resolution to the
    Provider.  A plain program directory/file, or a manifest without the
    field, keeps the RSI default of one iteration.
    """

    if not path:
        return None
    candidate = Path(path).expanduser()
    if not candidate.is_dir():
        return None
    manifest_path = candidate / "task.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RsiDatasetInvalid(
            f"程序 case 的 task.json 无法读取: {exc}",
            errors=[
                {
                    "reason": "程序 case 的 task.json 不是有效 JSON",
                    "code": "ARTIFACT_MANIFEST_INVALID",
                }
            ],
        ) from exc
    if not isinstance(manifest, Mapping):
        raise RsiDatasetInvalid(
            "程序 case 的 task.json 顶层必须是对象",
            errors=[
                {
                    "reason": "程序 case 的 task.json 顶层必须是对象",
                    "code": "ARTIFACT_MANIFEST_INVALID",
                }
            ],
        )
    if "max_iterations" not in manifest or manifest["max_iterations"] is None:
        return None
    return _positive_int(
        manifest["max_iterations"],
        default=1,
        field="task.json.max_iterations",
    )


def _harness_profile_options(params: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the direct API's Generic Harness training controls."""

    nested = params.get("training_options")
    source = nested if isinstance(nested, Mapping) else {}

    def value(name: str, default: Any = None) -> Any:
        if name in params:
            return params.get(name)
        return source.get(name, default)

    domain_raw = str(value("domain", "") or "").strip().lower()
    if domain_raw and domain_raw not in {"general", "office"}:
        raise RsiBadRequest("domain 只支持 general 或 office")

    execution_mode = str(value("execution_mode", "local") or "local").strip().lower()
    if execution_mode == "auto":
        execution_mode = "local"
    if execution_mode != "local":
        raise RsiUnsupportedParameter(
            "AgentServer Generic Harness 适配目前只支持 execution_mode=local"
        )

    evaluation_method = str(value("evaluation_method", "script-based")).strip().lower().replace("-", "_")
    if evaluation_method not in {"script_based", "exact_match", "llm_as_judge"}:
        raise RsiUnsupportedParameter("evaluation_method must be script_based, exact_match or llm_as_judge")

    sibling_candidate_count = _positive_int(
        value("sibling_candidate_count"), default=1, field="sibling_candidate_count"
    )
    improver_policy_ref = str(value("improver_policy_ref", "") or "").strip()
    if sibling_candidate_count != 1 or improver_policy_ref:
        raise RsiUnsupportedParameter(
            "single-harness optimization requires one candidate and no improver evolution policy"
        )

    return {
        "domain": domain_raw,
        "improver_policy_ref": improver_policy_ref,
        "execution_mode": execution_mode,
        "evaluation_method": evaluation_method,
        "max_epochs": _positive_int(value("max_epochs"), default=1, field="max_epochs"),
        "batch_size": _positive_int(value("batch_size"), default=1, field="batch_size"),
        "max_issue_attempts": _non_negative_int(
            value("max_issue_attempts"), default=8, field="max_issue_attempts"
        ),
        "max_repair_rounds": _positive_int(
            value("max_repair_rounds"), default=3, field="max_repair_rounds"
        ),
        "sibling_candidate_count": sibling_candidate_count,
        "rollout_concurrency": _positive_int(
            value("rollout_concurrency"), default=1, field="rollout_concurrency"
        ),
    }


def _non_negative_int(raw: Any, *, default: int, field: str) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RsiBadRequest(f"{field} 必须是非负整数") from exc
    if value < 0:
        raise RsiBadRequest(f"{field} 必须是非负整数")
    return value


def _ensure_provider_valid(result: Any) -> None:
    """Map either agent-core or local adapter validation to RSI errors."""
    if bool(getattr(result, "valid", False)):
        return
    raw_errors = getattr(result, "errors", None) or []
    errors: list[dict[str, str]] = []
    for item in raw_errors:
        raw = _plain_provider(item)
        if not isinstance(raw, dict):
            continue
        errors.append(
            {
                "reason": str(raw.get("message") or raw.get("reason") or "输入校验失败"),
                "code": str(raw.get("code") or "DATASET_INVALID"),
            }
        )
    path_invalid = next((item for item in errors if item.get("code") == "PATH_INVALID"), None)
    if path_invalid is not None:
        raise RsiPathInvalid(path_invalid["reason"])
    message = errors[0]["reason"] if errors else "Provider 输入校验失败"
    raise RsiDatasetInvalid(message, errors=errors)


def _plain_provider(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _plain_provider(model_dump(mode="python"))
        except TypeError:
            return _plain_provider(model_dump())
    model_dict = getattr(value, "dict", None)
    if callable(model_dict):
        return _plain_provider(model_dict())
    if is_dataclass(value):
        return {key: _plain_provider(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _plain_provider(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_provider(item) for item in value]
    return value


_PROVIDER_QUERY_FALLBACK = (
    FileNotFoundError,
    KeyError,
    NotImplementedError,
    OSError,
    RsiNotReady,
)


def _read_provider_snapshot(adapter: Any, method_name: str, task_id: str) -> Any:
    """Read an optional Provider snapshot while preserving service fallbacks."""

    method = getattr(adapter, method_name, None)
    if not callable(method):
        return None
    try:
        return method(task_id)
    except _PROVIDER_QUERY_FALLBACK:
        return None
