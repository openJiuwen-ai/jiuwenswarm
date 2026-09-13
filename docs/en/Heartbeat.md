# Heartbeat

[简体中文](../zh/心跳.md)

---

## Concept Overview

### What is Heartbeat

Heartbeat is a periodic liveness probe sent by the Gateway to AgentServer at fixed intervals, used to verify connectivity and agent availability. If **`workspace/HEARTBEAT.md`** is configured, it can also drive the Agent to periodically execute tasks listed in the file. Results can be relayed to a chosen channel (default: web).

### Heartbeat vs Scheduled Tasks

| Feature | Heartbeat | Scheduled Tasks |
|---------|-----------|-----------------|
| **Trigger Mode** | Fixed time interval (seconds), e.g., hourly | Cron expression with complex scheduling rules |
| **Initiator** | Gateway actively sends probe requests to AgentServer | Independent Scheduler module |
| **Task Definition** | Task list defined in `workspace/HEARTBEAT.md` file | Tasks created via web UI with query, interval_hours, etc. |
| **Execution Mechanism** | HEARTBEAT.md content injected into chat request, following normal conversation flow | Independent execution sessions with history and status management |
| **Primary Use** | Service liveness, periodic task execution, status monitoring | Schedule specific tasks with one-time/periodic execution |
| **Result Relay** | Pushed to specified Channel via `heartbeat.relay` event | Stored in task execution history, viewable in web UI |

### Heartbeat Workflow

1. **Startup**: Gateway initializes `GatewayHeartbeatService` at startup, creating a periodic task based on configured `interval_seconds`.

2. **Periodic Scheduling**: After startup, the service enters a main loop, executing a heartbeat (`_tick`) every `interval_seconds`.

3. **Time Check**: Before executing the heartbeat, the service checks if the current time falls within the `active_hours` window; if not, the heartbeat is skipped.

4. **Request Construction**: Constructs an E2A protocol request containing heartbeat identification and HEARTBEAT.md read instructions, sending it to AgentServer.

5. **Agent Processing**: AgentServer identifies the heartbeat request, reads `workspace/HEARTBEAT.md` content, injects the task list into the query, and executes tasks following the normal conversation flow.

6. **Result Relay**: After execution, if `relay_channel_id` is configured (e.g., `web`), the heartbeat response is pushed to the frontend via the `heartbeat.relay` event, updating heartbeat status and history.

```
Gateway ──(E2A heartbeat request)──→ AgentServer ──(read HEARTBEAT.md)──→ Agent ──(execute tasks)──→ Return results
   │                                                                                              │
   └────────────────────────────────(heartbeat.relay event)────────────────────────────────────────┘
```

---

## Configuration

Three configuration methods are available: web UI, config file, or environment variables.

### 1. Web UI Heartbeat Panel

Open **Heartbeat** in the left sidebar to view and modify heartbeat configuration:

![](../assets/images/heartbeat1.png)

**Configuration Options:**

| Option | Description | Default |
|--------|-------------|---------|
| **Interval** | Heartbeat interval in seconds, must be > 0 | 3600 |
| **Relay Target** | Channel for relaying heartbeat results, typically `web` | web |
| **Active Window** | Heartbeat only fires within this time range (`HH:MM` 24-hour format) | Always active if not configured |

After modifying configuration, click save to write back to `config/config.yaml` and automatically restart the heartbeat service.

![](../assets/images/heartbeat2.png)

### 2. Config file `config/config.yaml`

Configure the `heartbeat` section in `config/config.yaml`:

```yaml
heartbeat:
  # Interval in seconds; default 3600
  every: 3600
  # Channel for relaying results (e.g., "web" = web UI)
  target: web
  # Active window in local time; heartbeat only within this range; omit for 24/7
  active_hours:
    start: 08:00
    end: 22:00
```

