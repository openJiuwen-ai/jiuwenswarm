"""RSI 三场景 Provider 装配，统一选择生产或 mock 执行模式。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import ArtifactEngineAdapter
from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineAdapter

logger = logging.getLogger(__name__)


def build_mock_rsi_adapters(
    tasks_root: str | Path,
    *,
    model_resolver: Any = None,
) -> dict[str, Any]:
    """Build all deterministic adapters used by local RSI E2E."""
    from jiuwenswarm.agents.harness.common.rsi.mock_artifact_provider import (
        MockArtifactProvider,
    )
    from jiuwenswarm.agents.harness.common.rsi.mock_harness_provider import (
        MockHarnessProvider,
    )

    root = Path(tasks_root)
    return {
        "HARNESS": HarnessEngineAdapter(MockHarnessProvider(root)),
        "ARTIFACT:PROGRAM": ArtifactEngineAdapter(
            "PROGRAM",
            MockArtifactProvider(root, "program"),
            model_resolver=model_resolver,
            requires_model=False,
            tasks_root=root,
        ),
        "ARTIFACT:PAPER": ArtifactEngineAdapter(
            "PAPER",
            MockArtifactProvider(root, "paper"),
            model_resolver=model_resolver,
            requires_model=False,
            tasks_root=root,
        ),
    }


def build_rsi_adapters(
    tasks_root: str | Path,
    *,
    mode: str = "real",
    harness_provider: Any = None,
    artifact_adapters: dict[str, Any] | None = None,
    paper_provider: Any = None,
    model_resolver: Any = None,
) -> dict[str, Any]:
    """为 AgentServer 装配 Harness、论文、程序适配器。

    生产模式使用配套 SDK 引擎，保留 Provider 注入和程序达标停止行为。
    Harness 使用配置解析对象；产物适配器使用解析为 Model 的调用接口。
    """
    artifact_model_resolver = (
        model_resolver if callable(model_resolver) or model_resolver is None
        else model_resolver.build_model
    )
    normalized_mode = str(mode or "real").strip().lower()
    adapters = dict(artifact_adapters or {})
    if normalized_mode == "mock":
        adapters.update(build_mock_rsi_adapters(tasks_root, model_resolver=artifact_model_resolver))
    else:
        if "ARTIFACT:PROGRAM" not in adapters:
            # The public ``program_opt.provider`` module is the Protocol;
            # agent-core's concrete implementation is the PUCT provider.
            try:
                from jiuwenswarm.agents.harness.common.rsi.program_threshold_provider import (
                    ThresholdStoppingProgramProvider,
                )
            except ImportError as exc:
                # openjiuwen program_opt 引擎未随本构建分发：跳过装配，任务
                # 列表/投影/产物等本地能力不受影响，引擎操作在使用点降级。
                logger.warning(
                    "openjiuwen program_opt 未随本构建分发，跳过 ARTIFACT:PROGRAM 适配器装配: %s",
                    exc,
                )
            else:
                adapters["ARTIFACT:PROGRAM"] = ArtifactEngineAdapter(
                    "PROGRAM",
                    ThresholdStoppingProgramProvider(),
                    model_resolver=artifact_model_resolver,
                    tasks_root=tasks_root,
                )
        if paper_provider is None and "ARTIFACT:PAPER" not in adapters:
            # The implementation is intentionally owned by agent-core.  The
            # public ``paper_opt.provider`` module is only the Protocol; the
            # concrete implementation lives under the autoResearch tree provider.
            try:
                from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.provider import (
                    PaperArtifactProviderImpl,
                )
            except ImportError as exc:
                # 同 program_opt：paper_opt 引擎未随本构建分发时跳过装配。
                logger.warning(
                    "openjiuwen paper_opt 未随本构建分发，跳过 ARTIFACT:PAPER 适配器装配: %s",
                    exc,
                )
            else:
                paper_provider = PaperArtifactProviderImpl()
        if paper_provider is not None:
            adapters["ARTIFACT:PAPER"] = ArtifactEngineAdapter(
                "PAPER",
                paper_provider,
                model_resolver=artifact_model_resolver,
                tasks_root=tasks_root,
            )
    if harness_provider is None and normalized_mode != "mock":
        from jiuwenswarm.agents.harness.common.rsi.harness_provider import HarnessProvider
        harness_provider = HarnessProvider(tasks_root, model_resolver=model_resolver)
    if harness_provider is not None:
        adapters["HARNESS"] = HarnessEngineAdapter(harness_provider)
    return adapters


__all__ = ["build_mock_rsi_adapters", "build_rsi_adapters"]
