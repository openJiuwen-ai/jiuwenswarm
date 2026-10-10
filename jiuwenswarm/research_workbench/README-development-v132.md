# Same-evidence completeness diagnostic (development only)

The answer-checklist condition adds a fixed system instruction to the existing
short-answer prompt. Question, paper, paragraph IDs/order/text, output schema,
temperature, and per-call output ceiling remain identical. System length and
actual token costs differ. No reference answers enter the request builder.

`qasper_selection.py` also compares three conventional offline selectors:
prefix BM25, scan-to-fit BM25, and a fixed relevance/lexical-coverage heuristic.
Lexical coverage is not semantic fact verification. The selector experiment
does not establish novelty, answer correctness, or significant improvement.

Run related checks with Python UTF-8 mode (required by existing Windows tests):

```powershell
$env:PYTHONUTF8='1'
python -m unittest jiuwenswarm.research_workbench.test_qasper_selection jiuwenswarm.research_workbench.test_completeness_runner jiuwenswarm.research_workbench.test_qasper_runner
```

The bounded runner accepts at most 96 prepared development requests and at most
2 CNY reserved per batch, within the existing cumulative 15 CNY ceiling.
Requests, source, datasets, settings, and output artifacts are hash-bound.
No automatic retries are used. A 90-second cooperative total deadline surrounds
the SDK invocation; it complements the SDK's 60-second inactivity timeout.
Suspension or process termination prevents local deadline handling. A timed-out
or terminated request may have reached the provider: retain its reservation and
record unknown usage, never silently refund it or replay it.

The private project report preserves a 12-paper pilot and a separate 36-paper
expansion. Two baseline outputs in the expansion are missing after timeout and
local process disappearance; successful outputs are not substituted. Report both
the planned denominator (missing outputs zero) and the 34 complete-pair secondary
analysis, explicitly acknowledging timing discontinuity and selection effects.
The 34-pair interval includes zero. This is exploratory training evidence, not a
confirmed method improvement or an updated formal paper result.

The original competition submission, frozen experiment and public PR are not
modified by this development branch. No new external publication is performed.
