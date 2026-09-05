"""Thin jiuwenswarm-facing wrapper around agent-core's prompt-search optimizer.

The RLAF-P loop itself — Policy proposes candidates, Environment executes them,
RewardModel scores them, DriftJudge guards the objective, a compressed history
steers the next round — lives in
:class:`openjiuwen.dev_tools.tune.optimizer.prompt_search.optimizer.PromptSearchOptimizer`.
This class only translates jiuwenswarm's :class:`TaskSpec` into the shared
``PromptTaskSpec`` and forwards jiuwenswarm-resolved collaborators into it; see
docs/en/PromptOptimization.md for the full architecture.
"""

from __future__ import annotations

from openjiuwen.dev_tools.tune.optimizer.prompt_search.drift import DriftJudge
from openjiuwen.dev_tools.tune.optimizer.prompt_search.environment import PromptEnvironment
from openjiuwen.dev_tools.tune.optimizer.prompt_search.history import HistoryCompressor
from openjiuwen.dev_tools.tune.optimizer.prompt_search.memory import NullPromptMemory, PromptMemory
from openjiuwen.dev_tools.tune.optimizer.prompt_search.optimizer import PromptSearchOptimizer
from openjiuwen.dev_tools.tune.optimizer.prompt_search.policy import PromptPolicy
from openjiuwen.dev_tools.tune.optimizer.prompt_search.reward import RewardModel
from openjiuwen.dev_tools.tune.optimizer.prompt_search.run_log import NullRunLogger, OptimizerRunLogger

from jiuwenswarm.symphony.optimization.config import OptimizationConfig
from jiuwenswarm.symphony.optimization.models import OptimizationResult, TaskSpec, to_prompt_task_spec


class PromptOptimizer:
    """Runs one RLAF-P optimization for a jiuwenswarm :class:`TaskSpec`."""

    def __init__(
        self,
        config: OptimizationConfig,
        *,
        policy: PromptPolicy,
        environment: PromptEnvironment,
        reward_model: RewardModel,
        drift_judge: DriftJudge,
        memory: PromptMemory | None = None,
        history_compressor: HistoryCompressor | None = None,
        run_logger: OptimizerRunLogger | None = None,
    ) -> None:
        self._delegate = PromptSearchOptimizer(
            policy=policy,
            environment=environment,
            reward_model=reward_model,
            drift_judge=drift_judge,
            memory=memory or NullPromptMemory(),
            history_compressor=history_compressor,
            run_logger=run_logger or NullRunLogger(),
            candidate_prompts=config.candidate_prompts,
            max_iterations=config.max_iterations,
            parallel_execution=config.parallel_execution,
            convergence_threshold=config.convergence_threshold,
            convergence_window=config.convergence_window,
            drift_penalty=config.drift_penalty,
            min_correctness=config.min_correctness,
            policy_temperature=config.policy_temperature,
        )

    async def optimize(self, task: TaskSpec) -> OptimizationResult:
        return await self._delegate.optimize(to_prompt_task_spec(task))


__all__ = ["PromptOptimizer"]
