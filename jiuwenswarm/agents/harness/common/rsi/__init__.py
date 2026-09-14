"""RSI 服务域（agents/harness/common/rsi）—— Event Sink 主通道落地（内部接口 v3）。

六组件 + 派生薄服务 + 适配层 + 事件模型；对外统一组件拼装入口见
``RsiServiceContext``（组合根）。
"""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.rsi.adapter import RsiEngineAdapter, RsiEventSink
from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import ArtifactEngineAdapter
from jiuwenswarm.agents.harness.common.rsi.context import (
    RsiServiceContext,
    build_rsi_service_context,
    get_rsi_workspace_root,
)
from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiArtifactNotFound,
    RsiBadRequest,
    RsiDatasetInvalid,
    RsiError,
    RsiInvalidHarness,
    RsiHarnessInstallConflict,
    RsiHarnessInstallFailed,
    RsiHarnessInvalid,
    RsiHarnessNotPublished,
    RsiHarnessNotReady,
    RsiModelConfigInvalid,
    RsiModelNotFound,
    RsiPathInvalid,
    RsiPathNotAllowed,
    RsiResumeMismatch,
    RsiResumeInputChanged,
    RsiScenarioNotSupported,
    RsiTaskNotFound,
    RsiTaskStateConflict,
    RsiUnsupportedParameter,
)
from jiuwenswarm.agents.harness.common.rsi.harness_activation import (
    PublishedHarnessRef,
    RsiHarnessActivationStore,
    RsiHarnessInstaller,
    hash_harness_package,
    parse_published_harness_refs,
)
from jiuwenswarm.agents.harness.common.rsi.events import EngineEvent
from jiuwenswarm.agents.harness.common.rsi.harness_adapter import (
    HarnessEngineAdapter,
    HarnessEngineRequest,
    HarnessProviderContract,
)
from jiuwenswarm.agents.harness.common.rsi.mock_harness_provider import MockHarnessProvider
from jiuwenswarm.agents.harness.common.rsi.materializer import (
    RsiTaskMaterialization,
    RsiTaskMaterializer,
    VALIDATION_PROFILE_NAME,
)
from jiuwenswarm.agents.harness.common.rsi.model_resolver import (
    ResolvedRsiModel,
    RsiModelConfigResolver,
    select_rsi_model_entry,
)
from jiuwenswarm.agents.harness.common.rsi.models import RsiTask, RsiTaskView, TaskStatus
from jiuwenswarm.agents.harness.common.rsi.paper_provider import PaperProvider
from jiuwenswarm.agents.harness.common.rsi.provider_factory import (
    build_mock_rsi_adapters,
    build_rsi_adapters,
)

__all__ = [
    "EngineEvent",
    "ArtifactEngineAdapter",
    "HarnessEngineAdapter",
    "HarnessEngineRequest",
    "HarnessProviderContract",
    "MockHarnessProvider",
    "PaperProvider",
    "RsiArtifactNotFound",
    "RsiBadRequest",
    "RsiDatasetInvalid",
    "RsiEngineAdapter",
    "RsiError",
    "RsiEventSink",
    "RsiInvalidHarness",
    "RsiHarnessActivationStore",
    "RsiHarnessInstallConflict",
    "RsiHarnessInstallFailed",
    "RsiHarnessInstaller",
    "RsiHarnessInvalid",
    "RsiHarnessNotPublished",
    "RsiHarnessNotReady",
    "RsiModelConfigInvalid",
    "RsiModelNotFound",
    "RsiPathInvalid",
    "RsiPathNotAllowed",
    "RsiResumeMismatch",
    "RsiResumeInputChanged",
    "RsiScenarioNotSupported",
    "RsiServiceContext",
    "RsiTask",
    "RsiTaskNotFound",
    "RsiTaskStateConflict",
    "RsiTaskView",
    "RsiTaskMaterialization",
    "RsiTaskMaterializer",
    "RsiUnsupportedParameter",
    "PublishedHarnessRef",
    "hash_harness_package",
    "parse_published_harness_refs",
    "ResolvedRsiModel",
    "RsiModelConfigResolver",
    "select_rsi_model_entry",
    "VALIDATION_PROFILE_NAME",
    "TaskStatus",
    "build_mock_rsi_adapters",
    "build_rsi_adapters",
    "build_rsi_service_context",
    "get_rsi_workspace_root",
]