| Field | Meaning | Notes |
|-------|---------|-------|
| `every` | Interval (seconds) | Must be > 0; e.g., 60 = every minute, 3600 = hourly |
| `target` | Relay channel | Usually `web`, which pushes heartbeat responses to the web UI; empty or omitted = no relay |
| `active_hours` | Active window | `start`/`end` in `HH:MM` (24-hour format). Heartbeat only fires within `[start, end]`. Supports cross-midnight windows (e.g., 22:00–06:00). |

### 3. Environment variables (override YAML)

| Variable | Meaning | Example |
|----------|---------|---------|
| `HEARTBEAT_INTERVAL` | Interval (seconds) | `3600` |
| `HEARTBEAT_RELAY_CHANNEL_ID` | Relay channel | `web` |
| `HEARTBEAT_TIMEOUT` | Single heartbeat timeout (seconds) | `30` |

Environment variables take precedence over the `heartbeat` section in `config/config.yaml`.

---

## Viewing Heartbeat

### Heartbeat History

View the last 20 heartbeat records in the Heartbeat panel, including status (normal / warning), content, and timestamps.

![](../assets/images/heartbeat3.png)

### Popup Notifications

When `target` is set to `web`, each heartbeat response is pushed to the frontend via the `heartbeat.relay` event. If content is not `HEARTBEAT_OK`, a popup notification appears for viewing task results or error information.

![](../assets/images/heartbeat4.png)

---

## Examples

### Example: Hello World

Add the following content to `HEARTBEAT.md`:

```markdown
Please output "Hello Heartbeat!"
```

When heartbeat executes, the Agent reads this content and outputs `Hello Heartbeat!`. Results are relayed to the frontend via the `heartbeat.relay` event.
![Execute heartbeat task](../assets/images/heartbeat6.png)

---

## FAQ

**Q: I changed the heartbeat section in `config/config.yaml` but nothing happened.**  
A: Config is read at startup. If you use the web UI Heartbeat panel, it rewrites YAML and automatically restarts the heartbeat service. If you edit YAML directly, restart the entire application (e.g., `jiuwenswarm-web`) for changes to take effect.

**Q: How do I send heartbeats only during work hours?**  
A: Set `heartbeat.active_hours.start` / `end`, e.g., `start: 09:00`, `end: 18:00`. Heartbeats only fire within this window.

**Q: What if a heartbeat request times out?**  
A: Set the `HEARTBEAT_TIMEOUT` environment variable (seconds). On timeout, the beat is marked failed and a WARNING is logged.

**Q: Where must `HEARTBEAT.md` live?**  
A: At the DeepAgent workspace root: `~/.jiuwenswarm/agent/jiuwenclaw_workspace/HEARTBEAT.md`. Otherwise, it is treated as "no custom tasks" and only `HEARTBEAT_OK` is returned.

---

## Mechanism Introduction and Key Code

### Agent Behavior Under Heartbeat

The server reads `HEARTBEAT.md`, parses the task list, and sends a chat request to the agent following the normal conversation flow. If parsing fails or the task list is empty, `HEARTBEAT_OK` is returned directly. Otherwise, tasks are executed and responses returned. Heartbeat results are pushed to the frontend via the `heartbeat.relay` event for status updates and history logging; if content is not `HEARTBEAT_OK`, a popup notification is displayed.

### Code and Config Index

| Code Path | Function |
|-----------|----------|
| `jiuwenswarm/gateway/heartbeat/heartbeat.py` | Heartbeat service implementation, including periodic scheduling and E2A request construction |
| `jiuwenswarm/common/config.py` | Config reading and writing, `update_heartbeat_in_config` function |
| `jiuwenswarm/app.py` | Builds `HeartbeatConfig` from config file and environment variables at startup |
| `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py` | Agent-side HEARTBEAT.md handling, detects heartbeat sessions and triggers tasks |
| `jiuwenswarm/channels/web/frontend/src/components/HeartbeatPanel/` | Frontend heartbeat panel components |
| `heartbeat.get_conf` / `heartbeat.set_conf` | Frontend config read/write API |
| `heartbeat.relay` | Heartbeat response relay event |