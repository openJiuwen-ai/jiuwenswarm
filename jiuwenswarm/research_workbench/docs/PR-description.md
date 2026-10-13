# Add an auditable research workbench and offline evidence tool

Researchers using JiuwenSwarm need to preserve source bindings, rejected outputs, spending records and scoring provenance through paper preparation. This adds a bounded research workbench plus an offline native tool that audits saved experiments, prepares statistics and assembles an ICLR manuscript with figures.

The native tool accepts only audit/prepare/draft and does not run models or arbitrary commands. Paper input fingerprints detect stale ratings, analysis code, figures and PDFs. User acceptance of scoring is kept distinct from individual reviewer declarations. The companion model-settings UI keeps saving credentials opt-in.

Validation: local unit tests; hash checks on a frozen experiment; native adapter load/invoke/unload; offline PDF build; patch restoration. Exact counts and commit ID are recorded in the release verification. The observed experiment does not establish improved QA performance, and the implementation is not validated autonomous scientific discovery.

Scope: source and tests only. Do not attach API keys, raw model transcripts, teammate files or archived third-party PDFs to a public PR. No PR has yet been published.
