---
name: program-evolution-design
description: 'Use for program evolution — improving a working program by repeated search against a measured score, rather than by one edit. Covers both halves: deciding what is measured and how a candidate program is scored, and writing that decision into the task folder the run reads (`task.json`, `seed/`, `run/scorecard.json`, every parameter set). Triggers on "make this code faster", "search for a better implementation", staging or debugging a task folder, writing a scorecard or an evaluator, and on a `PROBE_REFUSED`, `SEARCH_FAILED` or `ARTIFACT_BUNDLE_INCOMPLETE` a run came back with.'
metadata:
  version: 3.0.0
---

# Designing and staging a program evolution

A search rewrites one program dozens of times and keeps what scores higher. It succeeds or fails on whether the scoring can tell a good candidate from a bad one — scoring that cannot shows up as a flat run, not as an error. This skill covers deciding that, and writing it into the folder the run is handed.

**The subject is a program**: source the machine executes, scored by running it. Every candidate is a rewrite of that program, and every score comes from an evaluator that runs it and measures something — time, error, count, pass rate. Anything scored by reading rather than by running is a different problem and this folder cannot carry it: the engine has one scoring mode, `custom_script`, and a card whose `measure.kind` is anything else is refused before the run — as is a card that mixes kinds across its criteria, which is scored one way or not at all.

The deliverable is **one folder, complete**. The caller hands the provider that folder's path and nothing else: no iteration count, no worker count, no timeouts, no token ceiling, no prompt wording. Every number the run needs is written in the folder, by you, explicitly. A key you leave out is not "the default"; it is a decision you did not make, that nobody can read off the folder afterwards, and that changes when the provider does.

```
<task>/
  task.json                 the manifest: what this folder is, for the caller and the reader
  evaluate.py               the evaluator's source — the run ignores it, the check compares it
  seed/                     the program: one file, or a tree
    candidate.py
  run/
    scorecard.json          every parameter of the run — all of them, always
    prompts/                optional: this task's own prompt wording
      mutation.md
      repair.md
      prior.md
```

The provider resolves the folder itself. `seed/` is the program; `run/scorecard.json` and `run/prompts/*.md` are copied into the run's own directory before anything reads them; the folder is never written to. A folder with only one half — `seed/` without `run/scorecard.json`, or the reverse — is refused by name (`ARTIFACT_BUNDLE_INCOMPLETE`), not guessed at. `task.json` is the caller's: the provider does not read it, the folder contract requires it, and the check at the end fails without it. Anything else you leave in the folder (`README.md`) is ignored.

Three rules, before any of the steps:

1. **Write every key in the Step 4 template.** Not the ones that differ from a default — all of them. The check at the end compares your card against the template's key set and fails on a missing one.
2. **Decide every number here.** The run reads only the folder.
3. **The folder must run as it is.** No script beside it, no environment variable, no argument. If it needs one, it is not finished.

## The rest of this skill

Four reference files carry the detail. Open each when you reach its step — they are the tables you write from, not background reading.

| file | open it when |
|---|---|
| `references/card.md` | writing `run/scorecard.json` — every key, what it does, how to choose it |
| `references/evaluator.md` | writing the evaluator — the contract, robustness, the traps |
| `references/prompts.md` | writing `run/prompts/`, or choosing `reply_format` |
| `references/speedup.md` | the task is "same answer, faster" — a settled recipe, take it as written |
| `scripts/check_folder.py` | before handing the folder over |

---

## Step 1 — Decide what "better" means

Read what is free first: this conversation, the workspace. Then settle three things.

**1. The measurable criterion.** "More accurate" — which error measure? "Faster" — measured how, on what inputs? It has to be a number your evaluator can compute by running the program, and it must separate the complaint from its opposite. "Well-structured and readable" fails: it is true of any program, and nothing runnable measures it.

**2. What must not change.** The interface candidates must keep, inputs unavailable when the program actually runs, files that define the score, hard limits (runtime, memory, safety).

**3. The starting point.** Use what is in the workspace. Otherwise write **the simplest thing that already does the job badly** — a dozen lines, no tuning, no edge cases. A strong seed is not an advantage: it spends the search space before the search begins.

