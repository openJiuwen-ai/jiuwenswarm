"""Runnable, network-free example of the RLAF-P prompt optimizer.

Optimizes a SYSTEM PROMPT for a support-ticket summarizer and prints the reward
climbing across iterations. It is fully deterministic (no live model) so it always
runs: a stub Policy plays the role of the LLM prompt generator, and a deterministic
Environment simulates how prompt quality shapes the output. Swap in
``LLMPromptPolicy`` + ``LLMEnvironment`` (the defaults from ``optimize_prompt``) to
run it against a real model.

    python docs/en/examples/optimize_summarizer.py
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import replace

from openjiuwen.dev_tools.tune.optimizer.prompt_search.drift import NullDriftJudge
from openjiuwen.dev_tools.tune.optimizer.prompt_search.environment import CallableEnvironment
from openjiuwen.dev_tools.tune.optimizer.prompt_search.memory import JsonlPromptMemory
from openjiuwen.dev_tools.tune.optimizer.prompt_search.models import PromptCandidate
from openjiuwen.dev_tools.tune.optimizer.prompt_search.policy import PolicyRequest, PromptPolicy
from openjiuwen.dev_tools.tune.optimizer.prompt_search.reward import CompositeReward, CustomReward

from jiuwenswarm.symphony.optimization import (
    PromptOptimizer,
    TaskCase,
    TaskSpec,
    default_optimization_config,
)

logger = logging.getLogger(__name__)

# The "good practices" a strong summarizer prompt should contain. The stub policy
# discovers them incrementally; the environment rewards prompts that include them.
PRACTICES = [
    "use a markdown bullet list",
    "limit the summary to at most 3 items",
    "phrase each item as a concrete action",
    "be concise",
]


class StubPolicy(PromptPolicy):
    """Stands in for the LLM policy: proposes prompts that build on the best-so-far,
    each adding one not-yet-used practice (the 'textual policy gradient' in miniature).
    """
    async def generate(self, request: PolicyRequest) -> list[PromptCandidate]:
        best = request.history.best_prompt or "Summarize the support ticket."
        present = [p for p in PRACTICES if p in best]
        missing = [p for p in PRACTICES if p not in best]
        candidates: list[PromptCandidate] = []
        for practice in missing[: request.num_candidates]:
            prompt = best.rstrip(".") + ". Also " + practice + "."
            candidates.append(PromptCandidate(prompt=prompt, rationale=f"add: {practice}"))
        if not candidates:  # everything already included → propose the best as-is
            candidates.append(PromptCandidate(prompt=best, rationale="no further practice to add"))
        return candidates


def environment_runner(system_prompt: str, case_input: str) -> str:
    """Deterministic backend: output quality depends on which practices the prompt has."""
    bullets = "use a markdown bullet list" in system_prompt
    limit3 = "limit the summary to at most 3 items" in system_prompt
    action = "phrase each item as a concrete action" in system_prompt

    verb = "Fix" if action else "Look at"
    items = [f"{verb} invoice", f"{verb} login crash", f"{verb} dark mode", f"{verb} emails"]
    items = items[:3] if limit3 else items
    if bullets:
        return "\n".join(f"- {i}" for i in items)
    return "; ".join(items)


def structure_reward(execution, task) -> float:
    outs = execution.visible_results
    return sum(r.output.strip().startswith("- ") for r in outs) / max(1, len(outs))


def brevity_reward(execution, task) -> float:
    scores = []
    for r in execution.visible_results:
        n = max(1, len([line for line in r.output.splitlines() if line.strip()]))
        scores.append(1.0 if n <= 3 else 3 / n)
    return sum(scores) / max(1, len(scores))


def action_reward(execution, task) -> float:
    outs = execution.visible_results
    return sum("Fix" in r.output for r in outs) / max(1, len(outs))


async def main() -> None:
    config = replace(
        default_optimization_config(),
        candidate_prompts=3,
        max_iterations=6,
        drift_penalty=0.0,
        memory_enabled=True,
    )
    task = TaskSpec(
        objective="Summarize a customer support ticket into a short list of action items.",
        constraints=["Output a markdown bullet list", "At most 3 items"],
        base_prompt="Summarize the support ticket.",
        cases=[
            TaskCase(input="Invoice is wrong; app crashes on login; dark mode broken."),
            TaskCase(input="Password reset email never arrives; billing double-charged.", hidden=True),
        ],
    )

    reward = CompositeReward(
        [
            CustomReward("structure", structure_reward),
            CustomReward("brevity", brevity_reward),
            CustomReward("action", action_reward),
        ],
        {"structure": 1.0, "brevity": 0.6, "action": 0.8},
        min_correctness=0.0,   # no LLM correctness judge in this offline demo
        drift_penalty=0.0,
        normalize=False,
    )

    with tempfile.TemporaryDirectory() as tmp:
        optimizer = PromptOptimizer(
            config,
            policy=StubPolicy(),
            environment=CallableEnvironment(environment_runner),
            reward_model=reward,
            drift_judge=NullDriftJudge(),
            memory=JsonlPromptMemory(tmp),
        )
        result = await optimizer.optimize(task)

    logger.info("=== Reward over iterations ===")
    for it in result.iterations:
        obs = "; ".join(it.observations[:2])
        logger.info("iter %s: best=%.3f  (%s)", it.iteration, it.best_score, obs)
    logger.info("converged: %s - %s", result.converged, result.convergence_reason)
    logger.info("=== Best prompt (reward %.3f) ===\n%s", result.best_score, result.best_prompt)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
