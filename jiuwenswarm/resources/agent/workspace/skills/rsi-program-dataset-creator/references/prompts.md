# Prompt wording and the reply protocol

`run/prompts/` is optional — the built-in wording is complete, and a prompt you did not write is one you cannot have broken. `reply_format` is not optional but has only two values.

## `run/prompts/` (optional)

Leave the directory out and the built-in wording is used. Supply a file and it replaces that prompt's wording entirely, rendered over the framework's slots with `${name}` syntax (`string.Template`, not `str.format` — a task's template is full of code and code is full of braces). Prompt wording is the one thing in the folder that is allowed to be absent: the built-ins are complete, and a prompt you did not need to write is one you cannot have broken.

| file | slots available |
|---|---|
| `mutation.md` | `${statement}` `${contract}` `${parent_code}` `${parent_score}` `${best_score}` `${feedback}` `${history}` `${imports}` `${how_to_change}` `${reply_format}` |
| `repair.md` | `${code}` `${error}` `${imports}` |
| `prior.md` | `${prompt}` |

Rules that bite:

- **`mutation.md` must contain `${reply_format}`.** It is refused at load without it. That slot carries the output protocol — how the model is told to answer — and the reader on the other side expects exactly that shape. A prompt that never says how to reply produces replies nothing can parse, which become candidates that do not compile, for the whole budget, with every step reporting success.
- **An unknown placeholder is refused by name at load**, with the vocabulary listed. `${statment}` would otherwise stay in the prompt as literal text and the model would optimise against a hole in it, silently.
- **`${parent_code}` already carries its own fence.** Do not wrap it in ```` ```python ```` — you get a double fence.
- **`${contract}` must have something to say.** The evaluator's docstring fills it, and a mutation prompt built without one is refused: a candidate aimed at nothing is aimed at nothing. (A run whose contract went missing had candidates bolting on CSV readers "to match the scoring requirements" while the thing being optimized never changed.)
- **Do not use `${history}` for an ERA-style search.** Its expansions are independent draws; injecting sibling history changes what the search is. Failure information belongs in the parent's `${feedback}` and in selection pressure.
- **`prior.md` is only rendered when `prior_exponent > 0`.** With it at `0` the file is loaded and validated and never used.

`${feedback}` carries what the evaluator wrote in `error` for the parent, up to 4 000 characters, prefixed `What the evaluator said about it:`. Keep the part that steers at the top; anything past the cap is dropped.

## `reply_format`

The output protocol is a **pair** — the sentences that ask for it and the reader that understands it — chosen together by name in `scorecard.json`. An unknown name is refused before the run.

| name | shape | when |
|---|---|---|
| `files` | one fenced block per changed file, labelled `name=path/to/file.py`; unlisted files inherited; `DELETE path` removes one | multi-file programs, and everything already written |
| `tagged` | `<PROGRAM>…</PROGRAM>` plus `<CHANGE_SUMMARY>…</CHANGE_SUMMARY>`, single file | reproducing an OpenEvolve-style prompt faithfully |

Both replace whole files — never a patch or a fragment. File-level granularity is the point: a model asked to restate ten files to change one spends the tokens on nine copies and, worse, rewrites the nine.
