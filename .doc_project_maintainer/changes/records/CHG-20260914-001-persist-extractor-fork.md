---
id: CHG-20260914-001
title: Fork the Persist Session Extractor from the Worker model prefix
type: refactor
date: 2026-09-14
modules:
  - agent-harness
flows:
  - eternal-conversation-memory
confidence: live-provider-verified
---

# Fork the Persist Session Extractor from the Worker Model Prefix

## What Changed

The Persist Session Rail now freezes the final successful Worker model request at a natural-task boundary. When that request consumes no more than 70% of the selected model's context window, the Extractor is built as a guarded OpenJiuwen DeepAgent fork: it inherits the exact Worker system prompt, message prefix, model-facing tool definitions, selected model request configuration, parent Session lineage, and KV-cache affinity configuration. The fork then receives exactly one new user message containing the adapted Extractor contract and frozen evidence request.

The fork exposes the same tool schemas to preserve the cache prefix but a guard Rail rejects execution, so semantic extraction cannot mutate the foreground workspace. Builder remains an independent coding DeepAgent in its private workspace. Fixed foreground byte thresholds were removed; the existing context-window processor remains responsible for foreground sizing.

Automatic Worker retrieval now derives its query only from the user-authored text, while leaving the Worker prompt untouched. Extractor UTs carry an explicit priority (20/40/60/80/100); retrieval orders higher-priority constraints first. Constraint assessments fail closed unless each relevant UT is preserved, explicitly overridden by direct user evidence, or surfaced as an unresolved Snapshot notice. The structural validator canonicalizes the unambiguous `id` alias to `ut_id`, while rejecting conflicting aliases.

## Verification

Ruff passed on the touched implementation and test files. The focused Persist Session suite passes 66 tests after the acceptance-gate additions. A real-model Web Code run using `DeepSeek-V4-Flash-0731` completed and passed all 200 natural tasks, all five hidden conflict probes used memory search, 49 context replacements completed, final pytest returned zero, and Raw History verified through cursor 12829. All 511 Extractor forks preserved the exact byte prefix. Provider-reported KV cache reuse remained diagnostic because the configured endpoint routes model calls; 221/511 calls reported a full provider hit. The prior task-124 `MemoryError` did not recur.
