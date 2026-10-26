# Distributed Team

This guide is for **development and integration testing**: how distributed Team (`team.runtime.mode=distributed` + `pyzmq`) maps to AgentServer / `TeamManager`, where config and code live, and how to run leader and teammate from two separate config roots for end-to-end verification. There is no separate runtime binary; the entry point remains the unified AgentServer.

The main config file is usually `~/.jiuwenswarm/config/config.yaml`. Override the directory with `JIUWENSWARM_CONFIG_DIR` (same as [Configuration](Configuration.md)).

[中文版（Chinese）](../zh/分布式Team.md)

---

## 1. Overview

| Item | Description |
|------|-------------|
| **Mode** | `team.runtime.mode`: `local \| distributed` |
| **Role** | `team.runtime.role`: `leader \| teammate` |
| **Transport** | `team.transport.type`: `inprocess \| pyzmq`; distributed setups typically use `pyzmq` |
| **Entry** | `TeamManager` (`jiuwenswarm/agents/harness/team/team_manager.py`): normalizes transport / identity before building `TeamAgentSpec` |
| **Loading** | `load_team_spec_dict()` (`jiuwenswarm/agents/harness/team/config_loader.py`): `name` / `display_name` compatibility for leader and `predefined_members` |
| **Sample** | `jiuwenswarm/resources/config.team.distributed.leader.yaml` / `config.team.distributed.teammate.yaml` (current role-specific templates) |

**Session semantics**: distributed mode retains **single active session** per channel — creating or switching to a Team for a new session first tears down other active or pending session Teams in the same channel, so remote member bootstrap, transport connections, and runtime resources are not reused across sessions. **Local mode** instead allows multiple Team sessions to run concurrently in the same channel and does not apply this single-session switch policy.

> **⚠️ Multi-TUI-window limitation**: Multiple TUI windows cannot run Team tasks concurrently under distributed mode — starting a Team session in a new window will automatically stop the existing Team session. 

---

## 2. Config keys you will touch

Typical keys for distributed integration (templates: `config.team.distributed.leader.yaml` / `config.team.distributed.teammate.yaml`).

