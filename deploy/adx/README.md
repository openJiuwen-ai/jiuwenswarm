# Swarm on an existing ADX cluster

This directory deploys the Swarm application, not the ADX cluster. It does not read
AgentOS topology, install runtime daemons, create credentials, or provision storage.

## Standalone

Prerequisites: Linux Bash 4+, root for service management, Python 3.11 with Swarm,
Agent Core and adx-environment dependencies installed; an existing authenticated
ADX HTTPS endpoint; available Supervisor/Conch image bindings. Prepare the same
workspace paths on all eligible runtime nodes, with permissions for the image user.
The Gateway validates existing `<workspace_root>/<user_id>` directories; it does not
provision shared storage. Mount targets in the builtin image use `/home/agentos`.

Export these values, or copy `settings.yaml` and replace its placeholders:

```sh
export SWARM_PYTHON=/path/to/venv/bin/python
export ADX_TENANT=my-tenant ADX_NAMESPACE=my-namespace
export ADX_INGRESS_URL=https://adx.example:8443
export ADX_NODE_INGRESS_URL=https://adx.example:8443
export ADX_TOKEN_FILE=/secure/adx-token ADX_CA_FILE=/secure/adx-ca.pem
export SWARM_WORKSPACE_ROOT=/srv/swarm/users
export SWARM_AGENT_IMAGE=swarm-agent:v1 SWARM_TOOL_IMAGE=swarm-tools:v1
export SWARM_SETTINGS=/path/to/swarm/deploy/adx/settings.yaml
bash deploy/adx/deploy.sh up
bash deploy/adx/deploy.sh down
```

The builtin image must start AgentServer on port 18092 and contain the Core provider
and SDK. It must have ADX credentials/CA and a reachable tool endpoint available to
its runtime. Node-local endpoint injection, when needed, is configured on the ADX
image binding by the cluster operator. The Gateway cannot guess the selected node.
Set `runtime.user` to the tool execution user present on the deployment host;
default is the current user. `runtime.cpu` / `memory` configure builtin Agent resources.
Tool sandboxes default to 2 CPU cores and 4096 MiB for both requests and limits.

Standalone is a trusted local mode: Gateway/Web bind loopback only, use the existing
local builtin catalog, and do not contact IAM or AgentRegistry. Protocol clients
provide `X-User-Id` matching a prepared workspace. This is not a public multi-tenant
login service; do not expose its ports through an unauthenticated proxy.
`gateway.instance_name` isolates service names and application data; use separate
ports for concurrent instances. Model credentials remain in the application's `.env`.

`install` optionally installs exactly one Core wheel and one Swarm wheel from
`SWARM_PACKAGE_DIR` with offline/no-deps pip. Dependency and SDK installation remains
a prerequisite. `configure` only prepares application configuration. Repeated `up`
preserves running services; use `down` then `up` to apply changed service settings.

## AgentOS composition

Enterprise supplies `SWARM_SETTINGS`, optional JSON-quoted `SWARM_BINDINGS`, and
`profile: agentos`, including explicit IAM/Registry and shared-workspace parameters.
Enterprise decides node/VIP placement before invoking `up`; `configure` can run on
all nodes. `SWARM_SERVICE_PREFLIGHT` optionally names an executable startup guard
owned by the caller. Swarm itself has no knowledge of cluster topology or VIPs.

Publish the deploy artifact with:

```sh
bash deploy/adx/package.sh /absolute/output/deploy.tar.gz
```

The archive contains `deploy/adx`. Enterprise extracts this directory into its
release's `deploy/jiuwenswarm` while retaining its own `module.sh` and `.env.custom`.
Old Swarm archives without `deploy/adx` fail packaging explicitly.

`down`, `status` and `uninstall` resolve only service identity (home, instance name
and static port). They do not load startup policy, CA, credentials or platform
connection settings. Optional bindings are consulted only if identity itself uses
an unresolved placeholder. A composer may explicitly provide `SWARM_HOME` and
`SWARM_INSTANCE_NAME` (empty for the default services), allowing cleanup even when
startup settings have not been prepared. An unresolved custom instance name fails
explicitly instead of stopping the default instance.

