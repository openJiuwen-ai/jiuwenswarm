"""RSI 服务域组合根：装配 store/worker/projector/artifact/usage/薄服务（内部 v3 §7 复用清单）。

AgentServer 侧用 ``build_rsi_service_context(tasks_root)`` 一次性构建，
再注入推送回调（send_push 包装）与 harness_refs 快照提供方。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from jiuwenswarm.agents.harness.common.rsi.artifact_service import RsiArtifactService
from jiuwenswarm.agents.harness.common.rsi.artifact_files_service import RsiArtifactFilesService
from jiuwenswarm.agents.harness.common.rsi.harness_activation import (
    RsiHarnessActivationStore,
    RsiHarnessInstaller,
)
from jiuwenswarm.agents.harness.common.rsi.projector import RsiProjector
from jiuwenswarm.agents.harness.common.rsi.recovery import RsiWorkspaceRecovery
from jiuwenswarm.agents.harness.common.rsi.services import (
    RsiArtifactDownloadService,
    RsiDatasetService,
    RsiReportService,
    RsiTaskService,
    RsiTreeService,
    RsiUsageService,
)
from jiuwenswarm.agents.harness.common.rsi.task_store import RsiTaskStore
from jiuwenswarm.agents.harness.common.rsi.usage_recorder import RsiUsageRecorder
from jiuwenswarm.agents.harness.common.rsi.worker import RsiWorker


class RsiServiceContext:
    """服务域组件容器（组合根）。"""

    def __init__(
        self,
        tasks_root: Path,
        *,
        worker_push_callbacks: dict[str, Any] | None = None,
        adapters: dict[str, Any] | None = None,
        harness_materializer: Any = None,
        model_resolver: Any = None,
        agent_manager: Any = None,
        harness_activation_store: RsiHarnessActivationStore | None = None,
        harness_installer: Any = None,
    ) -> None:
        self.tasks_root = Path(tasks_root)
        self._workspace_recovered = False
        self._recovery_summary: dict[str, Any] | None = None
        # Keep the production materialization dependencies on the context so
        # the composition root can inject the exact same instances into the
        # Provider and TaskService.  They remain ``None`` for mock/test roots.
        self.harness_materializer = harness_materializer
        self.model_resolver = model_resolver
        self.store = RsiTaskStore(self.tasks_root)
        self.harness_activation_store = harness_activation_store or RsiHarnessActivationStore(
            # The activation pointer is a workspace-level index.  Installed
            # Harness versions themselves are copied into each task's
            # ``harness/versions`` directory by RsiHarnessInstaller.
            self.tasks_root.expanduser().resolve()
        )
        self.harness_installer = harness_installer or RsiHarnessInstaller(
            self.store,
            self.adapter_for_task,
            agent_manager,
            activation_store=self.harness_activation_store,
        )
        self.usage_recorder = RsiUsageRecorder()
        self.projector = RsiProjector(self.tasks_root)
        self.artifact_service = RsiArtifactService(self.tasks_root)
        self.worker = RsiWorker(
            store=self.store,
            adapters={},  # C5 HarnessEngineAdapter / ArtifactEngineAdapter 注入（⚠️外部）
            usage_recorder=self.usage_recorder,
            projector=self.projector,
            artifact_service=self.artifact_service,
            push_callbacks=worker_push_callbacks or {},
        )
        self.adapters = self.worker.adapters
        if adapters:
            self.register_adapters(adapters)
        # 薄服务
        self.task_service = RsiTaskService(
            self.store,
            adapter_resolver=self.adapter_for,
            harness_materializer=harness_materializer,
            model_resolver=model_resolver,
            harness_activation_store=self.harness_activation_store,
        )
        self.dataset_service = RsiDatasetService(adapter_resolver=self.adapter_for)
        self.tree_service = RsiTreeService(
            self.projector,
            store=self.store,
            adapter_resolver=self.adapter_for,
        )
        self.usage_service = RsiUsageService(
            self.usage_recorder,
            store=self.store,
            adapter_resolver=self.adapter_for,
        )
        self.report_service = RsiReportService(
            self.store,
            self.projector,
            self.usage_recorder,
            self.artifact_service,
            adapter_resolver=self.adapter_for,
        )
        self.artifact_download_service = RsiArtifactDownloadService(
            self.artifact_service,
            self.store,
            adapter_resolver=self.adapter_for,
        )
        self.artifact_files_service = RsiArtifactFilesService(self.store)
        self.workspace_recovery = RsiWorkspaceRecovery(
            self.store,
            self.projector,
            self.adapter_for,
        )

    def bind_task_service(
        self,
        *,
        adapter: Any = None,
        harness_refs_provider: Callable[[], str | None] | None = None,
        harness_materializer: Any = None,
        model_resolver: Any = None,
    ) -> None:
        self.task_service.adapter = adapter
        self.task_service.harness_refs_provider = harness_refs_provider
        if harness_materializer is not None:
            self.task_service.harness_materializer = harness_materializer
        if model_resolver is not None:
            self.task_service.model_resolver = model_resolver
        if adapter is not None:
            # Backward-compatible harness adapter injection.  Artifact
            # adapters should use ``register_adapters`` with an explicit type.
            self.register_adapters({"HARNESS": adapter})

    def bind_harness_installer(self, agent_manager: Any) -> None:
        """Bind the AgentServer manager used by the RSI install operation."""

        self.harness_installer.agent_manager = agent_manager

    def bind_dataset_service(self, adapter: Any) -> None:
        self.dataset_service.adapter = adapter

    def register_adapters(self, adapters: dict[str, Any]) -> None:
        """注册场景适配器（HARNESS→HarnessEngineAdapter / ARTIFACT→ArtifactEngineAdapter）。"""
        self.adapters.update(adapters)

    def adapter_for(self, scenario: str | None, artifact_type: str | None = None) -> Any:
        """Resolve an adapter using the task's public scene/type pair."""
        scenario_key = str(scenario or "").strip().upper()
        artifact_key = str(artifact_type or "").strip().upper()
        candidates = []
        if scenario_key == "ARTIFACT":
            if artifact_key:
                candidates.extend(
                    [
                        f"ARTIFACT:{artifact_key}",
                        f"{scenario_key}:{artifact_key}",
                        f"artifact:{artifact_key.lower()}",
                    ]
                )
            candidates.append("ARTIFACT")
        elif scenario_key:
            candidates.extend([scenario_key, scenario_key.lower()])
        for key in candidates:
            adapter = self.adapters.get(key)
            if adapter is not None:
                return adapter
        return None

    def adapter_for_task(self, task_id: str) -> Any:
        if not task_id:
            return None
        task = self.store.get(task_id)
        return self.adapter_for(task.scenario, task.artifact_type)

    def install_mock_artifact_adapters(
        self,
        *,
        model_resolver: Any = None,
        iteration_delay: float = 0.0,
        node_delay: float = 0.0,
        branching_factor: int = 3,
    ) -> dict[str, Any]:
        """Install deterministic artifact Providers for local service E2E tests."""
        from jiuwenswarm.agents.harness.common.rsi.mock_artifact_provider import (
            build_mock_artifact_adapters,
        )

        adapters = build_mock_artifact_adapters(
            self.tasks_root,
            model_resolver=model_resolver,
            requires_model=False,
            iteration_delay=iteration_delay,
            node_delay=node_delay,
            branching_factor=branching_factor,
        )
        self.register_adapters(adapters)
        return adapters

    def install_mock_rsi_adapters(self, *, model_resolver: Any = None) -> dict[str, Any]:
        """Install Harness + Program + Paper mock Providers for local E2E."""
        from jiuwenswarm.agents.harness.common.rsi.provider_factory import (
            build_mock_rsi_adapters,
        )

        adapters = build_mock_rsi_adapters(
            self.tasks_root,
            model_resolver=model_resolver,
        )
        self.register_adapters(adapters)
        return adapters

    def register_harness_provider(self, provider: Any) -> Any:
        """Register the production ``HarnessProvider`` at the RSI seam."""
        from jiuwenswarm.agents.harness.common.rsi.harness_adapter import (
            HarnessEngineAdapter,
        )

        adapter = HarnessEngineAdapter(provider)
        self.register_adapters({"HARNESS": adapter})
        return adapter

    def register_artifact_providers(
        self,
        *,
        program: Any = None,
        paper: Any = None,
        model_resolver: Any = None,
    ) -> dict[str, Any]:
        """Wrap concrete program/paper Providers at the AgentServer seam."""
        from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import (
            ArtifactEngineAdapter,
        )

        adapters: dict[str, Any] = {}
        if program is not None:
            adapters["ARTIFACT:PROGRAM"] = ArtifactEngineAdapter(
                "PROGRAM", program, model_resolver=model_resolver
            )
        if paper is not None:
            adapters["ARTIFACT:PAPER"] = ArtifactEngineAdapter(
                "PAPER", paper, model_resolver=model_resolver
            )
        self.register_adapters(adapters)
        return adapters

    def register_worker_push(self, push_callbacks: dict[str, Any]) -> None:
        self.worker._push_callbacks.update(push_callbacks)  # noqa: SLF001 - 组合根内装配

    def ensure_root(self, task_id: str) -> None:
        self.projector.register_root(task_id)

    def recover_workspace(self) -> dict[str, Any]:
        """Run the one-time AgentServer restart reconciliation for this context."""

        if self._workspace_recovered:
            return dict(self._recovery_summary or {})
        summary = self.workspace_recovery.recover()
        self._workspace_recovered = True
        self._recovery_summary = dict(summary)
        return summary