But "simplest" means simplest *of the right kind*: the seed has to contain the mechanism the search is supposed to improve, in its feeblest form. A seed with no mechanism forces every candidate to invent one from nothing, and a from-scratch implementation fails far more often than an edit does. Measured on two real runs: a cache task seeded with a working LRU climbed 0.2218 → 0.9396, its candidates swapping in ARC, LIRS and TinyLFU on top of a policy that was already there; a compression task seeded with identity encoding — which "works" and compresses nothing — spent ten expansions on whole compressors written from scratch, seven of which did not run at all, and finished at 0.226. Seed the RLE, not the identity function.

**The seed is necessary and not sufficient.** A later compression run seeded with a working RLE-plus-Huffman at 0.62 still drew eleven candidates that each replaced the whole mechanism — arithmetic coding, LZ77, range coding, written from nothing in one reply — and ten did not run.

**Do not fix that by naming the mechanism.** "Add a longer match window to the existing RLE" reads like a helpful narrowing and is the search's own job taken away from it: the human picks the algorithm and the run is left tuning it. The objective is the score, always — the task says what "better" is measured as, never which approach to reach it by. If a run comes back with nothing above the seed, that is a *result*: on this scoring, these variations do not beat the starting point. What is legitimately yours to reconsider is the **scoring** — whether the cases reward what you care about, whether the set is wide enough to separate approaches — not the approach you would like the candidates to take.

**Aim for a seed that scores 0.3–0.7.** Zero is a floor and solved is a ceiling. Two refusals guard the top: a seed at or above the solved threshold (`scorecard.solvedThreshold`, 0.999 by default), and — below it — a seed whose remaining headroom is less than a quarter of what damaging it moved the score by. The second one is the reason 0.3–0.7 is a target rather than advice: a seed at 0.99 with a scoring that can resolve a full point of damage is refused, told to make the cases harder.

### Size the hold-out while you are here

1. **What is one unit?** Whatever one run of the program measures: a test case, one generated problem, one input scenario, one batch of rows.
2. **How big must one be to be stable?** Enough that the same candidate scores the same twice. A unit holding a single item makes its score **binary**, which quietly costs expansions: the search skips proposing on a unit it already solves. Measured — a record-matching run whose units held one record each scored 0 or 1 with nothing between, eleven of sixteen came out at 1.0, and a run planned for 20 expansions made 5.
3. **The gate is the biggest of the three.** Every candidate's score, the one the tree ranks and selects on, is measured on the gate shards. Too few and the tree ranks on noise, silently, for the whole run. Deterministic scoring 8–12; anything with randomness 16–24. **Four is a hard floor** — below it the run is refused.
4. **Rollout next**, four or more, never one, and no more than the gate. Observed with one: five candidates, all exactly 0.6000, budget spent, no signal.
5. **Test is 4–8** and never takes part in the search; it buys confidence in the final number.
6. **Expansions ≥ 4 × workers**, or the first sweep forks only the root and the tree is flat. By search space: a known defect 4–6; swapping approach or restructuring 12–20; writing something from scratch 20+.

## Step 2 — The environment the run will use

**Do this first.** Both the evaluator and every candidate run in one interpreter, and if what they import is not there, nothing in the rest of this file matters: the run is refused before a single model call.

Candidates run on **this machine**, in an interpreter you can reach and prepare. Preparing it is a step you do here, by hand, before the run: `packages` in the card cannot do it for you — a list a model wrote is not allowed to install itself onto someone's computer, and any entry in it refuses the run with the `pip install` line for you to run yourself.

### Find the interpreter, and do not guess it

It is whatever `python` resolves to on `PATH` for the process running the provider — **not the virtualenv the provider itself is installed in**. Those are routinely different, and installing into the wrong one looks exactly like installing into the right one:

```bash
python -c "import sys; print(sys.executable, sys.version.split()[0])"
```

Measured on one machine: the provider ran from a project `.venv` on 3.12, and candidates ran on `/opt/anaconda3/bin/python`, 3.9. A `pip install` into the venv would have changed nothing, and the refusal would have repeated word for word.

### Install what the task needs, into that one

```bash
python -m pip install scikit-learn "xgboost==2.1.0"
```

Then confirm it from the same place the run will ask, and keep the versions — `statement` should name what you *verified*, never what you tried to install:

