"""RSI Provider assembly.

The factory is the only place that chooses mock versus production execution.
The production Harness implementation is intentionally named
``HarnessProvider``; callers inject an instance once that implementation is
available, without adding a separate production-prefix name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import ArtifactEngineAdapter
from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineAdapter


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
        ),
        "ARTIFACT:PAPER": ArtifactEngineAdapter(
            "PAPER",
            MockArtifactProvider(root, "paper"),
            model_resolver=model_resolver,
            requires_model=False,
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
    """Assemble adapters for one AgentServer.

    ``mode='mock'`` installs Harness, Program, and Paper mock adapters.  In
    production mode a concrete ``HarnessProvider`` can be injected and is
    wrapped at the same seam.  The program and paper adapters default to the
    concrete Providers shipped by the configured agent-core RSI dependency;
    ``paper_provider`` remains injectable for compatibility and tests.
    """
    normalized_mode = str(mode or "real").strip().lower()
    adapters = dict(artifact_adapters or {})
    if normalized_mode == "mock":
        adapters.update(build_mock_rsi_adapters(tasks_root, model_resolver=model_resolver))
    else:
        if "ARTIFACT:PROGRAM" not in adapters:
            # The public ``program_opt.provider`` module is the Protocol;
            # agent-core's concrete implementation is the PUCT provider.
            from openjiuwen.rsi.artifact_rsi.program_opt import (
                PuctProgramArtifactProvider,
            )

            adapters["ARTIFACT:PROGRAM"] = ArtifactEngineAdapter(
                "PROGRAM",
                PuctProgramArtifactProvider(),
                model_resolver=model_resolver,
            )
        if paper_provider is None and "ARTIFACT:PAPER" not in adapters:
            # The implementation is intentionally owned by agent-core.  The
            # public ``paper_opt.provider`` module is only the Protocol; the
            # concrete implementation lives under the autoResearch tree provider.
            from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.provider import (
                PaperArtifactProviderImpl,
            )

            paper_provider = PaperArtifactProviderImpl()
        if paper_provider is not None:
            adapters["ARTIFACT:PAPER"] = ArtifactEngineAdapter(
                "PAPER",
                paper_provider,
                model_resolver=model_resolver,
            )
    if harness_provider is not None:
        adapters["HARNESS"] = HarnessEngineAdapter(harness_provider)
    return adapters


__all__ = ["build_mock_rsi_adapters", "build_rsi_adapters"]