| Key | Meaning |
|-----|---------|
| `team.runtime.mode` | Set to `distributed` for distributed semantics |
| `team.runtime.role` | Whether this process is `leader` or `teammate` |
| `team.runtime.member_name` | Default teammate identity; after bootstrap it adopts the member name dynamically requested by the leader |
| `team.transport.type` | `pyzmq` |
| `react.a2x_registry` | Teammates register idle nodes at startup; leaders reserve idle teammates from the registry before teaming. **The registry is not bundled with jiuwenswarm**: clone upstream [agent-protocol (`feature/Agentregistry`)](https://gitcode.com/openJiuwen/agent-protocol/tree/feature/Agentregistry) and deploy it as a separate service per that repo's instructions |
| `team.transport.params` | This process' `direct_addr` / `bootstrap_direct_addr`, `pubsub_*`, etc.; leaders do not need static teammate `known_peers` |
| `team.predefined_members` | Backward-compatible static member declaration; not required for current blank-teammate integration |
| `team.storage` | For multi-process setups, `connection_string` must point to a **shared** DB (e.g. the same sqlite path visible to all nodes) |

---

## 3. pyzmq Transport Field Normalization

When `transport.type == pyzmq` and **`pubsub_publish_addr` / `pubsub_subscribe_addr` not both set**, `TeamManager` auto-fills from topology:

| Field | Description |
|-------|-------------|
| `direct_addr` | Direct communication address |
| `pubsub_publish_addr` | Publish address |
| `pubsub_subscribe_addr` | Subscribe address |
| `known_peers` / `bootstrap_peers` | Node discovery list |
| `metadata.pubsub_bind` | Bind pubsub (leader=True, teammate=False) |

Default ports:
- Leader: `direct_port=18555`, `pub_port=18556`, `sub_port=18557`
- Teammate: `direct_port=18600`

---

## 4. PostgreSQL Bootstrap (Leader Role)

When `team.storage.type=postgresql` and role is `leader`, startup auto-checks PostgreSQL availability:

1. Check `pg_isready -h <host> -p <port>`
2. If unreachable, attempt to start local cluster:
   - Try `pg_ctlcluster <version> <cluster> start`
   - Fallback to `systemctl start postgresql` or `service postgresql start`
3. Wait up to 30 seconds for service ready

Config example:

```yaml
team:
  storage:
    type: postgresql
    params:
      connection_string: postgresql+asyncpg://user:pass@host:5432/teamdb
```

---

## 5. teammate_mode and spawn_mode

| Config | Value | Description |
|--------|-------|-------------|
| `teammate_mode` | `build_mode` (default) | Teammate built via build flow |
| `spawn_mode` | `inprocess` (default) | Teammate runs in same process |

---

## 6. Where to look in code

### 3.1 `TeamManager._load_team_spec`

Pipeline: `load_team_spec_dict(session_id)` → **`_normalize_team_identity_fields`** → if distributed, **`_normalize_distributed_transport_fields`** → `TeamAgentSpec.model_validate`.

Distributed mode detection: **`_is_distributed_mode`** (`runtime.mode == distributed` or `transport.type == pyzmq`).

### 3.2 pyzmq field normalization (bootstrap)

When `transport.type == pyzmq` and **`pubsub_publish_addr` / `pubsub_subscribe_addr` are not both set**, `params.leader` / `params.teammate` (and related fields) are used to fill **`direct_addr`, `pubsub_*`, `metadata.pubsub_bind`**. The current role-specific templates provide runtime-ready fields directly; teammate discovery is handled through the A2X registry instead of static leader-side peer config.

### 3.3 `config_loader`

- **`_build_leader_spec`**: keeps `name` and `display_name` consistent.
- **`_build_predefined_members`**: requires `member_name` and **`name` or `display_name`**; otherwise the entry is skipped and logged.

### 3.4 Current branch behavior: control plane vs data plane

The current implementation is explicitly split:

- **Control plane**:
  - Teammate registers its `bootstrap_direct_addr` as an idle A2X node at startup.
  - Leader config does not contain concrete teammate names or addresses; it only needs the A2X registry URL and dataset.
  - Leader calls `reserve_blank_agents` during teaming / `spawn_member`, then sends bootstrap using the returned `service_id` / `endpoint`.
  - Leader sends bootstrap through direct ZMQ (`jiuwen.remote_teammate_bootstrap.direct`) after `spawn_member`.
  - Teammate listens on `bootstrap_direct_addr`, applies leader route, and adopts the target member.
  - After successful bootstrap, the teammate uses its local A2X `service_id` to call `replace_agent_card`, replacing its registry card from blank/idle to busy/member. This prevents the same teammate from becoming reservable again after the reservation TTL expires.
  - On the remote spawn path, the leader forces the member to **`unstarted`** after the roster row is written, keeps it **`unstarted`** after direct bootstrap delivery, and only sets **`ready`** when the teammate sends ``jiuwen.remote_bootstrap_ack`` on the team **MESSAGE** channel (leader ACK listener). Direct ZMQ is for bootstrap payload delivery only, not for deciding member READY status.
  - Reservation lifecycle: the leader releases immediately when bootstrap delivery fails; after successful bootstrap, the leader does not actively release that reservation.
  - When the Team is dissolved, the leader sends `jiuwen.remote_team_destroy.direct` to each reserved teammate over direct ZMQ. The teammate cleans up its local session/team runtime, then uses A2X `replace_agent_card` to reset its own agent card back to idle teammate state; `bootstrap_direct_addr` stays alive so it can accept the next bootstrap.
  - On the teammate side, bootstrap may temporarily build an auxiliary `TeamAgent` to read shared DB/context. That helper must not remain cached in `TeamManager._team_agents`; after context construction it must stop its runtime/messager and be removed from the cache.
  - The real dynamic teammate runtime retargets its in-process loopback `direct_addr` to an available port instead of reusing the agent-core default `tcp://127.0.0.1:16000`, avoiding publish/event port conflicts.
- **Data plane**:
  - Business messages/tasks (create/claim/complete, normal team messaging) continue through team runtime + shared storage.
  - `team.storage` shares business state such as tasks, member status, and messages. The default `team-workspace` directory is still created under each process' own HOME; it is not a cross-process physical shared directory by itself.
- **Fallback policy (current)**:
  - Leader no longer falls back to `team_message` when direct bootstrap send fails.
  - Teammate no longer uses DB polling fallback for bootstrap intake.
- **Local-mode isolation**:
  - `TeamManager` attaches remote bootstrap hooks only for distributed configs; local / inprocess Team does not execute A2X registration, reservation, or remote bootstrap logic.

---

## 4. Current recommended config usage (complete templates)

The role templates in the repo are now **complete `config.yaml` files**. They include the base agent/model config, A2X registry config, the top-level `team` runtime marker, and the actual `modes.team.jiuwen_team` TeamAgentSpec config. For deployment, copy one template directly into the matching HOME config path; no manual merge with the default `config.yaml` is required.

- `jiuwenswarm/resources/config.team.distributed.leader.yaml`
- `jiuwenswarm/resources/config.team.distributed.teammate.yaml`

Suggested workflow:

1. Copy each complete template into the matching config root (`<LEADER_HOME>/.jiuwenswarm/config/config.yaml` and `<TEAMMATE_HOME>/.jiuwenswarm/config/config.yaml`).
2. Adjust:
   - `react.a2x_registry.base_url` / `dataset` so leader and teammate use the same registry dataset.
   - teammate `team.transport.params.bootstrap_direct_addr` or `react.a2x_registry.endpoint` so the registry advertises a reachable address.
   - `team.storage.params.connection_string` (must be shared and identical on both sides).
   - teammate `team.runtime.member_name` as its default local identity; leader no longer uses it for address lookup.
   - IPs/ports under `team.transport.params.*` and `modes.team.jiuwen_team.transport.params.*` (do not use loopback-only `127.0.0.1` values for multi-host deployments).
3. Prepare model environment variables before startup, for example `API_BASE` / `API_KEY` / `MODEL_PROVIDER` / `MODEL_NAME`; secrets in the templates remain environment-variable placeholders or empty strings.

Minimal ready-to-use copy commands for the complete templates:

```bash
# leader
mkdir -p "<LEADER_HOME>/.jiuwenswarm/config"
cp "<REPO_ROOT>/jiuwenswarm/resources/config.team.distributed.leader.yaml" \
  "<LEADER_HOME>/.jiuwenswarm/config/config.yaml"

# teammate
mkdir -p "<TEAMMATE_HOME>/.jiuwenswarm/config"
cp "<REPO_ROOT>/jiuwenswarm/resources/config.team.distributed.teammate.yaml" \
  "<TEAMMATE_HOME>/.jiuwenswarm/config/config.yaml"
```

---

## 5. Two config directories (recommended layout)

Use **two separate HOME trees** (or two `JIUWENSWARM_CONFIG_DIR` values) for leader and teammate so configs do not overwrite each other.

Placeholders:

- **Leader config dir**: `<LEADER_HOME>/.jiuwenswarm/config`
- **Teammate config dir**: `<TEAMMATE_HOME>/.jiuwenswarm/config`

Both sides must agree on:

- `team.runtime.mode=distributed`
- `team.runtime.role` as `leader` vs `teammate`
- `react.a2x_registry` pointing at the **same registry dataset**
- Teammate advertises its own bootstrap endpoint; leader does not need teammate addresses
- `team.storage.params.connection_string` pointing at the **same database** (for example PostgreSQL, or a sqlite file visible to all nodes)

Note: the distributed templates explicitly configure the team workspace root:

```yaml
team:
  workspace:
    enabled: true
    root_path: ${JIUWEN_TEAM_WORKSPACE_ROOT:-/tmp/jiuwenswarm/shared_workspace/jiuwen_team}
    version_control: false
```

Use the same `JIUWEN_TEAM_WORKSPACE_ROOT` on every node, and only share that directory over NFS. Do not share `.agent_teams`: it stores team.db, member workspaces, symlinks, and other local runtime state; sharing it across nodes can break kickoff and workspace initialization.

NFS scripts, checks, and teardown: see `scripts/nfs/README.md`.

Unless both sides explicitly configure a jointly visible workspace root, leader and teammate create local directories under their own HOME:

- `<LEADER_HOME>/.jiuwenswarm/.agent_teams/<team_name>/team-workspace`
- `<TEAMMATE_HOME>/.jiuwenswarm/.agent_teams/<team_name>/team-workspace`

These paths have the same shape but are not the same physical directory. If the leader must directly read files written by a teammate, configure a shared `team.workspace.root_path` or return results through messages, DB state, or file transfer tooling.

Open firewall ports as needed; replace `127.0.0.1` with real IPs for multi-host setups.

---

## 6. Example startup (four terminals)

Replace `<REPO_ROOT>`, `<LEADER_HOME>`, `<TEAMMATE_HOME>` with paths on your machine.

### 6.1 A2X Registry

Run the registry **as its own process**, separate from leader/teammate:

Follow the [agent-protocol Agent Team quick start](https://gitcode.com/openJiuwen/agent-protocol/blob/feature/Agentregistry/README_forAgentTeam.md). Since `0.1.6`, the default install is the lightweight Agent Team build: SDK, FastAPI, uvicorn, and a few small runtime dependencies only. The registry backend starts empty; it does not need preloaded data or LLM config. Teammate registration, leader lookup/reservation, and reservation leases are handled by the `jiuwenswarm` client-side integration.

Install (Python >= 3.10):

```bash
git clone -b feature/Agentregistry https://gitcode.com/openJiuwen/agent-protocol.git
cd agent-protocol
pip install -e .
```

Single-host setup (registry, leader, and teammate on one machine):

```bash
a2x-registry
```

It listens on `127.0.0.1:8000` by default. Configure both leader and teammate with:

```yaml
react:
  a2x_registry:
    base_url: http://127.0.0.1:8000
```

For multi-host setups, bind the registry to an address reachable from other machines and open the firewall / security group port:

```bash
a2x-registry --host 0.0.0.0
a2x-registry --host 0.0.0.0 --port 8080
```

Then set `react.a2x_registry.base_url` on leader and teammate to the registry host IP, domain, or HTTPS reverse-proxy URL, for example `http://192.168.1.10:8000` or `https://registry.example.com`.

### 6.2 Teammate (AgentServer only)

```bash
HOME="<TEAMMATE_HOME>" \
GIT_AUTHOR_NAME="teambot" \
GIT_AUTHOR_EMAIL="teambot@example.com" \
GIT_COMMITTER_NAME="teambot" \
GIT_COMMITTER_EMAIL="teambot@example.com" \
AGENT_SERVER_PORT=28193 \
uv run python -m jiuwenswarm.server.app_agentserver
```

After startup, the teammate registers its `bootstrap_direct_addr` as a blank agent, for example `endpoint=tcp://127.0.0.1:28610`.

### 6.3 Leader (Gateway + AgentServer)

```bash
HOME="<LEADER_HOME>" \
GIT_AUTHOR_NAME="teambot" \
GIT_AUTHOR_EMAIL="teambot@example.com" \
GIT_COMMITTER_NAME="teambot" \
GIT_COMMITTER_EMAIL="teambot@example.com" \
AGENT_SERVER_PORT=28192 \
GATEWAY_PORT=29101 \
WEB_PORT=29100 \
uv run python -m jiuwenswarm.app
```

Leader does not need a static teammate endpoint; `spawn_member` obtains an idle teammate through registry `reserve_blank_agents`.

### 6.4 Web UI (optional)

```bash
cd "<REPO_ROOT>/jiuwenswarm/channels/web/frontend"
VITE_WS_BASE="ws://localhost:29100" npm run dev -- --host 0.0.0.0 --port 5173
```

If Git user identity is not configured for the workspace, set `GIT_AUTHOR_*` / `GIT_COMMITTER_*` so Git-based tooling does not fail.

---

## 7. Verification prompt (team workflow)

Use a strict prompt in the web UI (or equivalent channel), adapted to your environment:

```text
[Distributed Team integration check]
You MUST run in team mode and complete the steps in order. Do not skip steps. Do not answer the math directly first.
1. Call team.build_team to create the team (leader + teammate_1).
2. Call team.create_task with title "compute 1+1" and assignee teammate_1.
3. Call team.send_message to teammate_1 asking for the result of 1+1 and one short sentence.
4. Wait until teammate_1 completes and responds.
5. Call team.view_task and confirm the task is completed (or equivalent).
6. Have the leader summarize the final answer.
Output format:
- STEP1: <result>
- STEP2: <result>
- STEP3: <result>
- STEP4: <result>
- STEP5: <result>
- FINAL: <final answer>
If any step fails, output FAILED_AT_STEP=<n> and the error.
```

### Success criteria (short)

- UI receives `chat.delta` and eventually `chat.final`.
- Leader logs: Team creation, `team.*` tool usage.
- Teammate logs: participation in session and task coordination.

---

## 8. Troubleshooting

| Symptom | What to check |
|---------|----------------|
| `Address already in use (tcp://0.0.0.0:18555)` | pyzmq bind port in use; free the port or change `direct_port` / topology ports in config. |
| `git commit failed ... Author identity unknown` | Export `GIT_AUTHOR_*` / `GIT_COMMITTER_*` in the startup command. |
| UI idle while backends run | Frontend must use `VITE_WS_BASE` (not `VITE_WS_URL`). |
| Teammate cannot reach leader | Firewall, or the leader address sent in bootstrap is still `127.0.0.1` on a multi-host setup. |
| Leader did not get a teammate from registry | Check registry logs for `POST /api/datasets/<dataset>/reservations 200 OK`; check teammate blank-agent registration succeeded. |
| Teammate can be reserved twice too early | Check teammate logs after bootstrap for `teammate agent card replaced ... member_name=...` / `teammate registry card replace ... replaced=True`; without this, the registry still sees it as blank/idle and may reserve it again after the reservation TTL expires. Also confirm the leader does not release the reservation immediately after successful bootstrap. |
| Teammate cannot bootstrap again after Team dissolve | Check teammate logs for `teammate applied team destroy notification ... cleaned=True`; `cleaned=False` or `cleanup failed` means the old team runtime / messager may still be partially alive. |
| `Address already in use (tcp://127.0.0.1:16000)` | The teammate process may still have an auxiliary `TeamAgent` or old dynamic runtime alive. Confirm the bootstrap helper is removed from `TeamManager` cache and its messager is stopped after context construction, and that dynamic runtime retargeted to a fresh `direct_addr`. |
| Leader and teammate both have `team-workspace/result.txt` with different contents | Default workspace paths are local to each process HOME, not a shared filesystem. Use a jointly visible path or return teammate results through messages/storage. |

---

## 9. Appendix: vs single-machine / inprocess Team

| Aspect | Single-machine / inprocess | Distributed (this guide) |
|--------|----------------------------|---------------------------|
| Entry | Same `TeamManager` | Same entry; behavior split by config |
| Transport | Mostly `inprocess` | `pyzmq`; hosts and ports must be reachable |
| Deployment | Single process | Leader/teammate can be separate processes or hosts |
| Config | Local `team` block suffices | Needs `runtime` + `transport` + shared storage agreement |

For deeper topology evolution, maintain a separate design note alongside this guide; day-to-day work follows **sections 2–7**.
