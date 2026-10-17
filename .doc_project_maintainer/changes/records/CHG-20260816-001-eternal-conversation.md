---
id: CHG-20260816-001
date: 2026-08-16
status: implemented
confidence: confirmed
---

# Add pluggable eternal conversation memory

## Scope

Adds an inert-by-default Rail shared by Deep/Code adapters, a process-level Session coordinator, append-only evidence, isolated Extractor/Builder model histories, the pinned dynamic-memory-cli Skill, and the pinned natural-task acceptance experiment.

## Architectural Decisions

- Foreground uses memory but never edits it; Extractor owns semantics and Builder owns Pending-to-Built review.
- Harness performs only recording, freezing, scheduling, structural/evidence/revision checks, atomic publication, and safe context replacement.
- Coordinator lifetime follows Session rather than disposable channel Adapter lifetime.
- The frontend runtime boolean is the V1 enablement boundary; no settings UI is added.
- Real ReactAgent creates ModelContext after `BEFORE_INVOKE`; projection is prepared there and applied during `ON_USER_MESSAGE` before query admission, with ContextProcessor operating on the replacement afterward.
- Extractor catch-up is bounded to four complete natural tasks per publication. Repeated model-visible messages are structurally deduplicated from the Extractor prompt while canonical Raw History remains authoritative.
- Large foreground/background inputs use content-addressed blobs. A cursor/hash-linked search projection and task-local derived indexes preserve bounded direct-evidence lookup without semantic summarization.
- Builder review is restricted to frozen-batch structural buildability; semantic omissions, wording, and conflict judgment remain exclusively with Extractor.
- Worker terminal errors are stored durably for fail-fast barriers and next-boundary recovery. Natural-task retries restore one immutable workspace baseline, wait for Extractor/Builder convergence, and retain every failed attempt as evidence.
- Structural retries include the exact prior invalid JSON plus field/index/length diagnostics, so the Extractor—not Harness—rewrites over-limit Snapshot semantics.
- Formal proof generation independently recomputes the Raw cursor/hash chain, verifies derived search/task indexes, records child proof hashes, and requires exactly 800 tasks plus 20 blind conflict probes at matrix level.
- Pre-content-addressing Extractor histories for all four formal Sessions were losslessly relocated to workspace archives after hash/byte capture; each original history path retains a verified archive manifest for transparent reconstruction and continued appends.

## Verification

- Dedicated Rail suite covers 32 integrity, recovery, isolation, catch-up, lifecycle, context-replacement, structural-retry, retry-baseline, evidence-index, Builder-boundary/fail-fast, and memory-state cases.
- The combined Eternal Conversation + existing Project Memory Rail regression set passes 63/63, confirming the pluggable Rail changes do not regress the adjacent project-memory Rail.
- AgentServer mode, Session lifecycle, and Web handler regression suites pass.
- The acceptance runner resolves an explicitly selected configured model and records both alias and actual model. User-selected formal execution currently uses alias `mass` (actual `glm-5`); no mock fallback is permitted.
- Formal Web/Work has demonstrated repeated real Snapshot replacement: model input dropped from hundreds of accumulated messages to the post-replacement working set while subsequent natural development tasks continued to pass. The current strict-sequence Web/Work run is 115/200; retained Web-Code/TUI-Work/TUI-Code runs remain paused until Web/Work is accepted. Execution is paused because `mass/glm-5` now returns `403 ModelArts.810006: The resource is frozen`, while the other configured `GLM-5` endpoint at `127.0.0.1:8000` refuses connections. The failure and restored task-116 checkpoint are audited; no mock, Qwen, or DeepSeek evidence was substituted.
