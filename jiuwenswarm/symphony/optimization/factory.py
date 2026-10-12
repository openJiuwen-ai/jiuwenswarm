"""Default-implementation factory for the prompt optimizer.

Mirrors :class:`jiuwenswarm.symphony.build.ScoreBuildRuntimeFactory`: one place that
constructs the default Policy / Environment / RewardModel / DriftJudge / Memory /
compressor from :class:`OptimizationConfig`, so callers can override exactly one
collaborator and inherit sensible defaults for the rest.

Every collaborator type (``PromptPolicy``, ``PromptEnvironment``, ``RewardModel``,
``DriftJudge``, ``PromptMemory``, ``HistoryCompressor``) comes from
``openjiuwen.dev_tools.tune.optimizer.prompt_search`` — this factory's job is only
to wire jiuwenswarm's configured LLM and memory backend into them.
"""

from __future__ import annotations

import logging

from openjiuwen.core.foundation.llm import Model
from openjiuwen.dev_tools.tune.evaluator.evaluator import DefaultEvaluator
from openjiuwen.dev_tools.tune.optimizer.prompt_search.drift import (
    DriftJudge,
    LLMDriftJudge,
    NullDriftJudge,
)
from openjiuwen.dev_tools.tune.optimizer.prompt_search.environment import (
    LLMEnvironment,
    PromptEnvironment,
)
from openjiuwen.dev_tools.tune.optimizer.prompt_search.history import HistoryCompressor
from openjiuwen.dev_tools.tune.optimizer.prompt_search.memory import (
    JsonlPromptMemory,
    NullPromptMemory,
    PromptMemory,
)
from openjiuwen.dev_tools.tune.optimizer.prompt_search.policy import LLMPromptPolicy, PromptPolicy
from openjiuwen.dev_tools.tune.optimizer.prompt_search.reward import (
    CompletenessReward,
    CompositeReward,
    CorrectnessReward,
    CostReward,
    LatencyReward,
    RewardModel,
    TokenUsageReward,
)

from jiuwenswarm.symphony.optimization.config import OptimizationConfig
from jiuwenswarm.symphony.optimization.llm_support import build_model, build_model_configs
from jiuwenswarm.symphony.optimization.models import TaskSpec

LOGGER = logging.getLogger(__name__)

# DefaultEvaluator judges every case against a "label" text. When a case has no
# fixed expected answer, the task objective stands in for it instead (see
# CorrectnessReward's own docstring in agent-core) — this hint tells the judge
# model to treat that substitution as "does the answer satisfy the objective",
# not "does it match verbatim".
_CORRECTNESS_METRIC_HINT = (
    "If the expected answer text is actually the task objective (no fixed expected "
    "value was available for this case), judge whether the model answer plausibly and "
    "correctly fulfills that objective rather than requiring a verbatim match."
)


class OptimizerRuntimeFactory:
    """Build default optimizer collaborators from an :class:`OptimizationConfig`."""

    def __init__(self, config: OptimizationConfig) -> None:
        self._config = config
        self._policy_model: Model | None = None
        self._environment_model: Model | None = None
        self._judge_model: Model | None = None

    # -- lazily-built shared clients -----------------------------------------

    def policy_model(self) -> Model:
        if self._policy_model is None:
            self._policy_model = build_model(
                self._config.models.policy_model, temperature=self._config.policy_temperature
            )
        return self._policy_model

    def environment_model(self) -> Model:
        if self._environment_model is None:
            self._environment_model = build_model(self._config.models.environment_model)
        return self._environment_model

    def judge_model(self) -> Model:
        if self._judge_model is None:
            self._judge_model = build_model(self._config.models.judge_model)
        return self._judge_model

    # -- collaborators --------------------------------------------------------

    def policy(self) -> PromptPolicy:
        return LLMPromptPolicy(self.policy_model())

    def environment(self) -> PromptEnvironment:
        return LLMEnvironment(
            self.environment_model(),
            parallel=self._config.parallel_execution,
            max_concurrency=self._config.candidate_prompts,
        )

    def reward_model(self, task: TaskSpec) -> RewardModel:
        del task  # kept for signature stability; DefaultEvaluator judges every case uniformly
        weights = self._config.reward_weights
        request_config, client_config = build_model_configs(self._config.models.judge_model)
        evaluator = DefaultEvaluator(request_config, client_config, metric=_CORRECTNESS_METRIC_HINT)
        max_concurrency = max(1, self._config.candidate_prompts)
        components = [
            CorrectnessReward(evaluator, max_concurrency=max_concurrency),
            CompletenessReward(),
            LatencyReward(),
            TokenUsageReward(),
            CostReward(),
        ]
        return CompositeReward(
            components,
            weights,
            min_correctness=self._config.min_correctness,
            drift_penalty=self._config.drift_penalty,
            normalize=False,
        )

    def drift_judge(self) -> DriftJudge:
        if self._config.drift_penalty <= 0:
            return NullDriftJudge()
        return LLMDriftJudge(self.judge_model())

    def history_compressor(self) -> HistoryCompressor:
        return HistoryCompressor(self.policy_model())

    def memory(self) -> PromptMemory:
        if not self._config.memory_enabled:
            return NullPromptMemory()
        directory = self._config.resolved_memory_dir
        embedding = self._config.embedding
        if embedding.base_url or embedding.model_name:
            backend = self._try_experience_backend(directory)
            if backend is not None:
                return backend
        return JsonlPromptMemory(directory)

    def _try_experience_backend(self, directory) -> PromptMemory | None:
        try:
            from jiuwenswarm.symphony.experience.bank import ExperienceBank
            from jiuwenswarm.symphony.experience.embed import EmbeddingClient
            from jiuwenswarm.symphony.optimization.memory.experience_backend import (
                ExperienceBankPromptMemory,
            )

            embedding = self._config.embedding
            embedder = EmbeddingClient(
                base_url=embedding.base_url or None,
                api_key=embedding.api_key,
                model=embedding.model,
                model_name=embedding.model_name,
                dimension=embedding.dimension,
            )
            bank = ExperienceBank(directory / "bank", embedder)
            return ExperienceBankPromptMemory(bank, directory)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "OptimizerRuntimeFactory: FAISS prompt memory unavailable, "
                "falling back to JSONL memory: %s",
                exc,
            )
            return None


__all__ = ["OptimizerRuntimeFactory"]