```bash
python -c "import sklearn, xgboost; print(sklearn.__version__, xgboost.__version__)"
```

**Say what you changed about the machine.** Installing into someone's interpreter is a side effect that outlives the run, and bootstrapping `pip` into a virtualenv that deliberately had none (`python -m ensurepip`) is a larger one. Do it when the task needs it, and report it in one line with the folder — the reader is the person who has to live with that interpreter.

**Everything your evaluator imports counts too, not just what candidates import.** The evaluator runs in the same interpreter — if it does `import numpy` to build cases, numpy has to be there. An evaluator that needs nothing outside the standard library is one fewer thing to arrange, and is worth preferring when the task allows it.

**A Python candidate may also import `numpy`, `pandas`, `scipy` and `sklearn` without asking**: the AST gate admits them, so the engine probes for them before the run and refuses if they are missing. Have those four, or expect that refusal. (They are not probed when the entrypoint is not Python — see "A program that is not Python".)

### If the program is not Python

The same rule with a different noun: whatever the `evaluator_command` names has to be on `PATH` for that same process — `node`, `sh`, `Rscript`, a compiler. Check it the way the run will:

```bash
node --version
```

## Step 3 — `seed/`

The program goes under `seed/`, and the card's `entrypoint` names the file the evaluator is handed. You always write `entrypoint`, so nothing is guessed:

- **One Python file** → `seed/candidate.py`, `"entrypoint": "candidate.py"`. Any other name works the same way as long as the two agree; AlgoTune-style tasks whose harness imports `solver.py` write `seed/solver.py` and `"entrypoint": "solver.py"`.
- **One file that is not Python** → keeps its own name — `seed/solve.sh`, `seed/solve.js` — and `entrypoint` says so. Nothing renames it and nothing parses it. See "A program that is not Python" below.
- **A tree** → keeps its own layout under `seed/`, and `entrypoint` names the file the evaluator imports, as a path relative to `seed/`. A tree whose entrypoint the card does not name is refused rather than guessed at.

**A tree is loaded by extension, and the list is short**: `.md .txt .py .json .yaml .yml .toml .sh .cfg .ini`. Anything else in a seed *directory* is dropped without a word — a directory of `.js` keeps only its `.sh`, and a directory of `.rs` fails with "no files matched". A one-file seed does not go through that loader, so a lone `solve.js` or `solve.rs` is fine. So: a non-Python program is one file, unless its extension is on that list. `__pycache__`, `.git`, `node_modules`, `.venv` and `.DS_Store` are excluded on purpose — a stray `__pycache__` from your own testing does not reach the run.

What the seed must contain is `evolve-design`'s subject, not this one's. The one thing to check here: whatever the evaluator calls must exist in the seed, or the probe fails before the run and the message is about your starting point rather than about the search.

**One file or a tree is a real trade rather than a default.** A reply carries only the files it changed, so file-level granularity is worth something only when there is more than one file — and what the files *are* is a ceiling on what the search may become.

* **One file** leaves the search free to invent any structure, and pays for it in full rewrites. Measured on a one-file run: by the third generation the program was 173 lines, the edits that actually mattered were 13 and 16 lines — 7% and 9% — and every reply restated all of it. The parent was 69% of the prompt and the same fraction of every answer. Each of those rewrites is also a chance to change something nobody meant to change.
* **A tree** localises the edit and narrows as the program matures. Measured on a four-file pipeline: the root's children rewrote all four (every file was a stub, so every file needed changing), the second generation rewrote three, and the third rewrote two — inheriting the rest untouched.

Split on boundaries that are **facts about the task** — read the data, build features, fit, score — never on the approach you expect to win. A directory that already says "sample, then refine" has chosen half the algorithm, and the search is left tuning your decision. On the one-file run above, what won was the model's own idea to add lattice refinement to differential evolution; a seed pre-split that way would have handed it that for free.

## Step 4 — `task.json`

The one file in the folder written for the caller rather than for the run. The provider never opens it; the UI lists folders by it, and a reader opening the folder learns from it what the folder is without parsing the card. Write all eight keys:

```json
{
  "task_id": "evolve-anagrams",
  "artifact_path": "seed/candidate.py",
  "run_dir": "run",
  "max_iterations": 20,
  "entrypoint": "candidate.py",
  "reply_format": "files",
  "language": "python",
  "files_in_seed": ["candidate.py"]
}
```

