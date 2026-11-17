"""RSI 服务域（agents/harness/common/rsi）—— Event Sink 主通道落地（内部接口 v3）。

六组件 + 派生薄服务 + 适配层 + 事件模型；对外统一组件拼装入口见
``RsiServiceContext``（组合根）。
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "ArtifactEngineAdapter": ("jiuwenswarm.agents.harness.common.rsi.artifact_adapter", "ArtifactEngineAdapter"),
    "EngineEvent": ("jiuwenswarm.agents.harness.common.rsi.events", "EngineEvent"),
    "HarnessEngineAdapter": ("jiuwenswarm.agents.harness.common.rsi.harness_adapter", "HarnessEngineAdapter"),
    "HarnessEngineRequest": ("jiuwenswarm.agents.harness.common.rsi.harness_adapter", "HarnessEngineRequest"),
    "HarnessProviderContract": ("jiuwenswarm.agents.harness.common.rsi.harness_adapter", "HarnessProviderContract"),
    "MockHarnessProvider": ("jiuwenswarm.agents.harness.common.rsi.mock_harness_provider", "MockHarnessProvider"),
    "PaperProvider": ("jiuwenswarm.agents.harness.common.rsi.paper_provider", "PaperProvider"),
    "PublishedHarnessRef": ("jiuwenswarm.agents.harness.common.rsi.harness_activation", "PublishedHarnessRef"),
    "ResolvedRsiModel": ("jiuwenswarm.agents.harness.common.rsi.model_resolver", "ResolvedRsiModel"),
    "RsiArtifactNotFound": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiArtifactNotFound"),
    "RsiBadRequest": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiBadRequest"),
    "RsiDatasetInvalid": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiDatasetInvalid"),
    "RsiEngineAdapter": ("jiuwenswarm.agents.harness.common.rsi.adapter", "RsiEngineAdapter"),
    "RsiError": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiError"),
    "RsiEventSink": ("jiuwenswarm.agents.harness.common.rsi.adapter", "RsiEventSink"),
    "RsiHarnessActivationStore": (
        "jiuwenswarm.agents.harness.common.rsi.harness_activation",
        "RsiHarnessActivationStore",
    ),
    "RsiHarnessInstallConflict": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiHarnessInstallConflict"),
    "RsiHarnessInstallFailed": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiHarnessInstallFailed"),
    "RsiHarnessInstaller": ("jiuwenswarm.agents.harness.common.rsi.harness_activation", "RsiHarnessInstaller"),
    "RsiHarnessInvalid": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiHarnessInvalid"),
    "RsiHarnessNotPublished": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiHarnessNotPublished"),
    "RsiHarnessNotReady": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiHarnessNotReady"),
    "RsiInvalidHarness": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiInvalidHarness"),
    "RsiModelConfigInvalid": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiModelConfigInvalid"),
    "RsiModelConfigResolver": ("jiuwenswarm.agents.harness.common.rsi.model_resolver", "RsiModelConfigResolver"),
    "RsiModelNotFound": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiModelNotFound"),
    "RsiPathInvalid": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiPathInvalid"),
    "RsiPathNotAllowed": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiPathNotAllowed"),
    "RsiResumeInputChanged": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiResumeInputChanged"),
    "RsiResumeMismatch": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiResumeMismatch"),
    "RsiScenarioNotSupported": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiScenarioNotSupported"),
    "RsiServiceContext": ("jiuwenswarm.agents.harness.common.rsi.context", "RsiServiceContext"),
    "RsiTask": ("jiuwenswarm.agents.harness.common.rsi.models", "RsiTask"),
    "RsiTaskMaterialization": ("jiuwenswarm.agents.harness.common.rsi.materializer", "RsiTaskMaterialization"),
    "RsiTaskMaterializer": ("jiuwenswarm.agents.harness.common.rsi.materializer", "RsiTaskMaterializer"),
    "RsiTaskNotFound": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiTaskNotFound"),
    "RsiTaskStateConflict": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiTaskStateConflict"),
    "RsiTaskView": ("jiuwenswarm.agents.harness.common.rsi.models", "RsiTaskView"),
    "RsiUnsupportedParameter": ("jiuwenswarm.agents.harness.common.rsi.errors", "RsiUnsupportedParameter"),
    "TaskStatus": ("jiuwenswarm.agents.harness.common.rsi.models", "TaskStatus"),
    "VALIDATION_PROFILE_NAME": ("jiuwenswarm.agents.harness.common.rsi.materializer", "VALIDATION_PROFILE_NAME"),
    "build_mock_rsi_adapters": ("jiuwenswarm.agents.harness.common.rsi.provider_factory", "build_mock_rsi_adapters"),
    "build_rsi_adapters": ("jiuwenswarm.agents.harness.common.rsi.provider_factory", "build_rsi_adapters"),
    "build_rsi_service_context": ("jiuwenswarm.agents.harness.common.rsi.context", "build_rsi_service_context"),
    "get_rsi_workspace_root": ("jiuwenswarm.agents.harness.common.rsi.context", "get_rsi_workspace_root"),
    "hash_harness_package": ("jiuwenswarm.agents.harness.common.rsi.harness_activation", "hash_harness_package"),
    "parse_published_harness_refs": (
        "jiuwenswarm.agents.harness.common.rsi.harness_activation",
        "parse_published_harness_refs",
    ),
    "select_rsi_model_entry": ("jiuwenswarm.agents.harness.common.rsi.model_resolver", "select_rsi_model_entry"),
}

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



def __getattr__(name: str) -> object:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
