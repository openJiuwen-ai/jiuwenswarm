# QASPER metric attribution

The metric semantics in `qasper_experiment.py` follow the official QASPER evaluator:
https://github.com/allenai/qasper-led-baseline/blob/afd0fb96bf78ce8cd8157639c6f6a6995e4f9089/scripts/evaluator.py

Upstream: Allen Institute for AI, `allenai/qasper-led-baseline`, Apache License 2.0.
The accompanying `QASPER_LICENSE` retains the upstream license. Answer normalization
and token F1 are identified by upstream as originating in the SQuAD v1.1 evaluator.

Local changes: compact metric functions, explicit unknown-prediction rejection,
data adaptation with annotation diagnostics, paragraph retrieval, public-only request
construction, and one output contract shared across experimental conditions.
Gold annotations are not supplied to the request builder or ranking function.
Empty-token F1, duplicate evidence denominators, missing-prediction handling and
independent maxima across reference answers/evidence retain upstream behavior.

QASPER dataset attribution (data is not bundled with these source files):
Dasigi et al. (2021), *A Dataset of Information-Seeking Questions and Answers
Anchored in Research Papers*, NAACL, https://aclanthology.org/2021.naacl-main.365/.
The official dataset card identifies CC-BY-4.0:
https://huggingface.co/datasets/allenai/qasper.

Generated synthetic metric-check predictions must never be presented as model answers.
