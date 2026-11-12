# A speedup task, in full

The commonest shape: a reference implementation is the seed, and a candidate is scored on how much faster it computes the same answer.

**Where this comes from.** It is the AlgoTune setup, ported to this provider and then run: eight AlgoTune tasks through this engine, against a published upstream run of the same benchmark. That is why it is this specific — every rule below is one that was got wrong first. Two of the eight beat upstream's own numbers by a wide margin (a 2-D convolution at 345x against upstream's 102x, a PSD cone projection at 25x against 4x) and the fixes that got them there were: the normalisation direction, the repair budget, and the per-evaluation timeout.

**What is a rule and what is a dial.** The card's three marked keys, and the timing rules about correctness and warm-up, are the task — get them wrong and the search optimises the wrong thing or measures noise. The counts (2 problems per shard, 3 timed runs, the 10x cut-off, a reference at ~100 ms) are AlgoTune's conventions, chosen so one evaluation stays inside its timeout; change them when your problem argues for it, and change them deliberately rather than by omission.


**The seed is the reference implementation, unoptimised.** Its slowness is what the task measures against. Optimising it before the run spends the search space.

**The evaluator carries its own copy of that reference**, and the two must be the same algorithm. It cannot time "the seed": by the second expansion the seed is gone, replaced by the candidate under test. So the reference is written into the evaluator, and the seed is a copy of it — which is also what makes the first check below meaningful.

**`entrypoint` is yours to name**, as everywhere else; the card below says `solver.py` because AlgoTune's harness imports that. A folder whose seed is `candidate.py` writes `candidate.py`, and nothing about this section changes.

**The card**, over the Step 2 template:

```json
{"entrypoint": "solver.py", "iterations": 45, "workers": 3, "max_tokens_per_call": 32000,
 "options": {"c_puct": 2.5, "prior_exponent": 2, "repair_attempts": 4,
             "completion_timeout": 900, "mode": "async", "staleness": "full", "async_ratio": 1},
 "scorecard": {"aggregate": "weighted_sum", "constraints": [], "solvedThreshold": 0.999, "criteria": [{
   "id": "speedup", "name": "speedup over the reference", "direction": "maximize", "weight": 1.0,
   "normalize": {"kind": "relative_to_baseline"},
   "measure": {"kind": "custom_script", "timeoutSeconds": 300,
               "split": {"gateShards": 4, "rolloutShards": 3, "testShards": 3, "seed": 3}}}]}}
```

Three of those are the task, not taste. `direction: maximize` with `relative_to_baseline` — get it backwards and a candidate running at half the reference's speed scores 0.66 and is adopted as the best node. `repair_attempts: 4` — the winning direction is usually a compiled one (numba, Cython), whose first draft rarely compiles, and 2 attempts abandon it before it is reached. `c_puct: 2.5` — it is what sends the search back to a node that scored *below* the seed but rated its own approach high, which is where the large wins came from.

**The evaluator's timing rules**, all of them:

* Each shard index builds 2 fresh problems of its own; no problem is shared between shards.
* Warm up on the previous problem, untimed, before timing either side.
* Time the minimum of 3 runs, reference and candidate alike.
* Check correctness before timing. One wrong answer means no speedup at all — `valid: false`, with which problem and how it was wrong in `error`.
* A candidate more than 10x slower than the reference on its first run is not timed again.
* `metrics` carries one key, `speedup`, the mean over the problems.
* `error` carries a readable report even when valid: the speedup, valid/invalid counts, per problem timings, and a `line_profiler` table of the parent's `solve` when that package is importable. That report is the whole of `${feedback}`.

**`statement`** names the packages a candidate may use and says outright whether `numba` is installed in the interpreter that runs candidates — a search told nothing about it will not reach for the thing that wins.

**`prompts/`**: `mutation.md` over `${statement}` `${feedback}` `${parent_code}` `${reply_format}`, ending with a demand for the entrypoint's complete contents in one block and a prohibition on calling or reconstructing the reference implementation to fake a speedup. `repair.md` over `${code}` `${error}`, keeping the approach and fixing only the defect. `prior.md` over `${prompt}`, adding the PROMISE line — asking how fast this *approach* could become once tuned, never how fast this draft is.

**Sizing.** Pick the problem size at which the reference takes roughly 100 ms. Below that the timing noise is the measurement; far above it, one evaluation outgrows `timeoutSeconds`. Check `timeoutSeconds` against `workers` evaluations sharing one CPU, not against a lone run.

**Two extra checks before handing it over**, on top of the ones in SKILL.md:

1. **The seed scores about 1.0.** It *is* the reference; any other number means the timing or the problem generation is wrong.
2. **A hollowed-out seed comes back `valid: false`**, not a crash — bodies replaced by `pass`, so `solve` returns `None`. This is the probe's own damaged copy, and the case an evaluator written only against exceptions dies on.
