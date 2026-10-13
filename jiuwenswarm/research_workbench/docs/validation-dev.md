# Prospective QASPER dev-answer validation (v138)

This adds an explicit dev allowlist to the existing bounded QASPER runner. It does not relax training inputs into arbitrary dev/test execution. The immutable v137 design contains 48 papers, one question per paper, three memory conditions, two repeats, and eight batches of 36 requests. Dev was previously inspected for offline retrieval: this is not an unseen test claim.

`qasper_validation_cohorts.json` binds each batch's public papers, public questions, separate gold file and exact question/method order to the v137 protocol hash. The full source and registry are included in new execution-plan hashes. Prompts are rebuilt from public fields and pinned BGE embeddings; the existing extractive-v2 algorithm, prompts and decoder controls are unchanged. Gold is hashed at execution and read only by scoring. Repetition is represented by distinct frozen batches, not by duplicate keys inside a batch.

Train remains bounded to a configured total of at most CNY15. Only allowlisted dev plans can propose up to CNY30; planning does not change the live budget. The prepared v138 plans propose a CNY20 total, while the real workspace remains at CNY15 and paid calls disabled pending the user's answer. Actual serialized requests reserve CNY4.756116 in aggregate, yielding CNY19.157078 with retained historical holds. Prices use the officially verified CNY2/8 per million uncached input/output peak rates on 2026-10-07; these conservative reservations are not reconciled provider charges.

All normal protections remain: per-question limits, per-batch bounds, 90-second cooperative deadline, zero automatic retries, failure stop, source/input hashes, single-use claims, and disabled paid execution on completion. Never replay prior train plans with changed source, relabel dev as train, delete historical unknown usage, or enable paid execution on the strength of this document.

Tests (from repository root):

```powershell
python -m unittest jiuwenswarm.research_workbench.test_qasper_validation jiuwenswarm.research_workbench.test_qasper_runner jiuwenswarm.research_workbench.test_qasper_dense_bundle jiuwenswarm.research_workbench.test_qasper_memory
```

Sixteen tests passed, including three new dev tests. A separate synthetic-transport rehearsal exercises all 288 actual planned slots, scoring paths, cumulative reservations and replay rejection in a temporary ledger. Synthetic outputs are not research results and are not inserted into the live workspace. Prepared bundles and the rehearsal record remain under the desktop project `research/release-v138`.

For a future real batch, use the existing runner CLI only after budget authorization and whole-study preflight. Each folder contains `bundle/` and `proposed-plan.json`. Keep the frozen order. Stop the whole study if any batch is incomplete; preserve attempted records and use a new explicit continuation plan only for unattempted slots. Aggregate repeated outputs by question/paper according to the original protocol; do not tune against validation answers.
