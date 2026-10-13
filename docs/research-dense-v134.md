# Local semantic retrieval baseline (v134)

This is a development baseline, not a new research contribution. It uses the
official BAAI/bge-small-en-v1.5 ONNX model on CPU, without sending paper text to a
model service. Do not interpret evidence retrieval as answer accuracy.

## Fixed model and dependencies

- Model: https://huggingface.co/BAAI/bge-small-en-v1.5
- Revision: `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`
- ONNX SHA256: `828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35`
- Tokenizer SHA256: `d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66`
- License: MIT as declared by the pinned official model card. Preserve that card
  and the acquisition manifest alongside downloaded files. We do not redistribute
  the model in the contribution patch.
- Existing environment provides numpy, onnxruntime and tokenizers; actual versions
  are saved in summary.json. No torch, remote code or new package installation.
- Query instruction: `Represent this sentence for searching relevant passages: `.
  Passage text has no instruction prefix. CLS pooling, 384 dimensions, L2
  normalization, cosine similarity; right truncation at 512 tokens, batch size 16,
  CPUExecutionProvider with 4 intra-op and 1 inter-op threads.

The model directory requires `onnx/model.onnx`, `tokenizer.json` and a
`manifest.json` whose `files` map records the SHA256 for each. The archived official
README, config and tokenizer_config make the download traceable. Runtime checks the
two execution files against the manifest. Keep the manifest hash under versioned
experiment records; it is an integrity record, not a cryptographic signature.

## Reproduce

From the JiuwenSwarm checkout with its Python environment active:

```powershell
python -m unittest jiuwenswarm.research_workbench.test_qasper_dense jiuwenswarm.research_workbench.test_qasper_experiment -v
python -m jiuwenswarm.research_workbench.qasper_dense_eval --data <adapted-train-directory> --model <pinned-model-directory> --output <new-output-directory>
```

Input files are `public-papers.json`, `public-questions.json` and
`gold-references.json` from the v129 QASPER adapter. Use training data only. The
evaluator refuses an existing output directory. It never resumes silently. If a
local run is interrupted, retain its partial artifacts and run into a fresh
directory; no API budget is involved.

## Protocol

Select 64 train paper IDs by SHA256 of `dense-20261004:` plus paper ID, and keep
every question for those papers. No answerability or score filtering determines
this sample. Freeze plan.json and source snapshots before encoding. Gold bytes
are hashed for provenance then annotations are parsed only after retrieval.

Compare BM25 (existing implementation), BGE cosine, and reciprocal rank fusion
of both rankings with fixed denominator 60+rank. No weight fitting. Each uses
the existing whole-paragraph prefix selector with top8 and 12,000 UTF-8 bytes.
Different real bytes and compute remain possible; this is a common ceiling,
not a claim of equal cost. BM25 omits zero-score passages whereas cosine ranks
all passages; neither is an abstention classifier.

Save embeddings, row IDs, full rankings, selected IDs, input/source hashes,
truncation counts and a final artifact manifest. The saved arrays are NumPy
numeric arrays and are loaded with `allow_pickle=False`.

Metrics:

- Evidence F1: official maximum over references, all questions retained. Empty
  retrieved/empty reference evidence scores 1; this is not proof of reasoning.
- Positive reference recall: maximum fraction covered among nonempty answerable
  references, excluding questions with no such reference from this metric only.
- Complete reference coverage: whether at least one nonempty answerable reference
  is fully contained in the retrieved text. Multiple references are not unioned.
- Figures and unmapped text stay in the reference denominator, and corresponding
  question counts are reported. Exact reference matching is not semantic judging.
- Paired 95% percentile intervals: 10,000 paper-cluster bootstrap draws, seed 134;
  question-weighted mean in both the point estimate and resamples. These are
  descriptive exploratory intervals, not a preregistered confirmatory test.

The entire train corpus was already inspected by previous retrieval development.
The sample is not an unseen test. Public pretrained model pretraining overlap
with these papers is unknown. A promising result must next pass an answer-level
comparison, including unanswerable questions, followed by a frozen held-out
evaluation. Formal QASPER test data is not read by this module.

## Integration boundary

The new module produces the same public-only `make_request` contract used by the
existing research pipeline. It is currently an offline retriever, not an enabled
paid backend in qasper_runner. Do not relabel dense requests as BM25 to bypass
that runner's reconstruction guard. A future paid adapter must validate the
pinned model/cache/source hashes and reconstruct the selected evidence, then
reuse budget reservation, zero retries, usage settlement and timeout handling.
Do not silently change or replay previously frozen paid experiment plans.
