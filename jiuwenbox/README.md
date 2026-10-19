# jiuwenbox

`jiuwenbox` is a lightweight Linux sandbox service for running agent tools and
code snippets with layered isolation.

It exposes a FastAPI server for sandbox lifecycle management, file transfer,
file listing/search, and command execution. The server applies the configured
isolation policy in-process by spawning `bubblewrap` directly for each sandbox
(long-lived per-sandbox daemon plus on-demand background commands).

## Features

- Process isolation with `bubblewrap`
- Static policy-based filesystem access rules
- Server-managed sandbox backing storage (`~/.jiuwenbox/workspace`)
- Optional network isolation with Linux network namespaces and firewall rules
- Namespace and Linux capability controls
- Landlock filesystem enforcement when supported by the kernel
- Seccomp syscall filtering
- Python and JavaScript execution support when the corresponding runtimes exist
- Audit logging and persisted sandbox lifecycle state
- Inference Privacy Proxy for LLM API request routing with automatic API key injection

## Architecture

- `server`
  - FastAPI app that manages sandbox lifecycle, policy loading, audit logs, and
    API routing.
- `server/runtime`
  - In-process runtime adapter that translates each sandbox policy into a
    `bubblewrap` command line and spawns it directly from the server (one
    long-lived daemon per sandbox plus per-call background commands).
- `server/proxy_manager`
  - Manages inference privacy proxies for LLM API routing with API key injection.
- `server/policy_reader`
  - Shared policy file reader for sandbox and proxy managers.
- `supervisor`
  - Policy-to-isolation translation helpers (`bubblewrap` argv builder,
    Landlock payload, seccomp filter, cgroup/network setup) consumed by the
    runtime adapter.
- `proxy`
  - HTTP-aware inference privacy proxy with path-based routing and API key
    injection (supports OpenAI and Anthropic formats).
- `models`
  - Pydantic models for policies, sandboxes, API responses, and common status
    structures.

## Requirements

- Linux
- Python 3.11+
- `bubblewrap`
- `iproute2`, `iptables`, and `nftables` when `network.mode: isolated` is used
- `NET_ADMIN` capability and `net.ipv4.ip_forward=1` on the host when isolated
  mode uses uplink egress
- Kernel support for Landlock and seccomp when those features are enabled
- `nodejs` if JavaScript execution is needed

On Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y bubblewrap iproute2 iptables nftables python3-pip python3-venv nodejs
```

## Install From Source

```bash
cd jiuwenswarm/jiuwenbox
uv venv
source .venv/bin/activate
uv sync
uv pip install --upgrade pip build
python3 -m build --wheel
uv pip install ./dist/jiuwenbox*.whl
```

The wheel ships `jiuwenbox/configs/*.yaml` (sources under `src/jiuwenbox/configs/`).
When `JIUWENBOX_POLICY_PATH` is unset, the server uses the bundled
`default-policy.yaml`.

## Start The Server

### Local Start

After installing the wheel, start with the bundled default policy:

```bash
sudo ./.venv/bin/jiuwenbox-server
# or
sudo ./.venv/bin/python -m uvicorn jiuwenbox.server.app:app --host 0.0.0.0 --port 8321 --log-level debug
```

To use a different policy or port, set `JIUWENBOX_POLICY_PATH` to an **absolute**
path (or, from a dev tree, `src/jiuwenbox/configs/<name>.yaml`):

```bash
sudo env \
  JIUWENBOX_POLICY_PATH="/absolute/path/to/policy.yaml" \
  ./.venv/bin/python -m uvicorn jiuwenbox.server.app:app --host 0.0.0.0 --port 9000 --log-level debug
```

### Docker Start

Build the image:

```bash
cd jiuwenswarm/jiuwenbox/scripts
sudo ./build_docker.sh
```

Run with the default policy:

```bash
sudo ./run_docker.sh
```

### Unix Domain Socket Deployment

The management HTTP API can be served over a Unix Domain Socket instead of
TCP (one-of-two per process). HTTP/1.1 framing, routes and payloads stay
identical; only the transport changes. UDS is useful for same-host agents,
filesystem-permission-based access control, and avoiding loopback port
conflicts.

Listen address is controlled by a single env var:

```bash
JIUWENBOX_LISTEN=http://0.0.0.0:8321               # default
JIUWENBOX_LISTEN=unix:///run/jiuwenbox/jiuwenbox.sock  # switch to UDS (absolute path required)
```

Start locally on UDS (same two rules as the ⚠️ in *Local Start*:
`sudo env` for envs, absolute `./.venv/bin/` paths for binaries):

```bash
sudo env \
  JIUWENBOX_LISTEN=unix:///run/jiuwenbox/jiuwenbox.sock \
  ./.venv/bin/python -m jiuwenbox.server.launcher

# or, with the entry script installed by uv sync / pip install:
sudo env JIUWENBOX_LISTEN=unix:///run/jiuwenbox/jiuwenbox.sock \
  ./.venv/bin/jiuwenbox-server
```

Docker deployment on UDS:

```bash
mkdir -p /tmp/jiuwenbox-sock

sudo env \
  JIUWENBOX_LISTEN=unix:///run/jiuwenbox/jiuwenbox.sock \
  JIUWENBOX_UDS_HOST_DIR=/tmp/jiuwenbox-sock \
  ./run_docker.sh src/jiuwenbox/configs/default-policy.yaml
```

`run_docker.sh` skips the management-API TCP port mapping under UDS mode and
bind-mounts the host socket directory into the container; **the proxy port
`${JIUWENBOX_PROXY_PORT:-8322}` is still mapped as TCP** because the
Inference Privacy Proxy is an independent TCP listener.

Reach the server:

```bash
curl --unix-socket /tmp/jiuwenbox-sock/jiuwenbox.sock http://localhost/health
jiuwenbox --base-url unix:///tmp/jiuwenbox-sock/jiuwenbox.sock health
JIUWENBOX_URL=unix:///tmp/jiuwenbox-sock/jiuwenbox.sock jiuwenbox sandbox ls

# pytest in dual transport mode (operator launches the matching server first)
pytest tests/integration --server-endpoint=http://127.0.0.1:8321
pytest tests/integration --server-endpoint=unix:///tmp/jiuwenbox-sock/jiuwenbox.sock
```

UDS-related env vars:

| Variable | Default | Notes |
| --- | --- | --- |
| `JIUWENBOX_LISTEN` | `http://0.0.0.0:8321` | Management API listen URI; accepts `http://host:port` or `unix:///abs/socket/path`. |
| `JIUWENBOX_UDS_MODE` | `0666` | UDS socket file permissions (octal string). The Docker default is permissive so a non-root host user can connect; for multi-tenant / hardened deployments set `0660` and pass `docker run --user $(id -u):$(id -g)`. |
| `JIUWENBOX_UDS_HOST_DIR` | `/tmp/jiuwenbox-sock` | Host directory bind-mounted by `run_docker.sh` to expose the socket. |
| `JIUWENBOX_UDS_CONTAINER_DIR` | `/run/jiuwenbox` | Container-side mount point; must match the directory in `JIUWENBOX_LISTEN`'s socket path. |

### API authentication (opt-in)

When `JIUWENBOX_API_TOKEN` is set, every HTTP endpoint (including `/health`, `/api/v1/*`, and `/mcp`) requires:

```http
Authorization: Bearer <token>
```

Missing or invalid tokens return `401` with `{"error":"unauthorized"}`.

```bash
export JIUWENBOX_API_TOKEN="$(openssl rand -hex 32)"
JIUWENBOX_API_TOKEN=$JIUWENBOX_API_TOKEN jiuwenbox-server

export JIUWENBOX_API_TOKEN=$JIUWENBOX_API_TOKEN
jiuwenbox health

curl -H "Authorization: Bearer $JIUWENBOX_API_TOKEN" http://127.0.0.1:8321/health
```

| Variable / flag | Notes |
| --- | --- |
| `JIUWENBOX_API_TOKEN` | Shared by server and CLI; unset = auth disabled |
| `jiuwenbox-server --api-token` | Launch flag; overrides env |
| `jiuwenbox --api-token` | CLI flag; overrides env |

### Persisting the audit log (`--save-logs DIR`)

**By default jiuwenbox writes no log files at all.** Audit events
surface only on the standard Python logger at ``DEBUG`` level, sandbox
daemon and background-exec stdout/stderr go straight to ``/dev/null``,
and ``/api/v1/sandboxes/{id}/logs`` returns the empty string. This
keeps a fresh install from creating files under ``$HOME`` and prevents
long-running servers from filling the disk silently.

Pass `--save-logs DIR` (or set `JIUWENBOX_SAVE_LOGS_DIR=DIR`) to opt
into per-sandbox **audit log** persistence. Files are **kept after the
sandbox is gone**, which is the shape you want for offline inspection,
log shipping, or postmortem debugging.

> Note: the historical per-sandbox `runtime.log` and `runtime.bg-N.log`
> files (raw daemon and background-exec stdout/stderr) were removed.
> The audit log already carries the truncated per-command stdout/stderr,
> which has proven to be enough for routine debugging while letting us
> drop a class of "two files for the same thing" foot-guns. If you do
> need the full raw byte stream, run the container with `docker run -it`
> so the bwrap output streams to your terminal.

Every operation produces **a single row** in the audit JSONL, emitted
after the call returns so the payload carries both intent (command,
path) and outcome (exit_code, stdout/stderr, error). Reading just the
audit file is enough to answer "did this command succeed?":

| `event_type` | Key fields |
| --- | --- |
| `exec_command` | `command`, `workdir`, `background?`, `ok`, `exit_code`, `stdout`, `stderr`, `duration_ms`, `error?` (stdout/stderr are tail-truncated to 4 KiB; overflow is annotated `[truncated, total N chars]`. Background exec records `started`/`pid` instead of `exit_code`/`stdout`/`stderr`.) |
| `file_transfer` | `direction` (upload/download), `sandbox_path`, `size`, `ok`, `duration_ms`, `path` (`ipc` vs `exec_fallback`), `error?` |

The filename layout is `{sandbox_id}-{ISO8601-basic-timestamp}.audit.log`.
The timestamp is captured the first time a given sandbox writes an
audit event and reused for the rest of that sandbox's lifetime:

```
<DIR>/
  └── 9284a4bf-870-20260515T112345.audit.log   # structured JSONL
```

The basic ISO 8601 layout (`%Y%m%dT%H%M%S`) sorts lexicographically
the same way it sorts chronologically; combined with the sandbox_id
prefix, `ls 9284a4bf-870-*` gives you every audit file for that
sandbox in boot order.

Local launch:

```bash
sudo ./.venv/bin/jiuwenbox-server --save-logs /var/log/jiuwenbox

# Equivalent via env:
sudo env \
  JIUWENBOX_SAVE_LOGS_DIR=/var/log/jiuwenbox \
  ./.venv/bin/jiuwenbox-server
```

Docker launch: pass `--save-logs DIR` (or set
`JIUWENBOX_SAVE_LOGS_HOST_DIR=DIR`) and `run_docker.sh` will bind-mount
`DIR` onto `JIUWENBOX_SAVE_LOGS_CONTAINER_DIR` (default
`/var/log/jiuwenbox`) and inject `JIUWENBOX_SAVE_LOGS_DIR=<container
path>` into the launcher — no `Dockerfile` change required. The CLI
flag and the env var are equivalent; the flag wins when both are
present:

```bash
# CLI flag (recommended)
sudo ./run_docker.sh --save-logs /tmp/jiuwenbox-logs

# Equivalent env-var form
sudo env JIUWENBOX_SAVE_LOGS_HOST_DIR=/tmp/jiuwenbox-logs ./run_docker.sh

ls /tmp/jiuwenbox-logs
# 9284a4bf-870-20260515T112345.audit.log
```

| Variable | Default | Notes |
| --- | --- | --- |
| `JIUWENBOX_SAVE_LOGS_DIR` | _unset_ | Target audit-log directory inside the container/process. Unset means **no log files at all** (the new default — see above). The launcher resolves `--save-logs` / the env to an absolute path before writing this back. |
| `JIUWENBOX_SAVE_LOGS_HOST_DIR` | _unset_ | `run_docker.sh` only: host-side directory (env-var form of `--save-logs DIR`). Empty disables persistence. When set, the script `mkdir -p`s it, bind-mounts it into the container, and exports `JIUWENBOX_SAVE_LOGS_DIR`. |
| `JIUWENBOX_SAVE_LOGS_CONTAINER_DIR` | `/var/log/jiuwenbox` | `run_docker.sh` mount point inside the container. Override only if something else already owns this path inside the image. |

## Policy Files

The server loads one static default policy at startup. Policy dynamic update is
not enabled.

### Field Reference

#### Top-level fields

| Field | Default | Notes |
| --- | --- | --- |
| `version` | `1` | Policy schema version. Only `1` is supported today. |
| `name` | `"default"` | Human-readable name shown by the policy API. |
| `environment` | `{}` | Environment variables injected into every process inside the sandbox. |

#### `filesystem_policy`

| Field | Notes |
| --- | --- |
| `directories` | Directories created by the server and bound into the sandbox for its lifecycle. Each entry may be a `"/path"` string or a `{ path, permissions }` object (`permissions` is octal, e.g. `"0755"`). |
| `files` | Empty files created by the server and bound into the sandbox. Same shape as `directories`, with optional `permissions`. |
| `read_only` | Sandbox-visible paths granted read-only access. These entries do not mount host paths by themselves; pair them with `bind_mounts` / `directories`. |
| `read_write` | Sandbox-visible paths granted read-write access. Use `directories` or `bind_mounts` to make the paths exist inside the sandbox. |
| `bind_mounts` | Explicit host-to-sandbox bind mounts with `host_path`, `sandbox_path`, and `mode` (`ro` / `rw`). `host_path` cannot be the literal `"*"`. |
| `bind_root_entries` | Bind every **first-level** child under `host_root` to `sandbox_path/{name}`. Supports `mode`, `include_hidden` (hidden entries excluded by default), and `exclude` (fnmatch globs). Useful for bulk-mounting immediate children of `/usr` and similar trees. |
| `device` | Device nodes exposed inside the sandbox with `bwrap --dev-bind`; each item has `host_path` and `sandbox_path`. |

#### `process`

| Field | Default | Notes |
| --- | --- | --- |
| `run_as_user` | `sandbox` | User name for sandbox processes. Falls back to nobody-style UIDs when unresolved. |
| `run_as_group` | `sandbox` | Group name for sandbox processes. Falls back to nobody-style GIDs when unresolved. |

#### `namespace`

Controls Linux namespaces created by `bubblewrap`. Each field is `true` (create new) or `false` (reuse current):

| Field | Default | Notes |
| --- | --- | --- |
| `user` | `true` | User namespace. |
| `pid` | `true` | PID namespace. |
| `ipc` | `true` | IPC namespace. |
| `cgroup` | `true` | Cgroup namespace. |
| `uts` | `true` | UTS (hostname) namespace. |

#### `capabilities`

| Field | Default | Notes |
| --- | --- | --- |
| `add` | `[]` | Extra capabilities to grant, e.g. `["CAP_NET_RAW"]` or `["NET_RAW"]`. |
| `drop` | `[]` | Capabilities to drop. `"ALL"` drops every capability when bubblewrap supports it. |

#### `landlock`

| Field | Default | Notes |
| --- | --- | --- |
| `compatibility` | `best_effort` | `disabled`: do not apply Landlock; `best_effort`: apply when supported, otherwise continue; `hard_requirement`: fail sandbox startup if Landlock is unavailable. |

#### `syscall`

Per-architecture seccomp syscall block lists:

| Field | Notes |
| --- | --- |
| `x86_64.blocked` | Syscall names blocked on x86_64, e.g. `ptrace`, `mount`, `bpf`. Empty means no extra blocking. |
| `arm64.blocked` | Syscall names blocked on arm64/aarch64. |

#### `network`

Outbound (`egress`) and inbound (`ingress`) traffic rules. **Only enforced when `mode` is `isolated`**; in `host` mode these rules are not installed inside the sandbox.

**`mode`**

| Mode | Behavior |
| --- | --- |
| `isolated` (default) | Creates a dedicated network namespace per sandbox (`jbx-{sandbox_id}`), connects it to the host default route via a veth uplink, and installs `egress` / `ingress` iptables rules inside the sandbox netns. |
| `host` | Sandbox processes share the host network namespace. **No** egress/ingress firewall rules are installed inside the sandbox, so `blocked_ips`, `blocked_domains`, and similar fields have no effect. In host mode, only the jiuwenbox management port (default 8321) is protected via `uid-owner` iptables rules. |

For intranet deployments that need egress restrictions (for example `blocked_ips` to block RFC1918 addresses), use `isolated` mode with `uplink` configured. Do not rely on `host` mode for egress enforcement.

**`uplink`** (`isolated` mode only)

Each sandbox gets a veth pair (`jwbH{hash}` / `jwbS{hash}`), an address, a default route, and optional NAT through the host.

| Field | Default | Notes |
| --- | --- | --- |
| `subnet` | `""` (auto-select) | IPv4 CIDR pool for the uplink. When empty, jiuwenbox scans private pools (`100.64.0.0/10`, focused `10.200.x/16` ranges, `172.30.0.0/16`, `172.31.0.0/16`, `192.168.240.0/20`, then `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) and allocates a `/30` block that does not overlap any IPv4 route (`ip route show table all`). When set, the value is treated as an address pool and a free `/30` is chosen inside it. |
| `nat` | `true` | Whether to MASQUERADE uplink traffic on the host side. Usually required in intranet deployments so sandboxes can reach external destinations through the host default route. |
| `interface` | `""` (auto-detect) | Egress interface name. When empty, jiuwenbox auto-detects the interface used by the host default route. |

**`egress` / `ingress`**

| Field | Notes |
| --- | --- |
| `default` | `allow`: permit traffic unless it matches a `blocked_*` rule; `deny`: block traffic unless it matches an `allowed_*` rule. |
| `blocked_ips` / `allowed_ips` | CIDR-based IP rules applied to the sandbox netns OUTPUT / INPUT chains. `blocked_*` takes precedence over `allowed_*`. |
| `blocked_domains` / `allowed_domains` | Domain rules resolved via DNS and applied to the resolved IPs. |
| `blocked_ports` / `allowed_ports` | TCP port rules. |

iptables rules live in the sandbox netns (`jbx-{sandbox_id}`), not in the container or host default netns. When debugging egress blocks, inspect rules inside the corresponding netns:

```bash
ip netns list
ip netns exec jbx-<sandbox_id> iptables -L OUTPUT -n
```

Docker deployments using `isolated` + uplink need `net.ipv4.ip_forward=1` and
`NET_ADMIN`; `run_docker.sh` already passes both. Grant the same privileges if
you run `docker run` manually.

#### `cgroup`

Optional per-sandbox cgroup resource limits. All three fields default to `null`
(no limit). When every field is empty or the `cgroup` block is omitted,
jiuwenbox skips cgroup setup so the default policy still runs on hosts without
a writable cgroup tree.

| Field | Format | Notes |
| --- | --- | --- |
| `memory_max` | Integer bytes or a unit string (e.g. `"256M"`, `"1G"`) | Memory cap. |
| `cpu_max` | Fractional cores (e.g. `0.5`) or a `"quota_us period_us"` pair (e.g. `"50000 100000"`) | CPU quota. |
| `pids_max` | Positive integer | Process/thread cap. |

Cgroup v2 is preferred; v1 is used as a fallback. If at least one field is
non-null and neither backend is writable, sandbox creation fails.

#### `timeout`

jiuwenbox **server-side** idle sandbox reaping. **Only the root policy loaded
at server startup takes effect**; the same fields on per-sandbox policies do
not affect isolation and are returned for display only.

| Field | Default | Notes |
| --- | --- | --- |
| `idle_timeout` | `null` (disabled) | Maximum idle duration in seconds. `null` / `0` / negative disables reaping. Idle means time since the last exec / file IO / list-dir API call; `get_sandbox` / `list_sandboxes` / `get_logs` do not refresh the timer. |
| `idle_check_interval` | `60` | Reaper poll interval in seconds. Must be `> 0`. |

#### `inference_privacy_proxies`

Inference privacy proxy settings. `listen_port: 0` (default) disables the
proxy; when enabled, set both `listen_host` (IP address) and
`listen_port > 0`. `routes` defines per-`path_prefix` forwarding targets and
API key injection. See [Inference Privacy Proxy](#inference-privacy-proxy)
below.

If the policy contains only `version` / `name` /
`inference_privacy_proxies` and `listen_port > 0`, jiuwenbox enters
proxy-only mode (skips the sandbox subsystem). See
[`src/jiuwenbox/configs/inference-policy.yaml`](src/jiuwenbox/configs/inference-policy.yaml).

### Minimal example

```yaml
version: 1
name: "example"

filesystem_policy:
  directories:
    - path: "/tmp"
      permissions: "1777"
  read_only:
    - "/bin"
    - "/sbin"
    - "/usr"
    - "/lib"
    - "/lib64"
    - "/etc"
  read_write:
    - "/tmp"
  bind_mounts:
    - host_path: "/bin"
      sandbox_path: "/bin"
      mode: "ro"
    - host_path: "/sbin"
      sandbox_path: "/sbin"
      mode: "ro"
    - host_path: "/usr"
      sandbox_path: "/usr"
      mode: "ro"
    - host_path: "/lib"
      sandbox_path: "/lib"
      mode: "ro"
    - host_path: "/lib64"
      sandbox_path: "/lib64"
      mode: "ro"
    - host_path: "/etc/resolv.conf"
      sandbox_path: "/etc/resolv.conf"
      mode: "ro"
    - host_path: "/etc/hosts"
      sandbox_path: "/etc/hosts"
      mode: "ro"
    - host_path: "/etc/nsswitch.conf"
      sandbox_path: "/etc/nsswitch.conf"
      mode: "ro"
    - host_path: "/etc/host.conf"
      sandbox_path: "/etc/host.conf"
      mode: "ro"
    - host_path: "/etc/ssl/certs"
      sandbox_path: "/etc/ssl/certs"
      mode: "ro"
    - host_path: "/etc/ssl/openssl.cnf"
      sandbox_path: "/etc/ssl/openssl.cnf"
      mode: "ro"
  device:
    - host_path: "/dev/null"
      sandbox_path: "/dev/null"

process:
  run_as_user: sandbox
  run_as_group: sandbox

namespace:
  user: true
  pid: true
  ipc: true
  cgroup: true
  uts: true

capabilities:
  add: []
  drop: []

landlock:
  compatibility: best_effort

syscall:
  x86_64:
    blocked:
      - "ptrace"
      - "mount"
      - "umount2"
      - "reboot"
      - "kexec_load"
  arm64:
    blocked:
      - "ptrace"
      - "mount"
      - "umount2"
      - "reboot"
      - "kexec_load"

network:
  mode: isolated
  uplink:
    nat: true
    interface: ""
  egress:
    default: allow
    allowed_domains: []
    blocked_domains: []
    allowed_ips:
      - "127.0.0.1/32"
      - "::1/128"
    blocked_ips: []
    allowed_ports:
      - 443
      - 80
    blocked_ports:
      - 22
  ingress:
    default: deny
    allowed_domains: []
    blocked_domains: []
    allowed_ips:
      - "127.0.0.1/32"
      - "::1/128"
    blocked_ips: []
    allowed_ports: []
    blocked_ports:
      - 22
```

## Enabling jiuwenbox from jiuwenswarm's config file

jiuwenswarm decides **whether the sandbox is on, which jiuwenbox to talk to, whether to spawn its own jiuwenbox subprocess, and which policy file to use** via the `sandbox` section of its `config.yaml`. The TUI's `/sandbox` command writes back to the same section, so you can also pre-populate it by hand.

### Configuration schema

```yaml
sandbox:
  # -- Endpoint & type --
  url: "http://127.0.0.1:8321"      # jiuwenbox HTTP endpoint; TCP uses http://, UDS uses unix:///abs/socket/path
  type: "jiuwenbox"                 # sandbox provider name; currently only "jiuwenbox"

  # -- Startup & policy --
  startup_mode: "internal"          # internal = agent-server spawns jiuwenbox-server; external = you start it yourself
  policy_file: "code-agent-policy.yaml"   # bare name -> jiuwenbox/configs/<name>; otherwise an absolute / explicit path
  preserve_file_sharing_mode: "mount"     # only `mount` is supported; any other value is rejected

  # -- Runtime (also managed by the /sandbox TUI command) --
  enabled: true                     # whether sandbox mode is on
  excluded_commands:                # shell globs whose matches run locally instead of in the sandbox
    - "git *"
  files:                            # user-configured write policy; auto-managed paths are injected by the server, no need to repeat them here
    allow: []
    deny: []
```

Field reference:

| Field | Values | Default | Notes |
| --- | --- | --- | --- |
| `sandbox.url` | URL string | `http://127.0.0.1:8321` | jiuwenbox management API endpoint. TCP: `http://host:port`; UDS: `unix:///abs/socket/path` (mirrors `JIUWENBOX_LISTEN`). |
| `sandbox.type` | string | `jiuwenbox` | Sandbox provider name. Currently jiuwenswarm only wires up `jiuwenbox`. |
| `sandbox.startup_mode` | `internal` / `external` | `internal` | `internal`: agent-server spawns `jiuwenbox-server` at boot and persists the effective `url` (auto-picks a free port if the configured one is busy). `external`: agent-server never touches jiuwenbox; you must start it yourself per the top of this README. |
| `sandbox.policy_file` | filename or path | `code-agent-policy.yaml` | Bare filename → resolved relative to `jiuwenbox/configs/`; otherwise expanded (`~`, `$VAR`) and used verbatim. **Only honored under `startup_mode=internal`**; in `external` mode the policy is chosen by whoever started jiuwenbox-server (via `JIUWENBOX_DEFAULT_POLICY_PATH`). |
| `sandbox.preserve_file_sharing_mode` | `mount` | `mount` | Intrinsic files (`AGENT.md` etc.) and `project_dir` are bind-mounted, with `project_dir/config/config.yaml` auto-added to `deny_write`. Writing any other value is rejected. |
| `sandbox.enabled` | bool | `false` | When true, agent rebuilds route tools through the sandbox provider; toggled by `/sandbox enable`. |
| `sandbox.excluded_commands` | list[str] | `[]` | Shell globs matched per **simple-command leaf**. All matches → whole command on host; no matches → whole command in sandbox; mixed → local bash orchestrates control flow and wraps remote leaves with `jiuwenbox sandbox exec` (CLI required). Unsafe mixed forms run the whole original command in the sandbox. |
| `sandbox.files.allow` / `sandbox.files.deny` | list | `[]` | User-configured write policy. The effective set shown by `/sandbox status` is `auto_managed ∪ user_configured`; see [the `/sandbox` command reference](../docs/en/SlashCommands.md). |

### Two typical deployment shapes

#### Shape A: `startup_mode: internal` (agent-server spawns jiuwenbox for you)

Good for local development and single-host deployments. Drop this into `config.yaml`:

```yaml
sandbox:
  url: "http://127.0.0.1:8321"
  type: "jiuwenbox"
  startup_mode: "internal"
  policy_file: "code-agent-policy.yaml"   # picked up from jiuwenbox/configs/
  enabled: true
```

At boot the agent-server will:

1. Resolve `policy_file` to a host absolute path (bare name → `jiuwenbox/configs/<name>`; otherwise expand `~` / `$VAR` and use as-is).
2. Probe the port in `url`; if taken, switch to a free one and persist the new `url` back into `config.yaml`, so `/sandbox status` shows the real port.
3. Spawn `jiuwenbox-server` with the resolved policy path. On failure, agent-server logs the last 10 lines of stderr; you can retry from the TUI via `/sandbox enable`.

#### Shape B: `startup_mode: external` (you start jiuwenbox-server yourself)

Good when jiuwenbox lives on a different host / container, or when jiuwenswarm should never escalate to root. Example:

```yaml
sandbox:
  url: "http://10.0.0.5:8321"   # or unix:///run/jiuwenbox/jiuwenbox.sock
  type: "jiuwenbox"
  startup_mode: "external"
  enabled: true
```

Under this mode agent-server **does not** try to spawn jiuwenbox, and `sandbox.policy_file` has **no effect** (the policy is whatever you passed to `jiuwenbox-server` via `JIUWENBOX_DEFAULT_POLICY_PATH`). See [`Start The Server`](#start-the-server) and [`Unix Domain Socket Deployment`](#unix-domain-socket-deployment) above for how to start jiuwenbox-server in TCP or UDS mode.

For cross-host setups, the jiuwenbox host has to be able to reach jiuwenswarm's intrinsic agent files on the same host paths: `preserve_file_sharing_mode` is now fixed to `mount`, so jiuwenswarm bind-mounts the intrinsic files (`AGENT.md`, `HEARTBEAT.md`, `IDENTITY.md`, `SOUL.md`, `USER.md`, `memory/daily_memory/`) and `project_dir` into the sandbox. Make the relevant directories visible on the jiuwenbox machine (via shared filesystem, container volume, etc.) and confirm the policy allows writes into them (the bundled `jiuwenbox/configs/code-agent-policy.yaml` already does).


## Remote MCP

JiuwenBox supports three access modes: **REST API**, **CLI**, and **remote MCP**.

The MCP endpoint is `/mcp` (Streamable HTTP transport). The current MCP tool is
`sandbox_run_command`, which executes a command inside a JiuwenBox sandbox and
returns the result.

### Quick start

```bash
JIUWENBOX_POLICY_PATH=/path/to/default-policy.yaml \
python -m uvicorn jiuwenbox.server.app:app --host 0.0.0.0 --port 8321 --log-level debug
```

### OpenCode configuration

```json
{
  "mcpServers": {
    "jiuwenbox": {
      "url": "http://YOUR_HOST:8321/mcp",
      "type": "remote",
      "enabled": true
    }
  }
}
```

### External IP deployment

When JiuwenBox is deployed on an external IP (not `localhost` / `127.0.0.1`),
set `JIUWENBOX_MCP_ALLOWED_HOSTS` to allow the client host:

```bash
JIUWENBOX_MCP_ALLOWED_HOSTS=10.0.0.5,10.0.0.5:8321 \
JIUWENBOX_POLICY_PATH=/path/to/default-policy.yaml \
python -m uvicorn jiuwenbox.server.app:app --host 0.0.0.0 --port 8321 --log-level debug
```

### Notes

- A plain `GET /mcp` may return **406 Not Acceptable** because the endpoint
  expects MCP Streamable HTTP protocol frames. Use a proper MCP client to
  interact with it.
- MCP enables clients to call JiuwenBox, but does **not** mean all commands
  must go through a sandbox. Clients decide which commands to route via MCP.


## Inference Privacy Proxy

The inference privacy proxy enables secure LLM API access from edge servers:

- Path-based routing to different LLM providers (OpenAI, Anthropic, custom)
- Automatic API key injection (OpenAI `Authorization: Bearer`, Anthropic `X-Api-Key`)
- Hot-pluggable via REST API (create/start/stop/update/delete)
- Configured via policy YAML or dynamically through API

**Architecture**:

One global proxy process listens on a single host:port.

**Privacy routes default to `listen_port=0` (disabled)**. When enabled, both `listen_host` (IP address) and `listen_port` must be configured.

Routes are differentiated by `path_prefix` (forwarding rules). Each route has **independent state** (`running` = enabled forwarding; `stopped` = disabled).

**Creating routes via API requires valid `listen_host` and `listen_port > 0`**, otherwise returns an error.

### Proxy-only mode

When the policy YAML file **only configures `inference_privacy_proxies`** (top-level keys limited to `version` / `name` / `inference_privacy_proxies`, and `listen_port > 0`), jiuwenbox automatically enters proxy-only mode on startup:

- The sandbox subsystem is skipped entirely (no `ProcessRuntime`, no zombie reaper, no idle reaper).
- `GET /health` keeps working and reports `sandboxes_active = 0`.
- Sandbox-side routes (`/api/v1/sandboxes/*`, `/api/v1/policy/*`) return `503 Service Unavailable` until the policy file adds sandbox configuration.
- The proxy routes (`/api/v1/proxy/*`) and the inference proxy itself work normally.

The startup log emits `Proxy-only policy detected (no sandbox config); skipping sandbox subsystem startup`, followed by `Inference privacy proxy listening on http://<host>:<port>` so operators can verify the listener address at a glance.

See the reference config at [`src/jiuwenbox/configs/inference-policy.yaml`](src/jiuwenbox/configs/inference-policy.yaml) (installed as `jiuwenbox/configs/inference-policy.yaml`).

### Proxy Configuration

Policy YAML configuration schema:

```yaml
inference_privacy_proxies:
  listen_host: ipaddress, IP address to bind  # MUST
  listen_port: number, listen port             # MUST, non-zero enables proxy

  # OPTIONAL, can be managed via REST API after startup
  routes:
   - path_prefix: str, path name for forwarding rule
      target_endpoint: URL, target endpoint
      api_key: str, api key to inject when forwarding
      skip_cert_verify: boolean, skip cert verify for self-signed https targets, debug only
```

### API Key Injection

- OpenAI: Replace `Authorization: Bearer <placeholder>` with actual key
- Anthropic: Replace `X-Api-Key: <placeholder>` with actual key

### Configuration Example

`Note: The network endpoints https://api.openai.com and http://192.168.1.100:9000 are examples only`

#### Policy YAML Example

```yaml
inference_privacy_proxies:

  listen_host: "127.0.0.1"
  listen_port: 8080
  
  routes:
    - path_prefix: "openai"
      target_endpoint: "https://api.openai.com"
      api_key: "sk_sandbox_managed_openai_key"
   - path_prefix: "custom"
      target_endpoint: "http://192.168.1.100:9000"
      api_key: "sk_sandbox_managed_custom_key"
```

For edge servers, use `listen_host: "0.0.0.0"` to accept connections from all interfaces.

#### Forwarding Example

```text
Client request:  POST http://127.0.0.1:8322/openai/v1/chat/completions -H "Authorization: Bearer sk_fake_key"
Proxy forwards:  POST https://api.openai.com/v1/chat/completions       -H "Authorization: Bearer sk_sandbox_managed_openai_key"

Client request:  POST http://127.0.0.1:8322/custom/v1/chat/completions -H "Authorization: Bearer sk_fake_key"
Proxy forwards:  POST http://192.168.1.100:9000/v1/chat/completions    -H "Authorization: Bearer sk_sandbox_managed_custom_key"
```

#### jiuwenswarm Configuration Example

| Config    | Old Value                     | New Value                          |
| --------- | ----------------------------- | ---------------------------------- |
| api_base  | http://192.168.1.100:9000/v1/ | http://127.0.0.1:8322/custom/v1/   |
| api_key   | sk_sandbox_managed_custom_key | sk_fake_key                        |

## Run Integration Tests

`./tests/test.sh default` runs `test_server_api_default.py` and
`test_cli_default.py` together, exercising both the server HTTP API and the
jiuwenbox CLI. Use `--server-endpoint=URI` to switch between transports;
**the transport is inferred from the URI shape**, so there's no separate
flag to maintain in sync:

```bash
# TCP (default, equivalent to --server-endpoint=http://127.0.0.1:8321; the
# server should be launched with default-policy.yaml as its security policy)
./tests/test.sh default

# Custom TCP listener (a bare host:port gets http:// prepended automatically)
./tests/test.sh default --server-endpoint=http://127.0.0.1:18321
./tests/test.sh default --server-endpoint=127.0.0.1:18321

# UDS: pass the absolute socket path as a unix:// URL
./tests/test.sh default --server-endpoint=unix:///tmp/jiuwenbox.sock
./tests/test.sh default --server-endpoint=unix:///tmp/jiuwenbox-sock/jiuwenbox.sock
```

`test.sh` does **not** start the server; launch jiuwenbox first on the
selected transport (TCP with `JIUWENBOX_LISTEN=http://0.0.0.0:8321` or a
custom port, UDS with `JIUWENBOX_LISTEN=unix:///...`).

Run specific test cases:

```bash
python3 -m pytest tests/integration/test_server_api_default.py::TestPolicyEnforcement::test_network_mode_isolated_allows_external_http_requests -s --server-endpoint 127.0.0.1:8321
python3 -m pytest tests/integration/test_server_api_default.py::TestPolicyEnforcement::test_network_mode_isolated_blocked_ip_rejects_egress -s --server-endpoint 127.0.0.1:8321
```

### MCP Integration Tests

`test_mcp_default.py` exercises the `/mcp` Streamable HTTP endpoint and the
`sandbox_run_command` MCP tool. It is included automatically when running
`./tests/test.sh default`, or can be run standalone:

```bash
# TCP
python3 -m pytest tests/integration/test_mcp_default.py -v --server-endpoint 127.0.0.1:8321

# UDS
python3 -m pytest tests/integration/test_mcp_default.py -v --server-endpoint=unix:///tmp/jiuwenbox.sock
```

The MCP tests open a real MCP client session, run commands inside sandboxes,
and verify sandbox auto-creation/reuse/deletion, stdin/env/workdir forwarding,
command failure propagation, timeout clamping, and concurrent sessions.

### Performance Tests

Run the office-workload performance suite:

```bash
./tests/test.sh performance --server-endpoint 127.0.0.1:8321
```

Tune sandbox count, per-sandbox concurrency, and per-task loop count:

```bash
./tests/test.sh performance \
  --sandbox-count 2 \
  --concurrency 16 \
  --loop 8 \
  --server-endpoint 127.0.0.1:8321
```

The script maps these arguments to environment variables used by the performance
fixtures:

| Script argument | Environment variable | Default |
| --------------- | -------------------- | ------- |
| `--sandbox-count` | `JIUWENBOX_PERF_SANDBOX_COUNT` | `1` |
| `--concurrency` | `JIUWENBOX_PERF_CONCURRENCY` | `4` |
| `--loop` | `JIUWENBOX_PERF_LOOP` | `8` |

### Real LLM Integration Tests

To run real LLM integration tests, set the following environment variables. These tests are skipped by default if the environment variables are not set.:

```bash
export JIUWENBOX_TEST_LLM_ENDPOINT="https://api.openai.com"
export JIUWENBOX_TEST_LLM_API_KEY="sk_sandbox_managed_key"
export JIUWENBOX_TEST_LLM_MODEL="YOUR_MODEL"
```

## Notes

- Restart the server after changing the startup policy file.
- Existing sandboxes keep the policy that was written for them when they were
  created.
- Command stderr is returned as command output by the `/exec` API; server-side
  diagnostics should use debug logging when they would otherwise pollute
  command stderr.

## CLI

`jiuwenbox` ships a single-file Python CLI client wrapping the HTTP API
documented in [`docs/jiuwenbox_server_api.md`](docs/jiuwenbox_server_api.md).

After `pip install` it is exposed as the `jiuwenbox` executable; from source
you can also run it as `python -m jiuwenbox.cli.jiuwenbox`.

```bash
# Health
jiuwenbox health

# Sandbox lifecycle
ID=$(jiuwenbox sandbox create | jq -r .id)
jiuwenbox sandbox exec "$ID" -- python3 -c 'print("hi")'
JOB=$(jiuwenbox sandbox bg-exec "$ID" --job-id http-srv -- python3 -m http.server 18080 | jq -r .job_id)
jiuwenbox sandbox bg-get "$ID" "$JOB"
jiuwenbox sandbox bg-list "$ID"
jiuwenbox sandbox bg-kill "$ID" "$JOB"
jiuwenbox sandbox upload "$ID" ./data.csv /tmp/data.csv
jiuwenbox sandbox download "$ID" /tmp/result.json - | jq .
jiuwenbox sandbox ls
jiuwenbox sandbox rm "$ID" --yes

# Policy
jiuwenbox policy get "$ID"

# Proxies
jiuwenbox proxy create --prefix /openai --target https://api.openai.com --api-key sk-xxx
jiuwenbox proxy logs openai --lines 50
```

Global flags:

| Flag | Default | Env var | Description |
| --- | --- | --- | --- |
| `--base-url` | `http://127.0.0.1:8321` | `JIUWENBOX_URL` | Server endpoint. Accepts `http://host:port` or `unix:///abs/socket/path` |
| `--api-token` | _unset_ | `JIUWENBOX_API_TOKEN` | Bearer token; required when the server has auth enabled |
| `--timeout` | `30` | `JIUWENBOX_TIMEOUT` | HTTP timeout seconds |
| `--verbose / -v` | off | – | Debug logging on stderr |
| `--no-color` | off | `NO_COLOR` | Disable ANSI colors on stderr |

Exit codes: `0` success / sandbox exec returned 0, `1` HTTP 4xx/5xx, `2`
connection failure, `3` local argument or file error, or `sandbox bg-get` /
`sandbox bg-kill` when the job is missing (404), `130` Ctrl+C; the
`sandbox exec` subcommand transparently propagates the in-sandbox process
exit code; `sandbox bg-exec` returns `3` when `started=false`.

## License

Apache-2.0
