# Paper evidence contract and PaperEvidenceRail

`jiuwenswarm-paper` runs agent-core's paper pipeline (`paper_opt.auto_research`). The host — not a
model — decides whether the experiment evidence supports the paper. This page describes that
evidence contract and `PaperEvidenceRail`, the harness rail that applies it inside the pipeline's
agents.

## What is checked

| Object | Written by | When | Content |
|---|---|---|---|
| `evidence/protocol.json` | host | **before** each execution | primary metric, required cells, required primary comparisons, comparisons review items require, pre-declared item sets, activation gate per tier, retired cells, per-cell spec hashes, missing-value rule, delivery policy, design identity; id = hash of the content |
| `evidence/manifest.json` (+ `executions/<id>.json`) | host audit | after each execution | protocol id, execution id, revision, each cell's role (executed / reused / frozen) / status / metrics sha256 / records hash / item coverage / model / dataset / budget / version / spec, status of every required and review comparison, audit verdict |
| `evidence/ledger.json`, `cells/<cell>/v<k>.metrics.json`, `history.jsonl` | host audit | after each execution | the evidence ledger: every successful execution is a hashed version tied to its cell spec; re-runs never overwrite history |
| `evidence/retirements.json` | operator (`retire`) | any time; effective with the next protocol | retired cells with the reason and the affected comparisons / claims |
| `acceptance.json` | host | end of run / resume | pipeline finished, evidence verified, PDF check, `primary_hypothesis_verified` (reported separately) |

`evidence.verify()` is the only acceptance rule. The execution audit, the revision gate,
`acceptance.json` and the rail all call it, and **none of them passes the design**: `verify()`
locates and reads the current design itself (the path the protocol recorded, else the pipeline's
`design/experiment_design.md`). It re-checks from disk:

- the protocol is unchanged (an audit made under an older protocol does not count) and has no problems (`PROTOCOL_INVALID`);
- the current design is the one the protocol froze (`DESIGN_CHANGED`); a design that cannot be found or read is `DESIGN_UNVERIFIED`, never a pass;
- every required cell completed, is not blocked by an audit error, and was recorded under its current cell spec (`CELL_SPEC_CHANGED`);
- each result file still has its audited hash (an edit after the audit fails);
- every required primary comparison is **verified**: both sides cover the same pre-declared item set in full, with equal recorded budget, model and dataset, unique item ids and a complete primary metric (no declared item set: `ITEM_SET_UNDECLARED`);
- in a revision: the evidence comes from this revision's executions, and every cell the revision executed is either still in the design with passing evidence or retired with a record (`REVISION_CELL_DROPPED`).

## Structured experiment protocol

One fenced JSON block tagged `experiment-protocol` at the end of the design; the host freezes it
into the protocol before execution:

```experiment-protocol
{"item_sets": {"": {"dataset": "hotpotqa-dev", "ids_file": "item_ids.json"}, "m2": {"same_as": ""}},
 "tier_gates": {"t1": 0.5},
 "calibration_cells": ["calib_reference"],
 "cells": {"abl_rank_T1": {"implementation": "v2"}},
 "review_comparisons": [{"item": "R-1a2b3c4d", "a": "proposed_T1", "b": "abl_rank_T1"}],
 "retired": [{"cell": "abl_old_T1", "reason": "...", "affected_comparisons": ["..."], "affected_claims": ["..."]}]}
```

- With the block, the design identity is the hash of the block and the declared metric names, so **rewording the prose does not invalidate evidence**; without it, the full text is hashed.
- `item_sets`: the item ids each setting (variant-name prefix, `""` = original) pre-declares (`ids`, or `ids_file` in the code directory). Fallbacks: the code's `item_ids.json`, the previous protocol's set, the item set the frozen cells of a revision share. Every variant must cover its declared set; missing records, missing metric values, unanswered items, API failures and parse failures are counted separately; `score_zero` scores only the last three, a missing record never.
- `tier_gates`: minimum activation rate per tier, in [0, 1], part of the protocol identity (`--tier-gate T1=0.5` overrides). The audit reads gates **only** from the protocol; gates in the experiment output are ignored and reported (`OUTPUT_GATE_IGNORED`). With no gate pre-registered the default 30% applies and is stated as a limitation. `calibration_cells` run before the gates are fixed and are never evidence.
- `cells`: per-cell entries; changing a cell's entry (e.g. `implementation`) invalidates its earlier results.

## Reuse and the re-run limit

A cell's spec covers its name / setting / method / tier, the primary and declared metrics, the
missing-value rule, its setting's item-set hash, its tier's gate, its `cells` entry and the
answering-model settings. In a revision the execution guard, for each non-frozen cell:

1. references a ledger version with the same spec, intact hashes and no blocking finding (restoring the results file from the archived copy if something replaced it) — not executed, no execution counted;
2. with versions under another spec only, notes that a re-run is required; the old versions are kept;
3. applies the new-cell cap and the per-cell re-run limit (default 3) **to new executions only**: a cell at its limit that has a valid version is still referenced;
4. does not execute cells the protocol retired.

A comparison is `verified`, `failed` (a cell did not complete) or `unverified` (with a reason). A
verified comparison has an outcome: `a_better`, `a_worse`, `bounded_null` or `inconclusive`. A
negative or null outcome is evidence and can be delivered. An unverified comparison is never read
as "no effect".

## Options

| Option | Default | Meaning |
|---|---|---|
| `--delivery-policy confirmatory\|descriptive` | `confirmatory` | `confirmatory`: every required primary comparison must be verified. `descriptive`: the paper may be delivered without that, but it must not claim the hypothesis was tested; the gaps become required limitations |
| `--missing-primary-rule refuse\|score_zero` | `refuse` | a missing primary value makes the comparison unverified. `score_zero` scores unanswered, API-failed and unparsable items as 0, and refuses any other gap |
| `--tier-gate T1=0.5` (repeatable) | the design's gate, else 30% | minimum activation rate per tier frozen into the protocol; overrides the design |
| `--evidence-rail` | off | mounts `PaperEvidenceRail` (needs the rigor protocol, which is on by default) |

