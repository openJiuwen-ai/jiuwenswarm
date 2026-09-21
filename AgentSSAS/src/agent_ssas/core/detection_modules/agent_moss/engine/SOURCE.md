# AgentMoss engine snapshot

This directory embeds the deterministic AgentMoss analysis core used by
JiuwenSwarm. It was imported from `agentmoss/agentmoss_core` in the local
JiuwenSwarm checkout whose base commit was `bd73bca9`; package imports were
changed to relative imports for AgentSSAS.

The graph implementation and its AgentSSAS-facing identifiers use the
Agent Behavior Graph terminology.

Upstream snapshot hashes recorded before the import; the graph source is listed
under its current AgentSSAS file name:

- `events.py`: `72eb6b6d8484e5856f1e0679bf61f995ef213f9db05db2ae0c37bcd0d15ddc89`
- `policy.py`: `541720c40b0dafa7eb1d8d92ae2e90f1147e353176b9a5c6aaae6514496420a1`
- `agent_behavior_graph.py`: `035703303ecab8cc2a1d2657206584c82283364bc690cca00bc3ebbb8986a8af`

The AgentSSAS-specific event conversion and report mapping remain outside this
directory so that future engine refreshes can be compared mechanically.
