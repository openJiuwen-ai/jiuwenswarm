# Open Questions

- Which non-AgentServer modules should be prioritized after the initial AgentServer slice: Gateway message handling, agent adapters, team runtime, skill manager, memory, sandbox, or frontends?
- Should TypeScript frontend symbols be manually reviewed where the heuristic extractor marked files `pending_review`?
- Which AgentServer symbols require trusted per-symbol health audit first: request dispatch, stream cancellation, session rewind, MCP config mutation, sandbox policy mutation, or scheduler operations?
