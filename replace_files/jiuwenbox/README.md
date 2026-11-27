# jiuwenbox

`jiuwenbox` is a lightweight Linux sandbox service for running agent tools and
code snippets with layered isolation.

It exposes a FastAPI server for sandbox lifecycle management, file transfer,
file listing/search, and command execution. Each sandbox command is launched
through a small supervisor process that applies the configured isolation policy.

## Features

- Process isolation with `bubblewrap`
- Static policy-based filesystem access rules
- Configurable sandbox backing workspace through `sandbox_workspace`
- Optional network isolation with Linux network namespaces and firewall rules
- Namespace and Linux capability controls
- Landlock filesystem enforcement when supported by the kernel
- Seccomp syscall filtering
- Python and JavaScript execution support when the corresponding runtimes exist
- Audit logging and persisted sandbox lifecycle state

## Architecture

- `server`
  - FastAPI app that manages sandbox lifecycle, policy loading, audit logs, and
    API routing.
- `server/runtime`
  - Runtime adapter that starts one supervisor process per sandbox command.
- `supervisor`
  - Per-command launcher that translates the effective policy into
    `bubblewrap`, Landlock, seccomp, and namespace settings.
- `models`
  - Pydantic models for policies, sandboxes, API responses, and common status
    structures.

## Layout

```text
configs/
  default-policy.yaml
  jiuwenclaw-policy.yaml
docker/
scripts/
  server.sh
  test.sh
src/jiuwenbox/
  models/
  server/
  supervisor/
tests/
  integration/
```

## Requirements

- Linux
- Python 3.11+
- `bubblewrap`
- `iproute2` and `iptables` when `network.mode: isolated` is used
- Kernel support for Landlock and seccomp when those features are enabled
- `nodejs` if JavaScript execution is needed

On Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y bubblewrap iproute2 iptables python3-pip nodejs
```

## Install

```bash
cd jiuwenclaw/jiuwenbox
uv venv
source .venv/bin/activate
uv sync
```

## Start The Server

### Local Start

`scripts/server.sh` accepts a policy file as the first positional argument.
The path is resolved relative to the caller's current directory and then passed
to the server as an absolute path.

```bash
sudo ./scripts/server.sh
sudo ./scripts/server.sh configs/default-policy.yaml
sudo ./scripts/server.sh configs/jiuwenclaw-policy.yaml 9000
```

The default port is `8321`. A numeric second positional argument overrides the
port. Additional arguments are forwarded to `uvicorn`.

The selected policy path is exported as:

```bash
JIUWENBOX_DEFAULT_POLICY_PATH=/absolute/path/to/policy.yaml
```

The port can also be set through:

```bash
JIUWENBOX_PORT=9000 sudo ./scripts/server.sh configs/default-policy.yaml
```

### Docker Start

Build the image:

```bash
cd jiuwenclaw/jiuwenbox/docker
sudo ./build_docker.sh
```

Run with the default policy:

```bash
sudo ./run_docker.sh
```

The Docker image starts `./scripts/server.sh` by default.

## Policy Files

The server loads one static default policy at startup. Policy dynamic update is
not enabled.

Important fields:

- `sandbox_workspace`
  - Host directory used for server-managed sandbox backing storage.
  - The value must be absolute after `~` and environment variables are expanded.
- `filesystem_policy.directories`
  - Directories created by the server and bound into each sandbox for its
    lifecycle.
- `filesystem_policy.read_only`
  - Sandbox-visible paths granted read-only access. These entries do not mount
    host paths by themselves.
- `filesystem_policy.read_write`
  - Sandbox-visible paths granted read-write access. Use `directories` or
    `bind_mounts` to make the paths exist inside the sandbox.
- `filesystem_policy.bind_mounts`
  - Explicit host-to-sandbox bind mounts.

Path fields support shell-style expansion such as `~` and environment
variables.

Minimal example:

```yaml
version: 1
name: "example"
sandbox_workspace: "/sandbox"

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
  blocked:
    - "ptrace"
    - "mount"
    - "umount2"
    - "reboot"
    - "kexec_load"

network:
  mode: isolated
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

## API Examples

Create a sandbox:

```bash
curl -sS -X POST http://127.0.0.1:8321/api/v1/sandboxes \
  -H 'Content-Type: application/json' \
  -d '{"command":["/usr/bin/python3","-c","import time; time.sleep(3600)"]}'
```

Run a command:

```bash
curl -sS -X POST http://127.0.0.1:8321/api/v1/sandboxes/<sandbox-id>/exec \
  -H 'Content-Type: application/json' \
  -d '{"command":["/usr/bin/python3","-c","print(\"hello\")"],"workdir":"/tmp"}'
```

Upload a file:

```bash
curl -sS -X POST \
  'http://127.0.0.1:8321/api/v1/sandboxes/<sandbox-id>/upload?sandbox_path=/tmp/input.txt' \
  -F 'file=@input.txt'
```

Download a file:

```bash
curl -sS \
  'http://127.0.0.1:8321/api/v1/sandboxes/<sandbox-id>/download?sandbox_path=/tmp/input.txt'
```

Delete a sandbox:

```bash
curl -sS -X DELETE http://127.0.0.1:8321/api/v1/sandboxes/<sandbox-id>
```

## Run Integration Tests

```bash
./scripts/test.sh
```

Run one policy-specific integration suite:

```bash
./scripts/test.sh default # jiuwenbox runs the service using default-policy.yaml as the security policy.
./scripts/test.sh jiuwenclaw-tool # jiuwenbox runs the service using jiuwenclaw-tool-policy.yaml as the security policy.
```

## Notes

- Restart the server after changing the startup policy file.
- Existing sandboxes keep the policy that was written for them when they were
  created.
- Command stderr is returned as command output by the `/exec` API; server-side
  diagnostics should use debug logging when they would otherwise pollute
  command stderr.

## License

Apache-2.0
