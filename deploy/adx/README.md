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

In the AgentOS runtime profile, a deployment may inject `JIUWENSWARM_ADX_NODE_CONTEXT_FILE` pointing to a read-only JSON file with `ingress_url` and `node_id`. Runtime reads the target node's file and passes `endpoint_file` to Core. Core refreshes the SDK connection scope before obtaining a tool environment, including when nodes share the same bridge address. Missing or invalid configured files fail closed. Existing launcher-injected `JIUWENSWARM_ADX_NODE_INGRESS_URL` remains supported when no context file is configured.

Deployment owns routing, protocol and runtime. HTTP/WS is explicitly supported for trusted loopback/bridge endpoints; HTTPS/WSS continues to verify TLS. Historical user URL, token and connection transport fields cannot redirect or downgrade a managed endpoint. Personal/non-ADX profiles keep their existing merge behavior.

Repeated configuration preserves models, IAM, sessions, supported ADX options,
SSH known-hosts settings and connection timeout. Portable legacy CPU/memory,
workdir and absolute local mounts are retained; identity is checked against the selected runtime. A legacy mount's
`source/target/readonly` becomes ADX `host_path/target/options`; omitted target
means source and omitted readonly means writable, matching the previous mapping.
`rootfs.workdir/mounts/user` are also supported. Identical platform workspace
mounts are deduplicated, while conflicting mounts are rejected.

Legacy Docker image/executor and Jiuwenbox template selectors are replaced by
the deployment's registered ADX image/runtime, with a warning containing field
names only. A custom image must be supported by the selected ADX backend and
configured explicitly with `type: adx-environment`. Nonlocal legacy mount
sources, other rootfs fields, file policies and excluded commands require explicit
migration; configuration reports an error instead of dropping these settings.
The deploy command validates existing user configurations. AgentOS can set `update_user_configs: false` to retain user YAML byte-for-byte and apply the deployment overlay in memory. User dotenv cleanup removes deployment API credentials while retaining model and business values. Workspace content, model settings and session databases are retained.

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
YAML, then removes the duplicates and retired YuanRong installer variables. When user YAML is retained, its legacy resource environment overrides remain until an explicit migration.
Invalid resource values fail before writing existing files. Management-plane
process overrides retain their existing priority and dynamic update behavior.

Gateway and the tool SDK inside AgentServer share `ADX_TOKEN`; tools do not inherit it; the generated
`gateway.adx.token_env` selects it. The identical `ADX_GATEWAY_TOKEN` duplicate is
removed. Model credentials and unknown application variables are retained.
Gateway/Web listen addresses and ports come from deployment settings and are
also written to application dotenv for CLI, Web and upgrade readers. AgentOS
launcher values take precedence when present. Legacy input cleanup is scoped to
the managed AgentOS profile; standalone retains existing inputs and token aliases. Builtin Agent resource
environment variables and CA settings remain active inputs. Node ingress is
supplied by the selected node context or legacy runtime launcher.

ADX's `deployment.env` expands native deployment YAML into component JSON; some
Ingress settings are also consumed directly from its process environment. These
bindings and generated outputs are separate stages of the same configuration.

### Runsc AgentServer and tools

AgentOS selects AgentServer and tool runtimes independently in deployment settings. For `agent_runtime: runsc`, the deploy helper generates `gateway.adx.builtin_launch` with an explicit Python command, read-only Python/library/CA mounts, a target-node context file, and network settings. Only the builtin AgentServer receives the Gateway's API credential as `JIUWENSWARM_ADX_API_TOKEN`; its entrypoint consumes that key into the SDK's `ADX_TOKEN` before loading user dotenv. ADX's reserved environment prefix remains protected. Tool SDK calls authenticate inside AgentServer; tools do not inherit that credential or its context-file mount. Host Python and mount layouts must match across eligible nodes.

AgentServer egress selects model origins from the user's model configuration and dotenv fallback; it does not copy API keys into policies. Private model origins need explicit deployment permission. Tools default to deny traffic and DNS, with optional `network.tool_origins`. Node policy is a mandatory permission ceiling. Runsc identity is defined by the deployed OCI spec: incompatible historical user/group or Conch-specific settings require migration, while legacy network denials are retained. Portable resource, mount and directory settings remain supported. Network isolation does not provide per-user authorization with a shared deployment token.

The generated AgentServer traffic policy can select `network.ingress_policy_peer`; Enterprise maps that peer to each node bridge in the optional ADX baseline. Restored requests retain the logical peer while the endpoint file supplies the actual target-node URL. Model policy refresh sends the lifecycle `X-ADX-Request-ID` separately from correlation headers. An unconfigured model grants no model network access. Workspace model resolution does not inherit model endpoints from the Gateway process. The managed `agentos-tools:v1` Conch binding follows the deployment image when selecting runsc; other explicitly configured OCI images remain unchanged.

For runsc AgentServers, ADX owns the Environment's idle deletion and checkpoint retention. Gateway copies its configured Agent idle timeout into `idleTimeoutSeconds` when creating the Environment. Later timeout changes apply to new Environments. Gateway's idle/disconnect cleanup releases local routing state and closes Relay activity; it does not delete the native Environment. A business transport error also retains Running, Pausing, Paused, Resuming and retrying executions. Confirmed terminal failures can use the existing cleanup path. Explicit user deletion still retires the Environment and its checkpoints. The tool idle timeout remains independent.

Managed runsc AgentServers retire saved Gateway transport connections after a
node/context, guest address, or native ADX runtime ID changes on restore.
The runtime ID is read from the existing runsc `/proc/gvisor/spec_environ`
interface, so restoring on the same node and IP also retires old transports.
The new push connection is published first; only the old connection-owned request
streams are cancelled. Resident Team producers and history remain available, and
the Web client reattaches its Team observer. Without a managed node-context file,
Front keeps its existing multiple-connection behavior.

Gateway replacements use generation-specific native Environment names and verify the generation through `env_vars` before publishing a replacement. An uncertain DELETE keeps the old instance fenced until a native lookup confirms absence. On Gateway restart, runsc routing checks matching registry rows against native state and can reuse Running/Paused environments, including earlier logical IDs; a missing native environment permits creation. Registry metadata alone never establishes native liveness.
