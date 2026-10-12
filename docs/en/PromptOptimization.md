# Prompt Optimization (RLAF-P)

A runtime prompt optimizer available to JiuwenSwarm agents. It improves a **system
prompt** for a repeatable task through an RL-style feedback loop — **no model
weights are trained**. A Policy (LLM) proposes candidate prompts, an Environment
executes them, a Reward model scores the results, a Drift judge keeps the objective
fixed, and a compressed optimization history steers the next round.

The algorithm lives in agent-core, not jiuwenswarm: it's a `BaseOptimizer` in
`openjiuwen.dev_tools.tune.optimizer.prompt_search`, built on the same `Case` /
`DefaultEvaluator` / `Model` scaffolding as agent-core's other prompt-tuning
optimizers (`InstructionOptimizer`, `ExampleOptimizer`, `JointOptimizer`), so any
product built on agent-core can use it — not only jiuwenswarm. jiuwenswarm's own
`jiuwenswarm/symphony/optimization/` package is the product-facing layer on top of
it: the task shape agents actually call with (`TaskSpec`/`TaskCase`), config
loading, wiring in jiuwenswarm's configured LLM and memory backends, and the tool /
rails / extension RPC below. Symphony itself stays retrieval-only, consistent with
its role everywhere else in the framework — this package sits beside it, not inside
its skill-selection pipeline.

