---
symbol: AgentWarmPool
kind: class
source: jiuwenswarm/server/runtime/agent_warm_pool.py
source_role: runtime_source
audit_scope: default_health_audit
health:
  overall: unknown
audit:
  status: unaudited
  confidence: confirmed
---

# AgentWarmPool

## Actual Role

Maintains at most one speculative READY or warming session globally across eligible channel/project/work-mode keys, prevents obsolete configuration revisions from publishing, and hands claimed initialization Futures to the chat path. It prioritizes the initial Web/work/default-project key and the most recently claimed key, promotes matching warming work, and cancels non-promoted speculative work when foreground chat begins.

## Key Contracts

- The pool is active unless `enabled` (default: the `JIUWENSWARM_AGENT_PREWARM` environment variable) opts out.
- Only single-Agent keys enter READY.
- Code keys acquire AgentManager roots with `mode=code, sub_mode=normal`; work keys use `mode=agent, sub_mode=None`.
- A claim is atomic; matching warming work retains its Session ID, and the claimed key is prioritized for replenishment after foreground chat.
- ACP/A2A and Swarm keys never enter READY.
- Foreground preparation does not acquire the background semaphore, but shares the process-global registry initialization lock.
- Preparation failure cannot publish READY.
- Markers never create normal user metadata.

Health remains unaudited; see the build plan for the pending audit slice.
