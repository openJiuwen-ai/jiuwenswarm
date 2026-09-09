# Writing the evaluator

Read this before you write the script that goes into the card's `script` field. It is the one part of the folder that decides whether the search can tell a good candidate from a bad one.

## The contract

It goes into `scorecard.json` as the `script` string. Its contract with a candidate:

```python
"""Score a regression program.

Your program must define:

    train_and_predict(train, test) -> list[float]

...
"""
import importlib, json, os

entry  = os.environ["SCIENCE_AGENT_CANDIDATE"]   # the candidate's filename
slots  = os.environ["SCIENCE_AGENT_SHARDS"]      # comma-separated slot indices
out    = os.environ["SCIENCE_AGENT_RESULT"]      # where to write the result

json.dump({"valid": True, "metrics": {"score": 0.83}, "error": ""}, open(out, "w"))
```

**The module docstring is what the model is shown** — not the whole script. That is deliberate: the full text would carry the answer key, and a candidate that has read the answers optimises for reciting them. So state the interface in the docstring, completely, and keep the cases out of it. A script with no docstring falls back to its first forty lines, which is worse in both directions.

**Write the result to the file, not stdout.** A candidate that prints is ordinary; a candidate whose print lands in the middle of your JSON is a run that fails for a reason nobody can see. (Printed JSON is accepted as a fallback, but the file is what the contract asks for.)

**Use the shards you are given.** They are what makes the gate a held-out gate rather than a second look at the same examples. An evaluator that ignores them turns the gate into a copy of the rollout and nothing downstream can tell.

**The evaluator must survive a broken candidate — including at import, and including one that does not raise at all.** Most candidates in a search are broken, and the discrimination probe deliberately scores a hollowed-out copy, so this is the normal path. Guard the `import`, guard each call, and guard the *answer*: a hollowed-out function returns `None`, and a confused one returns a scalar, a string, or a list of the wrong length. Measured on a real staging session — the evaluator survived every exception it was written for and then died on `len(None)` when the probe handed it a body-less `solve`, which reads as "your evaluator is broken" and refuses the run. Check the answer's shape before scoring it, and turn anything unexpected into `{"valid": false, "error": "..."}` with a clean exit. If the *script* crashes, the run is refused and the message names your evaluator rather than the candidate.

**A damaged copy that fails outright is a pass, not a problem.** The probe hollows out every function body and expects the score to move. An evaluator written as above answers that with `valid: false` and no number at all — the probe reports `worsened: None`, `flat: False`, and the run starts. You do not need the damaged copy to produce a *low* score; you need it to not produce the same one.

**Measure the work, not a proxy for it.** If the number you report is instrumentation rather than the thing itself, ask what implementation would bypass the instrument — because the search will find it. Measured: a task scored on how many points a nearest-neighbour query examined, counted by wrapping each point so coordinate reads were tallied. A candidate reached for `scipy.spatial.cKDTree`, which copies the points into a C array at construction, so the queries never touched the wrapper: `probes = 0.0`, a perfect score, answers all exactly right, and the metric had simply stopped measuring. Another candidate said so outright — "answer queries from the training set with zero probes". Nothing was cheating; the proxy was.

An earlier version of the same task counted reads on the *container* instead of the points, which a candidate that iterates never touches — every candidate scored 0 probes, the seed scored a perfect 1.0000, and the probe refused the run before a single model call. That is the good case: an instrument that measures nothing at all is caught; one that measures the right thing for interpreted code and nothing for compiled code is not.

**`error` is a channel, not a formality.** Whatever you write there reaches the next mutation prompt as `${feedback}` — up to 4 000 characters, room for an eval summary, timing rows and a profile — and it is the one place per-candidate, task-specific diagnosis can steer the search. Write it for valid candidates too; that is how the next one learns what to keep.

### A program that is not Python

Neither side has to be Python. The candidate is whatever file `seed/` contains, and the evaluator is whatever the card says to run.

| card key | write | what it is |
|---|---|---|
| `entrypoint` | `solve.sh` | the name the evaluator is handed in `SCIENCE_AGENT_CANDIDATE` |
| `evaluator_file` | `evaluate.sh` | what `script` is written to, beside the candidate |
| `evaluator_command` | `["sh", "evaluate.sh"]` | argv, run in that directory |

Three rules, each of which is enforced rather than assumed:

* **`evaluator_command` is argv, never a shell line.** `"sh evaluate.sh"` as a string names an executable that does not exist; the card is refused with the shape it wants.
* **A non-`.py` `evaluator_file` with `[]` for the command is refused when the run is created.** `[]` runs your file through the Python shim, so `evaluate.js` with it would be handed to `runpy` as JavaScript — a SyntaxError on every candidate, for the whole budget.
* **Only `[]` stages the shim.** A card that names its own command gets a directory holding its two programs and nothing else.

What changes in the prompt, automatically, once the entrypoint is not `.py`: the fenced blocks are labelled by suffix (```rust, ```bash) instead of ```python, the output-protocol example uses that language, the "you may import only ..." section disappears (it lists *this* interpreter's packages, which says nothing about a Rust candidate), and the change summary is asked for as the file's first comment instead of a module docstring — read back the same way.

What is still Python-only, by design: the AST gate (skipped entirely for other languages — it would refuse a Rust file for a syntax error in a language it is not), `packages`, and the `numpy`/`pandas`/`scipy`/`sklearn` runtime probe, which is not run when the candidate is not Python — so a machine with a toolchain and no interpreter can still run such a task.

**Your evaluator still writes the same one JSON object.** A shell evaluator that runs the candidate and scores its stdout:

```sh
#!/bin/sh
# Runs the candidate on each case and scores its output.
got=$(sh "$SCIENCE_AGENT_CANDIDATE" < case.txt)
printf '{"valid": true, "metrics": {"score": %s}}' "$score" > "$SCIENCE_AGENT_RESULT"
```

The leading comment block is read as the contract shown to the model, the way a module docstring is for a Python evaluator — so state the interface there, and keep the sample data below it.

## Two rules about the shape of the score

**Score on a gradient, not a cliff.** For hard limits prefer "stop and score what you have" over "violation scores zero" — zeroing lands every failure on the same 0 and leaves the search nothing to climb.

**Never reward a property the candidate can fake.** Rewarding a shape rather than a result — "uses a vectorised call", "keeps the function under 50 lines" — buys the shape and nothing else; reward the measured time or the measured error. This is the same failure as measuring a proxy, one level up: the search optimises what you wrote down, not what you meant.
