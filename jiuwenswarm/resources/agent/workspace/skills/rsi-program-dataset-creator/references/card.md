# Every key in `run/scorecard.json`

Open this while writing the card. The template is in SKILL.md Step 5; this file says what each key does and how to choose its value. A key that is not here is not read by the run.

### The run's size — the keys the caller used to pass

| key | write | how to choose |
|---|---|---|
| `iterations` | `20` | Expansions the run makes. **At least `4 × workers`**, or the first sweep forks only the root and the tree is flat. By search space: a known defect 4–6; swapping approach or restructuring 12–20; writing something from scratch 20+. Upstream's AlgoTune runs use 45 with 3 workers. The caller's own `max_iterations` is a placeholder this key overrides. |
| `workers` | `3` | Expansions in flight at once, bounded at 8. Model latency is the whole of a run's wall-clock and workers is what overlaps it: measured with a fixed-latency model, eight expansions took 20.4 s at 1 worker and 10.0 s at 4. The cost is timing fidelity — `workers` evaluations share the CPU, so a wall-clock metric is slower and noisier; size `timeoutSeconds` for the concurrent case. `1` when the metric is a timing you need clean, or the machine is small. |
| `max_tokens_per_call` | `32000` | Output ceiling per model call, and the injected model's own setting is **overridden** by this — the provider passes it on every call. A reasoning model bills hidden thinking against the same budget: at 8000 and 12000, models with thinking on spent the whole ceiling thinking and returned nothing, six times running. `32000` costs nothing when the model does not think (a ceiling, not a spend) and is the floor when it does. Whether it thinks at all is the model's configuration on the caller's side, not this file's. |
| `options.completion_timeout` | `900` | Seconds the engine waits for one model call before recording it as returned-nothing and moving on. The same number is handed to the model client as its timeout. A whole-program rewrite with thinking on is minutes: measured, 496 s for one prompt with thinking at its default, 26 s with it off. |

### Identity and protocol

| key | write | what it does |
|---|---|---|
| `statement` | the goal, in the task's own words | Rendered whole into the mutation prompt as `${statement}`. Free text and the widest field you have: a data preview, a worked example, the units the numbers are in, whether `numba` is installed — anything the model needs that has no slot of its own goes here. |
| `script` | the evaluator's source | Written beside the candidate on every evaluation. Empty is refused before the run. Step 3. |
| `hash` | `sha256:<task>-v<n>` | An identity for this card, recorded on every event. Change it when the scoring changes so two runs' numbers are never mistaken for one scale. |
| `entrypoint` | the seed file's name | What the evaluator is handed in `SCIENCE_AGENT_CANDIDATE`. Must exist under `seed/`. |
| `evaluator_file` | `evaluate.py` | What `script` is written as, beside the candidate. A non-`.py` name needs `evaluator_command`. |
| `evaluator_command` | `[]` | `[]` means the built-in shim runs `evaluator_file` as a Python script — the case for every Python evaluator. A non-Python evaluator names its own argv, e.g. `["sh", "evaluate.sh"]`. A string is refused: argv only. |
| `packages` | `[]` | Always empty. Any entry refuses the run with the `pip install` line for you to run yourself, so Step 0 is where the installing happened. (The key takes bare distribution names, optionally `==version`; anything else is refused outright.) |
| `reply_format` | `files` | The output protocol — its prompt instructions, its parser and the shape the parent is shown in, as one unit. `files` or `tagged`. Step 5. |

### `options` — how the search spends the budget

| key | write | how to choose |
|---|---|---|
| `c_puct` | `1.0` | PUCT's exploration constant. Higher weights unexplored and low-visit nodes against the current best. `1.0` when the gate ordering is trustworthy. `2.5` (upstream's AlgoTune value) when the score is coarse or noisy, or candidates keep landing on the same number — measured there, it is what sends the search back to a node that scored *below* the seed but rated its own approach high, which is where the 58x and 107x programs came from. Above 10 is refused. |
| `prior_exponent` | `0` | How sharply the model's own rating of its proposal bends `P(s, a)`. `0` is upstream's uniform prior and the request is not made. `2` when the failure you expect is a whole-mechanism rewrite: the prompt then ends with the PROMISE request (`prompts/prior.md` may reword it) and a rating of 8 becomes 2.12x the exploration term, a 2 becomes 0.13x — aiming the budget rather than widening it. Above 4 is refused. |
| `repair_attempts` | `2` | How many times a candidate that failed is handed back with its error for a fix before the direction is abandoned. Each attempt is a model call plus a rollout evaluation. `4` when the winning direction compiles something — numba, Cython, a C extension: the first draft of a compiled solver usually does not compile, and two attempts abandon that direction before it is reached. Upstream's numba wins on AlgoTune came with 4. |
| `completion_timeout` | `900` | Above. |
| `mode` | `async` | `serial`, `sync` (rounds with a barrier) or `async` (no barrier). **Write `async` when `workers > 1`, `serial` when `workers` is 1.** `serial` with several workers is not refused — it logs a warning and then runs one expansion at a time, so the workers you paid for are silently unused. An unknown value is refused. |
| `staleness` | `full` | What to do with a proposal made against a best node the merger has since replaced: `full` keeps it (the tree is append-only); `guarded` and `reflective` are the conservative variants. |
| `async_ratio` | `1` | In `async` mode, how far a worker may run ahead of the merged tree. `1` is upstream's. |

