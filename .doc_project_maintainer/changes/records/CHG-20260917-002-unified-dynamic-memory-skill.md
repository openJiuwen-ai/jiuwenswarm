---
id: CHG-20260917-002
title: Unify Persist Session background role guidance in dynamic-memory-cli
type: refactor
date: 2026-09-17
modules:
  - agent-harness
flows:
  - eternal-conversation-memory
confidence: synthetic-integration-verified
---

# Unify Persist Session Background Role Guidance

## What Changed

Extractor and Builder now load the same vendored `dynamic-memory-cli` Skill. Role-specific contracts remain separate references selected by the Harness-assigned Agent Spec: Extractor owns semantic Snapshot and UT proposals, while Builder runs the deterministic staging helper without changing meaning. Each role retains its independent DeepAgent, workspace, history, prompt, tools, and Harness-enforced publication boundary.

The Builder helper moved under `dynamic-memory-cli/scripts`. Background workspace staging now copies only this canonical Skill. Git ignore and package rules keep the canonical Skill tracked while excluding obsolete role Skill directories, preventing local ignored files from masking clean-checkout or wheel failures.

The first fresh real-model Web Code run exposed an Extractor fork edge case at task 8: the model followed its inherited Worker behavior and called `read_file`, but the fork only had the inherited schema and no usable resource. The fork now keeps the exact model-visible schemas while the Persist Session guard Rail binds the inherited filesystem tools to a private Extractor workspace, limits the inherited shell schema to read-only staged `dynamic-memory-cli` commands, and denies unrelated Worker tools. No foreground project path or canonical memory tree is exposed.

Each extraction now receives a unique staged copy of published memory and dynamically rendered input/output paths. The appended fork message stops foreground work, declares the new tool workspace, requires memory-cli inspection, and requires the final proposal to be written with `write_file`. Harness accepts the result only when a memory-cli call and proposal write were observed and the written JSON exactly matches the final response. The workspace digest is part of the private SysOperation identity so a changed path cannot reuse an older framework sandbox.

If one complete Extractor Agent run exhausts its response-level retries, the Coordinator now retries that exact frozen batch once without waiting for another foreground task. The cursor range, staged memory, request, and fork context remain unchanged; the retry is audited and neither attempt can publish until all existing gates pass.

Extractor proposal validation now also runs at its run-scoped `write_file` boundary. When a proposal is structurally invalid, the model-visible tool result contains only a concise text error identifying the violated rule and asking for another write; it does not echo the proposal JSON, internal paths, or a stack trace. This lets the same ReAct invocation repair an oversized UT or similar contract failure before returning. The Harness repeats the authoritative validation after the Agent finishes, so tool feedback is corrective rather than a publication bypass. Worker and Builder tools, and non-proposal Extractor writes, are unchanged.

## Verification

The focused Persist Session suite passed 85 tests. The added integration tests execute the real `publish-pending`, `freeze-pending`, `build-pending`, Built-only test, and benchmark chain; build a real DeepAgent with schema-identical sandboxed tools; run staged memory-cli; reject arbitrary commands and escaped file paths; prove that dynamic workspaces receive distinct SysOperation identities; verify that rendered run paths plus the proposal-file equality gate are enforced; and prove that an invalid proposal write becomes only the expected text in the next model-visible ToolMessage before a corrected write succeeds. Ruff passed on every touched implementation and test file.

The earlier repository-wide Windows run remains platform-blocked by unrelated POSIX-only tests. This workspace/tool-binding revision has focused unit and synthetic integration evidence; a new real-model 200-task acceptance run is still required before treating it as a release-complete replacement for the earlier live proof.
