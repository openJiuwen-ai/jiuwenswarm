"""Coverage for jiuwenswarm's product-facing layer on top of the RLAF-P
algorithm — which now lives in
``openjiuwen.dev_tools.tune.optimizer.prompt_search`` (agent-core). The
algorithm itself is tested there; this only covers what's still jiuwenswarm's
own code: the ``TaskSpec`` -> ``PromptTaskSpec`` conversion, and that
``PromptOptimizer`` correctly wires jiuwenswarm-supplied collaborators into
the shared optimizer and returns its result unchanged.
"""

import asyncio
from dataclasses import replace

from openjiuwen.dev_tools.tune.optimizer.prompt_search.drift import NullDriftJudge
from openjiuwen.dev_tools.tune.optimizer.prompt_search.environment import CallableEnvironment
from openjiuwen.dev_tools.tune.optimizer.prompt_search.memory import JsonlPromptMemory
from openjiuwen.dev_tools.tune.optimizer.prompt_search.models import (
    OptimizationResult as SharedOptimizationResult,
    PromptCandidate,
)
from openjiuwen.dev_tools.tune.optimizer.prompt_search.policy import PolicyRequest, PromptPolicy
from openjiuwen.dev_tools.tune.optimizer.prompt_search.reward import CompositeReward, CustomReward

from jiuwenswarm.symphony.optimization import (
    OptimizationResult,
    PromptOptimizer,
    TaskCase,
    TaskSpec,
    default_optimization_config,
    to_prompt_task_spec,
)


def test_to_prompt_task_spec_converts_fields():
    task = TaskSpec(
        objective="summarize tickets",
        cases=[
            TaskCase(input="a", expected="A"),
            TaskCase(input="b", expected="B", hidden=True),
        ],
        constraints=["be brief"],
        base_prompt="Summarize.",
        metadata={"source": "test"},
    )

    converted = to_prompt_task_spec(task)

    assert converted.objective == "summarize tickets"
    assert converted.base_prompt == "Summarize."
    assert converted.constraints == ["be brief"]
    assert converted.metadata == {"source": "test"}
    assert len(converted.visible_cases) == 1
    assert len(converted.hidden_cases) == 1
    assert converted.visible_cases[0].input_text == "a"
    assert converted.visible_cases[0].expected_text == "A"
    assert converted.hidden_cases[0].input_text == "b"


def test_optimization_result_is_the_shared_type():
    # jiuwenswarm doesn't redefine OptimizationResult anymore — it's a
    # re-export of the agent-core type the optimizer actually returns.
    assert OptimizationResult is SharedOptimizationResult


class _VersionPolicy(PromptPolicy):
    async def generate(self, request: PolicyRequest):
        n = request.iteration
        return [
            PromptCandidate(prompt=f"v{n}", rationale=f"iter {n}")
            for _ in range(request.num_candidates)
        ]


def _runner(system_prompt: str, case_input: str) -> str:
    return "- item\n" * int(system_prompt[1:])


def _quality(execution, task) -> float:
    outs = execution.visible_results
    scores = [min(1.0, len(r.output.splitlines()) / 4) for r in outs]
    return sum(scores) / max(1, len(scores))


def test_prompt_optimizer_delegates_to_shared_optimizer(tmp_path):
    config = replace(
        default_optimization_config(),
        candidate_prompts=2,
        max_iterations=6,
        drift_penalty=0.0,
    )
    optimizer = PromptOptimizer(
        config,
        policy=_VersionPolicy(),
        environment=CallableEnvironment(_runner),
        reward_model=CompositeReward(
            [CustomReward("quality", _quality)],
            {"quality": 1.0},
            min_correctness=0.0,
            drift_penalty=0.0,
        ),
        drift_judge=NullDriftJudge(),
        memory=JsonlPromptMemory(tmp_path),
    )
    task = TaskSpec(objective="produce a 4-item list", cases=[TaskCase(input="x")])

    result = asyncio.run(optimizer.optimize(task))

    assert isinstance(result, SharedOptimizationResult)
    assert result.best_prompt == "v4"
    assert result.best_score == 1.0
    assert result.converged is True

    # the delegate's own memory backend received the persisted best prompt
    memory = JsonlPromptMemory(tmp_path)
    records = memory.search_similar(to_prompt_task_spec(task), top_k=5)
    assert any(r.prompt == result.best_prompt for r in records)
