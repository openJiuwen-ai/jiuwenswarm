---
name: research-evidence-workflow
description: Audit the frozen competition study and prepare evidence-bound paper materials using the local research_evidence tool.
---

# Research evidence workflow

Use the separately installed research-evidence-offline plugin; if its research_evidence
tool is unavailable, report that prerequisite. Do not improvise shell or model calls.

1. Call research_evidence with action=audit. Stop on any integrity error.
2. Call research_evidence with action=prepare. Follow its stages: frozen sources,
   frozen protocol, saved experiment, human review gate, statistics, writing inputs.
3. Cite the returned report and manifest. Distinguish software validation, execution
   completion, and semantic effectiveness. Do not expose per-method mappings to raters.
4. Use only the verified writing_materials and resources for draft descriptions.
   If statistics.status is waiting_for_human_reviews, leave results and conclusions pending.
5. Missing human confirmation, identity, date, or exposure declaration is not a negative
   score. Never impersonate a reviewer or turn AI-assisted drafts into human ratings.
6. Report the next actual dependency. This workflow cannot run new experiments,
   create Reviewer tokens, upload, or submit. Host chat inference may still cost money;
   only the tool operations themselves are offline. Keep API execution disabled unless
   the user separately authorizes a concrete run within the recorded budget.

Current research scope: fixed-memory paper QA; catalog excerpts x output evidence
contract in a 2x2 study. This is not validated autonomous end-to-end scientific discovery.


The research_evidence tool also accepts {"action":"draft"}. It assembles an English
development manuscript and evidence manifest in a new research/paper-native-<id> directory.
Missing scores stay pending; completed scores use the fixed renderer, including negative effects.
This action makes no model or compiler calls. Compile later with that directory's build.py
and --research <research-directory>, then verify before author review. It is not final approval.
