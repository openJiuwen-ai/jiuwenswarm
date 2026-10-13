# Question-only query planning development baseline

This module adds a conventional multi-query baseline to isolate weak retrieval
from answer-generation failures. It is not claimed as a new retrieval algorithm
or a validated improvement to the submitted memory study.

The planner receives only the question, public paper title and up to 40 capped
section names. It returns 1–4 requested information items and 1–3 keyword queries.
It never receives reference answers, reference locators, prior model answers or
the paper body. The generated information items are intentions, not evidence.
Only the generated queries affect retrieval. Original and generated query lists
are deduplicated; BM25 rankings are fused using sum(1/(60+rank)). Both answer
conditions use the same checked prompt, JSON schema, top-8 whole-paragraph and
12,000-byte evidence ceilings. Actual context, token use and cost differ. The
planned condition pays for the planner as well as its answer call.

The 12 balanced QASPER train questions were previously observed. This is an
exploratory reused-cohort diagnostic, not an independent confirmation. The
official test remains unread; the original competition artifacts stay frozen.

The first segment stopped on a planner response containing an extra transport
metadata field, `type: json_object`. A versioned parser repair accepts precisely
that value and strips it; duplicate keys, unknown extra keys and all other type
values are still rejected. Stored raw output can be reparsed offline under the
new contract. The original failed record remains failed in its original run.
A separately hash-bound continuation records `offline_schema_recovery`, copies
verified parent artifacts, and sends only steps without a cached response.
Parent steps are not counted as new paid calls. Unknown or truncated responses
cannot be recovered through this path.

Reproduction: UTF-8 Python environment, the frozen plan, its dataset hashes and
source snapshot are required. Never rerun a claimed plan. To test without a key:

```powershell
$env:PYTHONUTF8='1'
python -m unittest jiuwenswarm.research_workbench.test_qasper_queries jiuwenswarm.research_workbench.test_qasper_reopen
```

Next research question: after establishing a stronger retrieval baseline, does
retaining requested information items and verified source pointers in compressed
memory improve complete, grounded answers under a fixed context and call budget?
That memory-specific intervention is still a proposal, not an experiment result.
