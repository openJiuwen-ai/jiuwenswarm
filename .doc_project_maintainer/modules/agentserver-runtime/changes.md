# AgentServer Runtime Changes

- [CHG-20260713-001: Close AgentWebSocketServer method audit delivery](../../changes/records/CHG-20260713-001-agentserver-method-closure.md) records the knowledge-model change that closed 128 method audits, refreshed the session flow, and added five AgentServer flows. It does not describe a product-code change.
- [CHG-20260731-001: AgentServer-owned session prewarming](../../changes/records/CHG-20260731-001-session-prewarm-allocation.md) adds session allocation, warm-pool reconciliation, and channel ownership changes.
- [CHG-20260801-005: Prewarming off by default](../../changes/records/CHG-20260801-005-disable-session-prewarm.md) puts warm-pool reconciliation and claiming behind the `JIUWENSWARM_AGENT_PREWARM` opt-in, leaving allocation intact.
- [CHG-20260803-001: Prewarming on by default](../../changes/records/CHG-20260803-001-enable-session-prewarm-by-default.md) turns that switch into an opt-out; the dormant behavior it gates is unchanged.
- [CHG-20260803-002: Route TUI startup creation through AgentServer](../../changes/records/CHG-20260803-002-tui-external-session-create.md) adds the unified startup barrier, normal allocation, explicit-ID compatibility path, and dual-state lifecycle regressions.
- [CHG-20260816-001: Add pluggable eternal conversation memory](../../changes/records/CHG-20260816-001-eternal-conversation.md) adds the common Work/Code Rail, Session-owned background Agents, audited dynamic-memory-cli state, Web/TUI runtime flag, and real-model acceptance matrix.
