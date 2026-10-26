---
symbol: AgentWebSocketServer._handle_session_delete
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_session_delete`

## Actual Role

Implements the destructive `session.delete` RPC. It validates only presence and filesystem shape, gates work on persistent-checkpointer availability, chooses team cleanup from persisted metadata or ordinary `Runner.release`, recursively removes the session directory, clears the plan-exit and metadata caches, and sends one locked E2A response. The runtime, filesystem, and cache mutations are sequential rather than transactional.

## Key Signals

- Input: `params.session_id`; metadata selects `TeamManager.delete_session_runtime` for team mode, otherwise `Runner.release`.
- Output: coded errors or `{session_id: target}` success; encoding and send locking stay local.
- Side effects: releases checkpoint/team state, calls synchronous `shutil.rmtree`, discards `_plan_exited_sessions`, and evicts metadata cache.
- Main risks: unchecked path composition reaches recursive deletion; active/local state is only partly coordinated; release, filesystem deletion, and cache eviction are non-atomic; synchronous rmtree can block the event loop.
- Test evidence: two direct tests cover ordinary success and checkpointer rejection; adjacent TeamManager tests cover team-runtime deletion. No tests were run for this documentation-only re-audit.
- Related flow: `agentserver-session-lifecycle` independently records the missing normalized-ID/containment contract and partial multi-store cleanup semantics.

## Detail Index

- Detail docs pending.