### Managed tool configuration and existing workspaces

The generated `sandbox` block contains only ADX tool settings. ADX manages tool
lifecycle, so `startup_mode` is unnecessary; old `internal`/`external` values are
accepted and ADX always uses external lifecycle management.

In the AgentOS runtime profile, tool ingress comes only from the selected node's
launcher-injected `JIUWENSWARM_ADX_NODE_INGRESS_URL`. Templates omit `sandbox.url`,
Gateway sandbox JSON omits the address, and application/user dotenv files do not
persist it. Runtime resolves the injected value into the SDK's effective URL;
missing injection is an error. Deployment owns the backend, enabled state and TLS
verification. Historical
user `url` and `connection.server_address/gateway_address/token/use_tls` values
cannot override that route. Credentials continue to come from the deployment's
ADX environment settings. Personal/non-ADX profiles keep their existing merge
behavior.

Repeated configuration preserves models, IAM, sessions, supported ADX options,
SSH known-hosts settings and connection timeout. Portable legacy CPU/memory,
UID/GID, workdir and absolute local mounts are retained. A legacy mount's
`source/target/readonly` becomes ADX `host_path/target/options`; omitted target
means source and omitted readonly means writable, matching the previous mapping.
`rootfs.workdir/mounts/user` are also supported. Identical platform workspace
mounts are deduplicated, while conflicting mounts are rejected.

Legacy Docker image/executor and Jiuwenbox template selectors are replaced by
the deployment's registered ADX image/runtime, with a warning containing field
names only. A custom Docker image must be registered as an ADX image binding and
configured explicitly with `type: adx-environment`. Nonlocal legacy mount
sources, other rootfs fields, file policies and excluded commands require explicit
migration; configuration reports an error instead of dropping these settings.
The deploy command updates existing user YAML and connection environment files;
workspace content, model settings and session databases are retained.

Unused `checkpoint.sqlite_journal_mode`, `workspace_sqlite.journal_mode` and
legacy SSH `user_template` are no longer generated. Team SQLite's `db_enable_wal`
and `mmap_size_mb` settings remain active and are retained for shared workspaces.
For AgentOS, node addresses are not persisted in application or user `.env` files;
legacy `persist_user_node_ingress_url` remains accepted for standalone deployments.

Management-plane `TOOL_SANDBOX_CPU/MEMORY/IDLE_TIMEOUT` overrides retain their
existing priority over user YAML. User resource settings are fallback values;
when a request is overridden, a lower retained limit is raised to that request.
A higher user limit is retained.

### YAML and dotenv ownership

Persisted ADX tool resources live in `config.yaml::sandbox`. Deployment first
migrates effective legacy `TOOL_SANDBOX_CPU/MEMORY/IDLE_TIMEOUT` dotenv values to
YAML, then removes the duplicates and retired YuanRong installer variables.
Invalid resource values fail before writing existing files. Management-plane
process overrides retain their existing priority and dynamic update behavior.

Gateway and tool SDK authentication share `ADX_TOKEN`; the generated
`gateway.adx.token_env` selects it. The identical `ADX_GATEWAY_TOKEN` duplicate is
removed. Model credentials and unknown application variables are retained.
Gateway/Web listen addresses and ports come from deployment settings and are
also written to application dotenv for CLI, Web and upgrade readers. AgentOS
launcher values take precedence when present. Legacy input cleanup is scoped to
the managed AgentOS profile; standalone retains existing inputs and token aliases. Builtin Agent resource
environment variables and CA settings remain active inputs. Node ingress is
supplied only by the runtime launcher.

ADX's `deployment.env` expands native deployment YAML into component JSON; some
Ingress settings are also consumed directly from its process environment. These
bindings and generated outputs are separate stages of the same configuration.