### `scorecard` — the scoring itself

| key | write | what it does |
|---|---|---|
| `aggregate` | `weighted_sum` | Or `weighted_geomean` when no dimension may be traded away — a geomean is dragged to zero by any criterion near zero. |
| `constraints` | `[]` | Hard vetoes evaluated on the raw metrics; a candidate that trips one is invalid with that constraint named. Usually empty. |
| `solvedThreshold` | `0.999` | The score at which the task counts as solved, and the ceiling the probe refuses a seed for reaching. Lower it when a perfect score is genuinely reachable and you want the run stopped earlier; it also feeds the headroom refusal, which needs the seed to sit at least a quarter of one damage signal below this. |
| `criteria[0].id` | the metric's key | **The key your evaluator writes under `metrics`.** The first criterion is the one the search ranks on; later ones are normalized and aggregated into the score but do not choose the parent. |
| `criteria[].name` | a label | Shown in reports. |
| `criteria[].direction` | `maximize` / `minimize` | Read by `clamp` and by `relative_to_baseline` to turn the ratio the right way — a speedup and an error are opposite metrics and the seed sits at 0.5 either way. |
| `criteria[].weight` | `1.0` | Its share of the aggregate, as a fraction of the weights' sum. |
| `criteria[].normalize` | `{"kind": "identity"}` | How the raw number becomes higher-is-better in `[0, 1]`. Table below. |
| `measure.kind` | `custom_script` | The only mode this engine scores by: anything else is refused before the run, and so is a card whose criteria do not all name the same kind. |
| `measure.timeoutSeconds` | `90` | Wall-clock for **one evaluation** — the whole evaluator run over its shards, reference and candidate included, compilation included. With `workers: 3` three of these share the CPU, so size it for that: a solver that takes 20 s alone takes 60 s with company. Too short kills evaluations and the loop with them. |
| `measure.split.gateShards` | `5` | Shards every candidate is scored on — the number the tree ranks and selects by. **At least 4**, or the run is refused. Deterministic scoring 8–12; anything with randomness 16–24. |
| `measure.split.rolloutShards` | `3` | Shards a proposal is tried on first. Four or more when you can afford it, never one, and no more than the gate. |
| `measure.split.testShards` | `3` | Never take part in the search; read once at the end. 4–8 is plenty. |
| `measure.split.seed` | `4` | Seeds the permutation that assigns case ids to slots, so every role draws from across the whole space rather than from the tail of a list an author wrote easy-first. |

Slots are positional and their roles are fixed in order — rollout, then gate, then test — which is what keeps the engine's held-out set the same shards the gate scores on. Your evaluator receives the slot indices in `SCIENCE_AGENT_SHARDS` and builds case `i` for each.

### `normalize.kind`

Choose it by the **magnitude** of your raw metric, not by its direction. All four turn a measurement into higher-is-better in `[0, 1]`; what separates them is where they have any resolution left. An unknown kind is refused before the run.

| your raw metric | use | why |
|---|---|---|
| already in `[0, 1]`, higher better | `identity` | nothing to do |
| any magnitude, either direction | `relative_to_baseline` | `ratio/(1+ratio)` with the ratio turned by `direction`, so the seed lands on exactly 0.5 and improvements climb from there — a 2x speedup and a halved error both score 0.667 |
| lower-is-better, and around 1 | `reciprocal` | `1/(1+x)` only spreads values when `x` is near 1 |
| bounded, known floor and ceiling | `clamp` with `lo`/`hi` | maps that window onto `[0, 1]` |

`reciprocal` is the one that goes wrong quietly, in both directions. Measured: a probe count around 800 normalised to 0.00125 and every improvement stayed within a thousandth of the floor; an absolute error around 0.0085 normalised to 0.9916 and six different algorithms tied at 0.99583 with the whole run living in the last 0.008. Same function, opposite ends, no resolution either time.

`relative_to_baseline` needs the root's raw measurements to survive a resume, and the tree snapshot carries them. A run directory from before that was written is refused on resume with "this run's seed event does not carry the root's measurements"; that run has to start again.

### What the provider build in front of you reads

Keys were added to this card over time, and a provider built before a key existed ignores it silently. Write them all regardless — the folder is the record of what you decided — and know which ones an older build drops:

| key | read since | an older build does instead |
|---|---|---|
| `options.repair_attempts`, `measure.timeoutSeconds` | the AlgoTune fixes (`fix(rsi): three program_opt defects …`) | 2 repair attempts, 60 s per evaluation — which kills any evaluation your card sized above 60 s |
| `iterations`, and the folder layout itself | the task-folder change (provider resolves `seed/` and `run/`, the card's `iterations` sets the run length) | reads `<run_dir>/scorecard.json` and the caller's `max_iterations`; a folder path as `artifact_path` fails with `FileNotFoundError` |

If a run refuses or behaves as the right-hand column says, the build is the older one, and the fix is on the deployment's side, not in the folder.