- Algorithm: [`openjiuwen/dev_tools/tune/optimizer/prompt_search/`](https://gitcode.com/openJiuwen/agent-core) (agent-core)
- Product layer: [`jiuwenswarm/symphony/optimization/`](../../jiuwenswarm/symphony/optimization/)
- Extension: [`jiuwenswarm/extensions/optimization/`](../../jiuwenswarm/extensions/optimization/)
- Tools: `optimize_prompt`, `list_pending_prompt_improvements`, `mark_prompt_improvement_applied`
- Rails: `PromptOptimizerPromptRail` (how to start one), `PromptOptimizerReviewRail` (surfaces unreviewed results)
- Config: `symphony.optimization` in `config.yaml`

---

## Architecture

```
TaskSpec (jiuwenswarm) ─▶ to_prompt_task_spec() ─▶ PromptTaskSpec (agent-core)
                                                          │
                                             PromptSearchOptimizer.optimize()
          │
          ├─ PromptPolicy ........... generate N candidate system prompts (LLM)
          │     └─ OptimizationHistory + HistoryCompressor  (textual "policy gradient")
          ├─ PromptEnvironment ...... execute each candidate (LLM / workflow / agent / callable)
          ├─ RewardModel ............ CompositeReward = Σ wᵢ·componentᵢ → scalar + breakdown
          │     ├─ Correctness (via BaseEvaluator / DefaultEvaluator)  ├─ Latency / TokenUsage / Cost
          │     └─ Completeness / StructuredValidation / Custom
          ├─ DriftJudge ............. deviation(objective, candidate) → reward penalty
          ├─ ConvergenceDetector .... moving average / variance / no-improve-K
          └─ PromptMemory ........... store & retrieve prior optimizations
          ▼
     OptimizationResult (best prompt + full trace) ─▶ PromptMemory
```

Every collaborator is an **ABC with a swappable default**, defined in agent-core's
`prompt_search` package. jiuwenswarm's `OptimizerRuntimeFactory`
(`jiuwenswarm/symphony/optimization/factory.py`) builds the defaults from
`symphony.optimization` config — it wires jiuwenswarm's LLM client and memory
backend into agent-core's collaborator types, it doesn't reimplement them. The loop
itself emits a JSONL run log the same way `SymphonyScoreBuilder.build` does.

| Component | Interface (agent-core) | Default (agent-core) | jiuwenswarm supplies |
|---|---|---|---|
| Policy | `PromptPolicy` | `LLMPromptPolicy` | a `Model` resolved from jiuwenswarm's default LLM config |
| Environment | `PromptEnvironment` | `LLMEnvironment` (+ `WorkflowEnvironment`, `CallableEnvironment`) | same |
| Reward | `RewardModel` / `RewardComponent` | `CompositeReward` + built-ins | a `DefaultEvaluator` for `CorrectnessReward` |
| Drift | `DriftJudge` | `LLMDriftJudge` | same `Model` |
| Memory | `PromptMemory` | `JsonlPromptMemory` | `ExperienceBankPromptMemory` (jiuwenswarm's own FAISS-backed implementation of the same interface, in `memory/experience_backend.py`) |
| History | `HistoryCompressor` | LLM buckets | same `Model` |

Also available directly through agent-core, independent of jiuwenswarm: a new
`BaseOptimizer` bound to a `Trainer`/agent's `LLMCall` generates multiple candidate
prompts per round instead of one, and `Trainer.search_prompt_candidates()` searches
across all of them — see agent-core's own tuning docs.

---

## Quick start (Python)

```python
import asyncio
from jiuwenswarm.symphony.optimization import optimize_prompt, TaskSpec, TaskCase

task = TaskSpec(
    objective="Summarize a customer support ticket into at most 3 action items.",
    constraints=["Output a markdown bullet list", "At most 3 bullets"],
    cases=[
        TaskCase(input="My invoice is wrong and the app keeps crashing on login.",
                 expected="- Fix invoice\n- Investigate login crash"),
        TaskCase(input="Password reset email never arrives; also dark mode is broken.",
                 expected="- Fix password reset email\n- Fix dark mode", hidden=True),
    ],
)

result = asyncio.run(optimize_prompt(task))
print(result.best_score, result.best_prompt)
```

`optimize_prompt` resolves `symphony.optimization` config, builds defaults, and runs
the loop. Inject any collaborator to override it:

```python
result = await optimize_prompt(task, environment=my_workflow_env, reward_model=my_reward)
```

## Inside a workflow (tool + rail)

When `symphony.optimization.enabled: true`, the team **leader** gets:
- the `optimize_prompt` **tool** (`PromptOptimizerToolkit`), and
- the `PromptOptimizerPromptRail`, which injects guidance on when to call it.

The agent calls `optimize_prompt(objective=..., cases=[...])`; the tool dispatches to
the `optimizer.optimize` extension RPC and returns the best prompt + reward. This is
the requested flow — *Task → optimizer → candidates → parallel execution → reward →
prompt update → best prompt → continue workflow* — as native JiuwenSwarm pieces.

RPC methods: `optimizer.optimize`, `optimizer.status`, `optimizer.best_prompt`,
`optimizer.pending_improvements`, `optimizer.mark_applied`.

---

## Review queue: closing the discovery gap

A finished optimization only ever produces a *record* — nothing installs the
winning prompt as a teammate's live system prompt automatically. Left there,
a result found without a human watching (the leader decided on its own, or a
developer ran a batch optimization) is easy to lose track of.

Two pieces close that gap:

- **`PromptRecord.baseline_reward` / `.gain`** — every persisted record now
  remembers the best reward previously known for the same `objective` (`None`
  the first time), so `gain` (`reward - baseline_reward`) shows the actual
  improvement, not just an absolute score.
- **`PromptOptimizerReviewRail`** — a leader-only rail (mirrors
  `PromptOptimizerPromptRail`) that checks `PromptMemory.pending()` — records
  with a positive gain that nobody has confirmed applying yet — and, when any
  exist, injects a short summary into the leader's own context so it can
  proactively mention them to the user instead of the user needing to already
  know to ask.

Two more agent tools close the loop:

- `list_pending_prompt_improvements(threshold?)` — the same "review queue"
  the rail surfaces, callable on demand.
- `mark_prompt_improvement_applied(record_id)` — call once a human confirms a
  suggested prompt was actually installed somewhere; the record then drops out
  of `pending()` and stops being surfaced.

Nothing here auto-applies a prompt — the reward is a proxy signal, not a
substitute for a human reviewing the actual prompt text before it goes live.

---

## Configuration (`symphony.optimization`)

```yaml
symphony:
  optimization:
    enabled: false
    candidate_prompts: 5          # candidates generated per iteration
    max_iterations: 6
    parallel_execution: true      # execute candidates concurrently
    convergence_threshold: 0.01   # min reward gain counted as improvement
    convergence_window: 3         # stop after K iterations with no improvement
    drift_penalty: 0.5            # weight on semantic deviation from the objective
    min_correctness: 0.5          # hard gate against reward hacking
    memory_enabled: true
    memory_dir: ""                # default <workspace>/symphony/optimization/prompt_kb
    policy_temperature: 0.9
    reward_weights:               # component weights (already-bounded metrics)
      correctness: 1.0
      completeness: 0.3
      latency: 0.1
      token_usage: 0.1
      cost: 0.0
      structured_validation: 0.0
    models:
      policy_model: ""            # "" => JiuwenSwarm default model
      environment_model: ""
      judge_model: ""             # correctness + drift judges
    embedding:                    # enables FAISS prompt memory when set
      base_url: ""
      api_key: ""
      model: ""
      model_name: ""
      dimension:
```

A component with weight `0` is skipped entirely. If `embedding` is unset, memory
falls back to a dependency-light JSONL store with lexical retrieval.

---

## Extension points

Swap any implementation without touching the loop — pass it to `optimize_prompt`
or override a method on `OptimizerRuntimeFactory`.

**Custom reward metric:**

```python
from openjiuwen.dev_tools.tune.optimizer.prompt_search.reward import (
    RewardComponent, CompositeReward, CorrectnessReward,
)

class KeywordReward(RewardComponent):
    name = "keyword"
    def __init__(self, keyword): self._kw = keyword
    async def score(self, execution, task):
        outs = execution.visible_results
        return sum(self._kw in r.output for r in outs) / max(1, len(outs))

reward = CompositeReward(
    [CorrectnessReward(evaluator), KeywordReward("action")],
    {"correctness": 1.0, "keyword": 0.5},
    min_correctness=0.5, drift_penalty=0.5,
)
result = await optimize_prompt(task, reward_model=reward)
```

**Custom environment** — implement `PromptEnvironment.execute(candidate, task)`, or
wrap any coroutine with `WorkflowEnvironment` / `CallableEnvironment` to score
candidates against a real JiuwenSwarm workflow, agent, plugin, or benchmark.

**Custom policy / drift judge / memory** — subclass `PromptPolicy`, `DriftJudge`, or
`PromptMemory` respectively.

---

## Anti-reward-hacking

The optimizer will not maximize one metric while destroying quality:

- **Min-correctness gate** — reward is capped at correctness when correctness is below
  `min_correctness`, so latency/token gains can't buy a bad answer a high score.
- **Hidden validation cases** (`TaskCase(hidden=True)`) — visible-vs-hidden correctness
  gaps are detected and penalized as overfitting.
- **Drift penalty** — an LLM judge scores semantic deviation from the objective; large
  deviations subtract from the reward.
- **Bounded, composite metrics** — built-in components are already in `[0, 1]`, so no
  single metric runs away; enable `normalize=True` only for custom unbounded metrics.

---

## Best practices

- Provide 3–6 evaluation cases; mark 1–2 as `hidden`.
- Keep `drift_penalty ≥ 0.3` and always keep a correctness weight.
- Start with `candidate_prompts=5`, `max_iterations=6`; raise only if reward is still
  climbing at the last iteration.
- Review the JSONL run log (`optimizer.status`) before promoting a prompt — it records
  candidate prompts, outputs, reward breakdowns, drift, and convergence metrics.

## Example

A runnable, network-free walkthrough that shows reward climbing across iterations is
in [`examples/optimize_summarizer.py`](examples/optimize_summarizer.py).
