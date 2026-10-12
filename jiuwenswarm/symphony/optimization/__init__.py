"""RLAF-P runtime prompt optimizer for JiuwenSwarm.

An RL-style feedback loop that improves system prompts over repeated executions of
similar tasks — without training any weights. A Policy (LLM) proposes candidate
prompts, an Environment executes them, a RewardModel scores the results, a DriftJudge
guards the objective, and a compressed optimization history steers the next round.

The algorithm itself lives in
:mod:`openjiuwen.dev_tools.tune.optimizer.prompt_search` (agent-core) — this package
is jiuwenswarm's product-facing layer on top of it: the task shape (:class:`TaskSpec`),
config loading, the runtime factory that wires in jiuwenswarm's LLM and memory
backends, and the ``optimize_prompt`` entrypoint used by the tool/rails/extension RPC.
See docs/en/PromptOptimization.md.

Public entrypoint::

    from jiuwenswarm.symphony.optimization import optimize_prompt, TaskSpec, TaskCase

    result = await optimize_prompt(
        TaskSpec(objective="...", cases=[TaskCase(input="...", expected="...")])
    )
    print(result.best_prompt, result.best_score)
"""

from jiuwenswarm.symphony.optimization.config import (
    OptimizationConfig,
    default_optimization_config,
    load_optimization_config,
    optimization_config_from_dict,
)
from jiuwenswarm.symphony.optimization.factory import OptimizerRuntimeFactory
from jiuwenswarm.symphony.optimization.models import (
    Evaluation,
    Execution,
    IterationRecord,
    OptimizationResult,
    PromptCandidate,
    PromptRecord,
    RewardBreakdown,
    TaskCase,
    TaskSpec,
    to_prompt_task_spec,
)
from jiuwenswarm.symphony.optimization.optimizer import PromptOptimizer
from jiuwenswarm.symphony.optimization.service import default_run_log_path, optimize_prompt

__all__ = [
    "optimize_prompt",
    "default_run_log_path",
    "PromptOptimizer",
    "OptimizerRuntimeFactory",
    "OptimizationConfig",
    "load_optimization_config",
    "default_optimization_config",
    "optimization_config_from_dict",
    "TaskSpec",
    "TaskCase",
    "to_prompt_task_spec",
    "PromptCandidate",
    "Execution",
    "Evaluation",
    "RewardBreakdown",
    "IterationRecord",
    "OptimizationResult",
    "PromptRecord",
]