| key | write | what it is |
|---|---|---|
| `task_id` | `evolve-<task>` | The folder's name for itself. The run's own id is assigned by the caller; this one is what a reader searches for. |
| `artifact_path` | `seed/<entrypoint>` | Where the program is, relative to the folder. |
| `run_dir` | `run` | Where the card and prompts are, relative to the folder. |
| `max_iterations` | the card's `iterations` | **Must equal `iterations` in the card.** A caller that has to fill in the contract's `max_iterations` reads it here; the provider reads the card; the two must not disagree. |
| `entrypoint` | the card's `entrypoint` | Repeated here so the manifest is complete on its own. Must equal the card's. |
| `reply_format` | the card's `reply_format` | Same rule. |
| `language` | `python` | Or the language of a non-Python seed: `shell`, `javascript`, `rust`. |
| `files_in_seed` | the listing of `seed/` | Every file under `seed/`, relative paths, sorted. Must match the directory. |

Nothing else goes in it. A field the manifest and the card both carry is the card's decision repeated, never a second place to decide it — the check at the end fails on a disagreement.

## Step 5 — `run/scorecard.json`, complete

This is every key the provider reads, with values that run. Copy it, then change the values — never remove a key. A key that is not here is not read, so do not add ones either.

```json
{
  "statement": "Group words that are anagrams of each other. Case and punctuation do not matter; spelling does. Scored on the fraction of word lists partitioned exactly right.",
  "script": "<the evaluator's full source, as one JSON string — assemble it, do not hand-escape it>",
  "hash": "sha256:anagrams-v1",
  "entrypoint": "candidate.py",
  "evaluator_file": "evaluate.py",
  "evaluator_command": [],
  "packages": [],
  "reply_format": "files",
  "iterations": 20,
  "workers": 3,
  "max_tokens_per_call": 32000,
  "options": {
    "c_puct": 1.0,
    "prior_exponent": 0,
    "repair_attempts": 2,
    "completion_timeout": 900,
    "mode": "async",
    "staleness": "full",
    "async_ratio": 1
  },
  "scorecard": {
    "aggregate": "weighted_sum",
    "constraints": [],
    "solvedThreshold": 0.999,
    "criteria": [{
      "id": "exact",
      "name": "partitions exactly right",
      "direction": "maximize",
      "weight": 1.0,
      "normalize": {"kind": "identity"},
      "measure": {
        "kind": "custom_script",
        "timeoutSeconds": 90,
        "split": {"gateShards": 5, "rolloutShards": 3, "testShards": 3, "seed": 4}
      }
    }]
  }
}
```

**Assemble the card; do not type the evaluator into it.** `script` is a whole file inside a JSON string, and hand-escaping one is where quotes and newlines go wrong. Write the evaluator as a file, then build the card from it:

```bash
python - <<'PY'
import json, pathlib
card = json.loads(pathlib.Path("card.json").read_text())        # the template, script left empty
card["script"] = pathlib.Path("evaluate.py").read_text()        # the evaluator you wrote
pathlib.Path("run/scorecard.json").write_text(json.dumps(card, indent=2) + "\n")
PY
```

Keep `evaluate.py` in the folder root — the run ignores it, the check at the end compares it against `script` and catches the copy going stale, which is the failure this shape invites: the card holds a *copy*, so an evaluator edited after assembly leaves the run scoring the old one, silently. Assemble last, re-assemble after every edit, and never edit the evaluator inside the JSON.

**Every key's meaning is in `references/card.md`** — the four tables (top-level, `options`, `scorecard`/`split`, `normalize`), what each value costs, and which keys an older provider build ignores. Open it now rather than guessing a value.

## Step 6 — The evaluator

It goes into the card as the `script` string, and it is where a search is usually lost. **Read `references/evaluator.md` before writing it.** The contract in brief:

```python
"""Score a program.                         <- this docstring is what the model is shown

Your program must define:

    solve(problem) -> answer
"""
import json, os
entry = os.environ["SCIENCE_AGENT_CANDIDATE"]   # the candidate's filename
slots = os.environ["SCIENCE_AGENT_SHARDS"]      # comma-separated shard indices
out   = os.environ["SCIENCE_AGENT_RESULT"]      # write one JSON object here, not stdout
json.dump({"valid": True, "metrics": {"score": 0.83}, "error": ""}, open(out, "w"))
```

