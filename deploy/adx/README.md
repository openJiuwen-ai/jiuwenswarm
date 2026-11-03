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