def build_rsi_service_context(
    tasks_root: Any,
    *,
    adapters: dict[str, Any] | None = None,
    harness_materializer: Any = None,
    model_resolver: Any = None,
    enable_harness_materialization: bool | None = None,
    allow_missing_harness: bool = False,
    agent_manager: Any = None,
    harness_activation_store: RsiHarnessActivationStore | None = None,
    harness_installer: Any = None,
) -> RsiServiceContext:
    """默认组合根（tasks_root 缺省取 ``.jiuwenswarm/rsi/tasks``）。"""
    use_production_harness = enable_harness_materialization
    if tasks_root is None:
        tasks_root = get_rsi_workspace_root() / "tasks"
        if use_production_harness is None:
            use_production_harness = True
    if use_production_harness:
        if harness_materializer is None:
            from jiuwenswarm.agents.harness.common.rsi.materializer import (
                RsiTaskMaterializer,
            )

            harness_materializer = RsiTaskMaterializer(
                Path(tasks_root),
                dataset_root=_configured_root("RSI_DATASET_ROOT"),
                harness_root=_configured_root("RSI_HARNESS_ROOT"),
                allow_missing_harness=allow_missing_harness,
            )
        if model_resolver is None:
            from jiuwenswarm.agents.harness.common.rsi.model_resolver import (
                RsiModelConfigResolver,
            )

            model_resolver = RsiModelConfigResolver()
    return RsiServiceContext(
        Path(tasks_root),
        adapters=adapters,
        harness_materializer=harness_materializer,
        model_resolver=model_resolver,
        agent_manager=agent_manager,
        harness_activation_store=harness_activation_store,
        harness_installer=harness_installer,
    )


def get_rsi_workspace_root() -> Path:
    """Return the RSI workspace root (``.jiuwenswarm/rsi``)."""

    from jiuwenswarm.common.utils import get_user_workspace_dir

    return (Path(get_user_workspace_dir()) / "rsi").expanduser().resolve()


def _configured_root(name: str) -> Path | None:
    """Read an optional trusted input root without making env mandatory.

    Deployments that expose browser-selected local files should set these
    roots explicitly.  Leaving a root unset preserves compatibility for
    callers that already perform their own trust checks (notably unit tests).
    """

    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else None
