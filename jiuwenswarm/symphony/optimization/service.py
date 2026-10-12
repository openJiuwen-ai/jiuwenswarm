"""High-level prompt-optimization service API.

Mirrors :func:`jiuwenswarm.symphony.orchestration.service.plan_from_score`: a single
async entrypoint that resolves config, assembles defaults via
:class:`OptimizerRuntimeFactory`, applies any caller overrides, and runs the loop.
Every collaborator is overridable, so this stays a thin wiring layer.
"""

from __future__ import annotations

from pathlib import Path

from openjiuwen.dev_tools.tune.optimizer.prompt_search.drift import DriftJudge
from openjiuwen.dev_tools.tune.optimizer.prompt_search.environment import PromptEnvironment
from openjiuwen.dev_tools.tune.optimizer.prompt_search.history import HistoryCompressor
from openjiuwen.dev_tools.tune.optimizer.prompt_search.memory import PromptMemory
from openjiuwen.dev_tools.tune.optimizer.prompt_search.policy import PromptPolicy
from openjiuwen.dev_tools.tune.optimizer.prompt_search.reward import RewardModel
from openjiuwen.dev_tools.tune.optimizer.prompt_search.run_log import OptimizerRunLogger

from jiuwenswarm.symphony.optimization.config import (
    OptimizationConfig,
    load_optimization_config,
)
from jiuwenswarm.symphony.optimization.factory import OptimizerRuntimeFactory
from jiuwenswarm.symphony.optimization.models import OptimizationResult, TaskSpec
from jiuwenswarm.symphony.optimization.optimizer import PromptOptimizer


async def optimize_prompt(
    task: TaskSpec,
    *,
    config: OptimizationConfig | None = None,
    policy: PromptPolicy | None = None,
    environment: PromptEnvironment | None = None,
    reward_model: RewardModel | None = None,
    drift_judge: DriftJudge | None = None,
    memory: PromptMemory | None = None,
    history_compressor: HistoryCompressor | None = None,
    run_log_path: str | Path | None = None,
    factory: OptimizerRuntimeFactory | None = None,
) -> OptimizationResult:
    """Optimize a system prompt for ``task``.

    Pass ``config`` to override ``symphony.optimization`` settings, or inject any
    collaborator (``policy``, ``environment``, ``reward_model``, ``drift_judge``,
    ``memory``, ...) to swap an implementation. Omitted collaborators are built by
    :class:`OptimizerRuntimeFactory` from ``config``.
    """

    resolved_config = config or load_optimization_config()
    runtime = factory or OptimizerRuntimeFactory(resolved_config)

    optimizer = PromptOptimizer(
        resolved_config,
        policy=policy or runtime.policy(),
        environment=environment or runtime.environment(),
        reward_model=reward_model or runtime.reward_model(task),
        drift_judge=drift_judge or runtime.drift_judge(),
        memory=memory or runtime.memory(),
        history_compressor=history_compressor or runtime.history_compressor(),
        run_logger=OptimizerRunLogger(run_log_path) if run_log_path else None,
    )
    return await optimizer.optimize(task)


def default_run_log_path(config: OptimizationConfig | None = None) -> Path:
    """Location of the JSONL run log for the most recent optimization run."""

    resolved = config or load_optimization_config()
    return resolved.resolved_memory_dir.parent / "runs" / "latest.jsonl"


__all__ = ["optimize_prompt", "default_run_log_path"]