These options, plus the budget options, are stored in `run_settings.json` (and in `revision.json`
for a revision). A `resume` without them restores them; an explicit different value is applied and
recorded as an override.

Offline commands:

```bash
jiuwenswarm-paper audit    --run-dir runs/p1 [--design .../experiment_design.md]   # rebuild the protocol from the current design, re-audit (in an open revision: for it)
jiuwenswarm-paper evidence --run-dir runs/p1                                         # verify now; exit 0 / 2
jiuwenswarm-paper retire   --run-dir runs/p1 --cell abl_x_T1 --reason "..." \
    --affects-comparison "proposed_T1 vs abl_x_T1" --affects-claim "..."             # retire a cell (next protocol)
jiuwenswarm-paper abandon  --run-dir runs/p1 --revision 1 --reason "..."             # end a revision
jiuwenswarm-paper rollback --run-dir runs/p1 --revision 1                            # restore the previous paper
```

Runs made before this contract have no evidence manifest. Run `audit` on them once before a
`revise`; until then, acceptance reports `NO_EVIDENCE_MANIFEST`. If their design declared no item
set, `audit --item-set <ids file>` declares one after the fact — recorded as such (a limitation,
not a pre-registration).

A design changed after the audit gives `DESIGN_CHANGED`. Re-execute (in a revision, cells whose
spec did not change are reused) or rebuild the protocol with `audit`. If the change touches a
cell's condition (gate, item set, cell entry ...), that cell's old result fails the new protocol
with `CELL_SPEC_CHANGED` and must be re-run: a gate moved after the fact cannot pass an old result.

## Revisions

A revision that finishes without passing acceptance becomes `needs_repair`, not closed. Its frozen
cells, new-cell cap, per-cell re-run limit (default 3, separate from the cap) and budgets keep
applying. The next `resume` tells the manager what is missing. Only `accepted`, `rollback` or
`abandon` ends a revision. `not_accepted`, which older versions wrote automatically, is read as
`needs_repair`.

Review responses (`revision_response.json`) end in one of three states:

- `verified_resolved`: a `new_experiment` entry whose item id the protocol binds to `review_comparisons` (declared before execution, apart from the primary-hypothesis comparisons), each verified over the declared item set (any outcome — a negative or null result answers the item; significance is not required), with a cell executed in this revision and matching `conditions`; any evidence the entry cites must pass the same checks, and a retired cell never counts;
- `addressed`: a rewrite, narrowed claim or stated limitation, at a `\label` or an exact section or caption title — legitimate, but not "resolved by experiment";
- unresolved: anything else, e.g. a one-sided ablation, a new setting with the proposed arm but no baseline, or an item the protocol binds to no comparison.

## PaperEvidenceRail

`install_paper_evidence_rail(run_dir)` wraps `ManagerAgent._create_agent` and
`ReportingAgent._build_paper_agent`. It queues the rail on each agent they build through the
framework's public `DeepAgent.add_rail`, once per agent; installing it twice does not stack.

| Point | Hook | Behaviour |
|---|---|---|
| experiment executed | host execution wrapper (`execution_audit.AFTER_RECORD_HOOKS`) | logs execution and protocol identity to `evidence/rail_log.jsonl`, drops the cache |
| writing starts | reporting `on_user_message` | prepends the evidence manifest: verified comparisons with outcomes, unverified ones (not to be claimed), required limitations |
| DONE requested | manager `before_tool_call` on `submit_manager_decision` | without accepted evidence, the call is **skipped** by the framework's tool-call skip mechanism, and the manager gets the pending tasks instead |

Failure handling: a check that cannot run (no results, unreadable files, an exception) rejects DONE
as unverified. Only the rail's log writing is best-effort. The rail runs no audit and makes no model
call. Its verdicts are cached by the state of the files on disk, and DONE always re-checks without
the cache.

Limitations, stated plainly:

- In the pinned agent-core (`9e3390195`), experiment execution is a host-side runner, not a `DeepAgent`. There is no rail callback at "execution finished", so that point is the existing host wrapper calling the same service.
- The host's `acceptance.json` and the revision gate stay the deterministic delivery check. The rail rejects DONE earlier and explains why; it does not replace them. A manager that keeps asking for DONE ends its round in agent-core's decision retries, not in delivery.
- Budget limits are checked before each manager round and before each experiment variant starts. A running variant is not interrupted.
- The rail is mounted on the `jiuwenswarm-paper` path (`runner.run`). The AgentServer RSI paper adapter (`rsi/provider_factory.py`) installs neither the rigor protocol nor this rail, and is unchanged. JiuwenSwarm's rail-provider registry (`register_rail_provider`) builds JiuwenSwarm's own agents; the paper agents are built inside agent-core, which is why the rail wraps their builders.

## Model-free example

```bash
python scripts/paper_evidence_demo.py --out /tmp/evidence-demo
```

The example runs a first execution under a design with an `experiment-protocol` block, then a
revision whose design binds a review item to a second-model replication: one of its two new cells
succeeds, one fails (acceptance: `needs_repair`). After a restart the successful cell is **reused**
from its evidence version (not executed, not counted) and only the failed one is re-run; the rail
check and acceptance pass and the item is `verified_resolved`. Only the experiment subprocess is a
stand-in. The wrapper, guard, ledger, audit, statistics, evidence manifest, acceptance and rail
service are the real code. It is a simulation, not a paper run.
