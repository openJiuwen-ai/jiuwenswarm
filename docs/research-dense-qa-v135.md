# Semantic retrieval QA pilot (v135)

Extends `qasper_runner` with an explicit dense-cache bundle branch. Existing
non-dense requests still use the original reconstruction contract. This changes
the runner code hash: archived experiments keep their source snapshots; do not
replay them with the new code.

`qasper_dense_bundle.reconstructed` verifies the pinned BGE weight/tokenizer
identity, public-input hashes, numeric array checksums, dimensions, norms and row
IDs. It recomputes BM25/cosine/RRF60 rankings and rebuilds prompts using the
existing public-only selector. It never reads gold. The request list must contain
every question exactly once for each of the three conditions and must exactly
equal the reconstructed prompts. Paths cannot escape the cache directory. Numpy
pickle loading is disabled. Cache provenance is bound to the experiment manifest,
not a proof against a malicious author replacing all files and hashes together.

The existing `make_plan`, `preflight`, reservation, settlement, zero retries,
90-second cooperative deadline and single-use batch guard are reused. Dense
cache files and dense reconstruction source hashes are added to the frozen plan.
All methods use one concise-answer plus completeness-check instruction. Do not
rename a dense request as a BM25 request or remove the reconstruction guards.

Protocol: 24 unique train papers, one question each, 16 unanimously answerable
and 8 unanimously unanswerable. Gold is used only for predefined strata and later
scoring. Mixed-answerability questions are excluded from this exploratory pilot,
not from the benchmark as a whole. Exclude paper IDs from saved prior paid
request artifacts and the previous 64-paper retrieval comparison. Select by a
fixed hash seed, shuffle question order with seed135, repeat all six method
permutations four times. Each method appears eight times in each position.
This cohort is new to the paid QA runs, but train has previously been inspected
by offline retrieval, so it is not an independent held-out test.

Three conditions: `bm25-check@8`, `bge-check@8`, `hybrid-check@8`. Same top8,
12,000 UTF-8 evidence byte ceiling, 512 output tokens, temperature0,
thinking disabled and JSON output. One run each; actual input tokens differ.
Only public questions and paper fragments go to the configured DeepSeek API.

The official Chinese pricing page was checked on 2026-10-04 through its indexed
official content after direct fetches timed out:
https://api-docs.deepseek.com/zh-cn/quick_start/pricing/?helper=penn
The conservative configured peak cache-miss rates are CNY2/M input and CNY8/M
output for deepseek-flash. This is an estimate, not a billing reconciliation.
Prepared maximum72 calls reserve CNY1.546102; previous reserve CNY12.233114;
combined bound CNY13.779216 is below the existing CNY15 total limit. Failed calls
retain their upper reservation. Keys are entered through hidden session input
and are not written to the bundle, manifest or source.

Report Answer F1 over all24 and by the16/8 groups, Evidence F1, abstentions,
valid/failed counts and resources. Missing/failed predictions remain in the
denominator. Question-level pairs are also paper-level pairs in this one-question
per-paper sample. Bootstrap intervals are exploratory. Standard BGE and RRF are
baselines, not our innovation. Answer F1 is lexical, not independent factual
verification, and cannot establish an improved Reviewer score.

Tests: new cache/model/row and prompt tampering rejection, full three-condition
execution through a fake transport, incomplete cohort rejection and replay guard;
existing SDK serialization, zero-retry and budget tests continue to run offline.
