# Multi-Instance Operation

Run multiple independent JiuwenSwarm instances on the same machine, each with isolated workspace, configuration, and ports.

## Use Cases

- **Dev/Prod isolation**: Run test and production instances simultaneously on a development machine
- **Multi-tenant environment**: Create independent Agent instances for different users or projects
- **Parallel modes**: Run Agents with different configurations (models, permission policies) concurrently

> **Not the same as multi-window TUI**: This page covers **separate backend instances** (different workspaces and ports). To open several TUI terminals against one Gateway, see [TUI Usage Guide (zh) — Multi-window TUI](../zh/TUI使用指南.md#多窗口-tui); you do not need extra instances for that.

---

## Core Concepts

### Instance Isolation

Each instance has independent:

| Resource | Description |
|----------|-------------|
| **Workspace directory** | `workspace/` containing SOUL.md, skills, memory, etc. |
| **Configuration file** | `.env` file with API_KEY, model settings, etc. |
| **Ports** | Separate ports for AgentServer, Gateway, WebChannel, Frontend |
| **Process** | Independent process group managed via PID file |
| **Startup lock** | Prevents concurrent starts of the same instance |

### Instance Naming Rules

- Length: 1-64 characters
- Allowed characters: letters, digits, underscore, hyphen
- Cannot start with `.`
- Reserved names: `default`, `config`, `tmp`, `jiuwenswarm`, `all`

---

## Configuration File

### instances.yaml

Location: `~/.jiuwenswarm/instances.yaml` (or repository root)

```yaml
instances:
  dev:
    workspace: ~/.jiuwenswarm_dev
    ports:
      agent_server: 19092
      web: 20000
      gateway: 20001
      frontend: 6173
  prod:
    workspace: ~/.jiuwenswarm_prod
    ports:
      agent_server: 20092
      web: 21000
      gateway: 21001
      frontend: 7173
```

### Port Auto-Allocation Algorithm

Default port = base port + instance index × 1000

| Service Type | Base Port | Default Instance (index=0) | First Named Instance (index=1) |
|--------------|-----------|---------------------------|-------------------------------|
| agent_server | 18092 | 18092 | 19092 |
| web | 19000 | 19000 | 20000 |
| gateway | 19001 | 19001 | 20001 |
| frontend | 5173 | 5173 | 6173 |

---

## Commands

### jiuwenswarm-init --name

Create a named instance:

```bash
# Create dev instance
jiuwenswarm-init --name dev

# Create prod instance with specified workspace
jiuwenswarm-init --name prod --workspace ~/.jiuwenswarm_prod
```

This will:
1. Create workspace directory
2. Generate instance-specific `.env` file
3. Update `instances.yaml` configuration
4. Allocate ports (auto or manual)

### jiuwenswarm-start Management Commands

```bash
# List all instance statuses
jiuwenswarm-start --list

# Output example:
# INSTANCE     STATUS     PID     WORKSPACE                               PORTS
# --------------------------------------------------------------------------------
# default      running    12345   ~/.jiuwenswarm                           as:18092,w:19000,g:19001,f:5173
# dev          stopped    -       ~/.jiuwenswarm_dev                       as:19092,w:20000,g:20001,f:6173
# prod         stopped    -       ~/.jiuwenswarm_prod                      as:20092,w:21000,g:21001,f:7173

# Show specific instance details
jiuwenswarm-start --status dev

# Start named instance
jiuwenswarm-start --name dev
jiuwenswarm-start --name dev app    # Start backend only
jiuwenswarm-start --name dev web    # Start web service only

# Stop instance
jiuwenswarm-start --stop dev

# Restart instance
jiuwenswarm-start --restart dev
jiuwenswarm-start --restart dev --mode app
```

---

## Startup Lock Mechanism

To prevent concurrent starts of the same instance, the system uses file locks:

- Lock file: `<workspace>/.instance.lock`
- Lock timeout: 30 seconds (`STALE_LOCK_TIMEOUT`)
- Cross-platform support:
  - Unix: `fcntl.flock`
  - Windows: exclusive file creation

If lock conflict occurs during startup:

```
[start_services] ERROR: Instance 'dev' startup in progress by another process
[start_services] Wait a few seconds or check if another terminal is starting this instance.
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `JIUWENSWARM_DATA_DIR` | Override data root directory (affects instances.yaml location) |
| `JIUWENSWARM_CONFIG_DIR` | Override configuration directory |

Use `--dotenv` at startup to specify instance-specific config:

```bash
# Internal mechanism: bootstrap .env is auto-loaded when starting named instance
jiuwenswarm-start --name dev
# Equivalent to loading ~/.jiuwenswarm_dev/.env
```

---

## PID File

Each instance maintains a PID file in the workspace directory:

- Filename: `.instance.pid`
- Content: Process ID + startup timestamp
- Purpose:
  - Status query (`--status`, `--list`)
  - Process control (`--stop`, `--restart`)

---

## Best Practices

### 1. Port Planning

Avoid port conflicts:

```bash
# Check port usage
jiuwenswarm-start --status dev
# System auto-detects and reports conflicts
```

### 2. Configuration Isolation

Use separate API keys or model configs for each instance:

```bash
# dev instance .env
API_KEY="sk-dev-key"
MODEL_NAME="deepseek-chat"

# prod instance .env
API_KEY="sk-prod-key"
MODEL_NAME="gpt-4"
```

### 3. Workspace Management

Instance workspace contains all Agent runtime data:

```
~/.jiuwenswarm_dev/
├── .env                # Instance config
├── .instance.pid       # Process management
├── .instance.lock      # Startup lock
├── workspace/
│   ├── agent/
│   │   ├── SOUL.md     # Agent personality
│   │   ├── memory/     # Memory storage
│   │   └── skills/     # Skills directory
│   └── session/        # Session data
```

---

## FAQ

### Q: How to delete an instance?

Manually remove workspace directory and instances.yaml entry:

```bash
rm -rf ~/.jiuwenswarm_dev
# Edit ~/.jiuwenswarm/instances.yaml to remove corresponding entry
```

### Q: Difference between default and named instances?

- **Default instance**: Started without `--name`, uses `~/.jiuwenswarm/` workspace
- **Named instance**: Specified via `--name`, uses independent workspace and ports

### Q: Can instances share configuration?

Yes. Reference shared config in instance `.env`:

```bash
# ~/.jiuwenswarm_dev/.env
source ~/.jiuwenswarm/.env  # Shared base config
MODEL_NAME="special-model" # Instance-specific override
```