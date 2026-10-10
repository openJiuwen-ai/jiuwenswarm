# Bounded extractive evidence memory prototype (v136)

This is a single-task evidence-compression experiment, not a persistent memory
engine, a self-evolving agent, or an established novel algorithm.

Starting from the fixed BGE/BM25 hybrid retrieved context, compare:

1. Full retrieved context: up to8 complete paragraphs and12,000 UTF-8 bytes.
2. Plain extractive memory: BM25-relevant sentences scanned to fit2,400 bytes.
3. Qualifier-aware extractive memory: relevant sentences plus one-hop adjacent
   sentences in the same paragraph that have scope, quantity, contrast or
   condition markers. Greedy score is0.8 normalized BM25 plus0.2 the fraction of
   previously uncovered marker categories, under the same2,400-byte ceiling.

Both compressed conditions preserve original paragraph IDs, exact character
offsets, verbatim spans and source hashes. There is no model-generated summary or
new inferred fact. Source text is reconstructed from validated public inputs;
offset or prompt tampering is rejected before calling the paid API. Whole
sentences that cannot fit are skipped, and selected spans within a paragraph
are restored to source order with newline separators. Empty memory permits only
an abstention under the existing answer contract. Sentence splitting may split
abbreviations; offsets are reliable but semantic boundaries are not guaranteed.

Markers are lexical cues, not a verified list of facts the question needs.
Adjacent context can be irrelevant, and important qualifiers may have no marker.
The fixed byte cap applies to evidence text; the full serialized messages are
counted conservatively for API reservation. Same cap does not mean identical
actual bytes, token usage or information. Model citations name parent paragraphs;
reported parent Evidence F1 must not be interpreted as fragment-level support.

## Development protocol and implementation gate

The first prepared variant only reweighted already-relevant sentences. Offline
comparison found identical plain/qualifier contexts on all12 questions, so **no
API calls were made for that variant**. Keep its plan as a discarded preparation,
not a completed experiment. The second variant adds adjacent qualifier sentences
and differs on5/12 contexts, before any new answers were observed. The remaining
7 identical-context pairs act as repeated-input diagnostics; no effect on them
can be attributed to the memory selector.

Use12 reused QASPER train questions:8 unanimously answerable and4 unanswerable,
selected by fixed ID hash within strata from the previous24-question pilot. Do
not filter on previous answer scores. One question per paper, shuffled seed136;
six method permutations repeated twice, four positions per method. All three use
the same answer instruction, output512-token cap, JSON contract and temperature0.
Formal test remains untouched. This is development and cannot provide independent
confirmation. Three-way requests are rebuilt through the same budget runner;
old plans are never replayed after runner source changes.

Primary exploratory contrast: qualifiers minus plain memory in Answer F1; also
report full context and answerable/unanswerable groups, all failed/missing outputs,
token counts and conservative cost. Report the5 changed-context subset separately
without pretending it was an outcome-independent held-out dataset: it is an input
diagnostic. No-reference text is ever sent to the answer model.

The official Chinese DeepSeek pricing index was checked2026-10-07; configured
peak cache-miss estimates remain CNY2/M input and CNY8/M output:
https://api-docs.deepseek.com/zh-cn/quick_start/pricing/?helper=penn
Second-variant maximum36 calls reserves CNY0.621746, combined cumulative ceiling
CNY14.400962 below15. Supplier billing is not reconciled. Zero automatic retries,
90-second cooperative timeout, stop on failure, retain unknown-use reservations.

## Reproduce and test

The release bundle contains the pinned dense embeddings/public sources,
`memory-mode.json` (version `extractive-v2`) and exactly three requests per selected
question. `qasper_memory.memory_inputs` rebuilds and checks the complete bundle.
The existing runner validates hashes and one-time execution, saves source
snapshots and raw responses, and disables paid execution when finished.

```
python -m unittest jiuwenswarm.research_workbench.test_qasper_memory
```

Tests cover Unicode byte limits, offsets, source identity, deterministic output,
unique parent IDs, zero-fitting contexts, adjacent qualifiers without query terms,
unrelated-paragraph exclusion, and request/provenance tampering. Existing runner
tests cover budget limits, transport serialization and zero retries. Do not
manually edit cached requests to bypass reconstruction.