Five rules that decide whether the run works at all, each expanded in the reference file: the docstring is the contract the model sees and must not carry the answer key; write to the result file, not stdout; use the shards you are given; survive a broken candidate — including one that returns `None` instead of raising; and measure the work rather than a proxy for it, because a proxy is what the search will find its way around.

## Step 7 — `run/prompts/` and `reply_format`

Prompt wording is optional and the built-ins are complete; `reply_format` defaults to `files`. Both are in `references/prompts.md`. The one rule worth carrying here: a `mutation.md` without `${reply_format}` is refused at load, and a prompt that never says how to reply produces replies nothing can parse, for the whole budget, with every step reporting success.

## Before handing it over

Run the checker. It checks the shape, the key set against the template, and the numbers' floors; it never imports the seed and never runs the evaluator.

```bash
python scripts/check_folder.py <task-folder>
```

Then, in the order they would fail:

1. Step 2 is done: the interpreter that will run candidates imports everything your evaluator and your seed need, checked from that interpreter rather than from yours.
2. The check above prints `ok`.
3. Your evaluator runs against your seed, once, and writes the result file. You are checking that what you wrote executes — a typo, a missing import, a file never written. **You are checking the ruler, not looking for the answer**: do not go hunting for a candidate that beats the seed. That is the search's entire job, done by hand, at the cost of the turn — and succeeding is worse than failing, because you then either throw the answer away or seed it, and a strong seed spends the search space before the search begins. A seed that no obvious variation beats is a good seed, not a problem to solve first. **Do not score a damaged copy yourself**: the discrimination probe does exactly that, on the real shards, in the interpreter the run uses, and reports both numbers when the run starts. If your own number and the probe's disagree, the probe's is the one that is true — it measured the real thing.
4. Every seed file the evaluator names exists under `seed/`, and the folder is its own directory named for the task — `nearest-centroid/`, not the project root you happened to be working in. The name is part of the deliverable: it is what the caller picks from a list.
5. Hand over the folder's path as `artifact_path`. Nothing else travels with it; a caller that must fill in `max_iterations` takes it from `task.json`, where it equals the card's.
6. **Read the baseline number the probe reports, not just whether it passed.** It refuses a start at or above the solved threshold, and also one whose headroom is small next to the damage signal — but a start that clears both can still be a bad one. Anything near either bound means the scoring has little room, whatever the probe said. Aim for 0.3–0.7.

## When a run comes back refused

| `error_code` | what it means |
|---|---|
| `ARTIFACT_BUNDLE_INCOMPLETE` | the path is a folder with one half of the layout — the message names which of `seed/` and `run/scorecard.json` is missing. |
| `PROBE_REFUSED` | the scoring cannot separate the seed from a damaged copy, or the seed does not run, or the interpreter is missing something the card asked for. The message names which. |
| `SEARCH_FAILED` | the engine refused the configuration (a `measure.kind` other than `custom_script` or a card mixing kinds, unknown normalisation, unknown `reply_format`, `gateShards` below 4, an empty `script`, a missing candidate runtime, an unknown `mode`), no expansion produced a candidate that ran, or the search loop itself stopped on an error — the message says which, and for the last one carries the error. |
| `MODELCONFIG` | no model instance was injected — an AgentServer wiring problem, not yours. |
| `EXECUTIONUNAVAILABLE` | no way to run candidates was configured, and none could be built. |
| `TASK_ALREADY_RUNNING` | a second `run`/`resume` reached a task whose search is still in flight. |
| `TERMINATED_NOT_RESUMABLE` | `terminate` is terminal; only `paused`, `completed` and `failed` resume. |

A refusal costs a handful of evaluations and no model calls. A run that completes reports `usage` — model calls and tokens — on `read_state`. A node whose message says the model "spent all N output tokens on hidden thinking" is the ceiling from `max_tokens_per_call`, not a model that cannot write code: raise it, or turn thinking off on the caller's side, and start a new run — a card already copied into a run directory is kept as it was.
