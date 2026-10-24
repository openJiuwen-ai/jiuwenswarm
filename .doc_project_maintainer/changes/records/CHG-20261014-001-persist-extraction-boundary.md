---
id: CHG-20261014-001
title: Treat the Persist Session 70 percent boundary as a trigger
type: fix
date: 2026-10-14
modules:
  - agent-harness
flows:
  - eternal-conversation-memory
confidence: unit-verified
---

# Treat the Persist Session 70 Percent Boundary as a Trigger

## What Changed

Persist Session no longer rejects an Extractor fork when the frozen Worker input reaches or exceeds 70% of the selected model context window. The ratio now has one responsibility: it marks the extraction boundary and produces an audit warning. The exact Worker prefix is still retained and passed to the Extractor.

The former `within_seventy_percent` validity predicate and the second hard rejection in `BackgroundAgentRunner` were removed so the trigger cannot be reused as an admission rule.

## Verification

The focused Persist Session Rail and DeepAgent suites pass 94 tests. Ruff passes on the touched implementation and test files. Live recovery of the paused 200-task acceptance run remains separate evidence and is not claimed by this record.
