---
symbol: AgentWarmPool.sync
kind: method
source: jiuwenswarm/server/runtime/agent_warm_pool.py
class: AgentWarmPool
audit:
  status: unaudited
---

# AgentWarmPool.sync

## Actual Role

Returns zero statistics without touching revision or slot state while the pool is disabled. Otherwise filters ineligible channels, discovers visible/default projects off the AgentServer event loop, advances the config fingerprint revision, and reconciles an immutable target snapshot. Missing targets remain pending while only the bounded next background batch is started; the method returns statistics without waiting for initialization.
