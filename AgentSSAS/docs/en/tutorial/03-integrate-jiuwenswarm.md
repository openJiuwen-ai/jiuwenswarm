# Integrating JiuwenSwarm

> This chapter explains how to connect AgentSSAS to JiuwenSwarm: applying a patch to register AgentSSASSecurityRail, so that agent behavior events are automatically collected and run through threat detection. It is intended for users who need to enable security situational awareness in JiuwenSwarm.

## How the Integration Works

AgentSSAS integrates into JiuwenSwarm through a patch. After the patch is applied, `AgentSSASSecurityRail` is registered into the DeepAgent execution chain of JiuwenSwarm, automatically collecting agent behavior events and reporting them to the AgentSSASCore analysis engine.

The patch modifies 3 files in the JiuwenSwarm repository:

| File | Change |
| ---- | ------ |
| `jiuwenswarm/resources/config.yaml` | Adds the `ssas` configuration section (configuration template) |
| `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py` | Optionally imports AgentSSASSecurityRail, adds the `_build_ssas_rail` builder method, and registers the Rail |
| `pyproject.toml` | Declares the `agent-ssas` dependency |

## Prerequisites

- Python >= 3.11, with [uv](https://docs.astral.sh/uv/) installed
- The JiuwenSwarm source repository (develop branch) and the AgentSecurity repository

The **local-dependency patch** (`jiuwenswarm_ssas_local.patch`) requires the two repositories to be placed side by side:

```text
<workspace>/
├── jiuwenswarm/          # JiuwenSwarm repository (develop branch)
└── AgentSecurity/
    └── AgentSSAS/        # This repository
```

The **online-dependency patch** (`jiuwenswarm_ssas_online.patch`) pulls `agent-ssas` from atomgit, does not require the two repositories to be side by side, and can be applied to a jiuwenswarm repository at any path.

## Step 1: Apply the Patch (Choose One)

From the jiuwenswarm repository root, run:

```bash
# Option A: local-dependency version - references the local AgentSSAS source in editable mode,
# so source changes take effect immediately; suitable for development and debugging
git apply ../AgentSecurity/AgentSSAS/patches/jiuwenswarm_ssas_local.patch

# Option B: online-dependency version - pulls agent-ssas from atomgit; suitable for production use
git apply ../AgentSecurity/AgentSSAS/patches/jiuwenswarm_ssas_online.patch
```

Both patches make exactly the same changes to the 3 files; the only difference is the source of the `agent-ssas` dependency in `pyproject.toml`:

- The local-dependency version declares `agent-ssas = { path = "../AgentSecurity/AgentSSAS", editable = true }`
- The online-dependency version declares `agent-ssas @ git+https://atomgit.com/yieux1/AgentSecurity.git@develop#subdirectory=AgentSSAS`

## Step 2: Install Dependencies

From the jiuwenswarm repository root, run:

```bash
uv sync
```

## Step 3: Confirm the Configuration

The patch adds an `ssas` section to `resources/config.yaml` (the configuration template) of jiuwenswarm:

```yaml
ssas:
  enabled: true            # Master switch; when disabled, AgentSSASSecurityRail is not registered
  mode: inprocess          # inprocess (in-process) / http (standalone HTTP service)
  decision_policy: observe_only   # observe_only (awareness only) / active_protection (risk-based handling)
  # ...See docs/en/reference/configuration.md for the complete field list
```

- **New users**: when `jiuwenswarm-init` runs, the template is copied to `~/.jiuwenswarm/config/config.yaml`; no extra action is needed
- **Existing users**: `~/.jiuwenswarm/config/config.yaml` already exists (it is not overwritten by the template); manually add the `ssas` section to that file and set `enabled` to `true`

> Note: the user config.yaml does not include the `ssas` section by default. You must explicitly add it and set `enabled: true` to enable SSAS.

## Step 4: Start and Verify

```bash
jiuwenswarm-init      # Initialize on first use
jiuwenswarm-start     # Start; open http://localhost:5173 in a browser
```

The integration succeeded if the following line appears in the startup log:

```text
[JiuWenSwarmDeepAdapter] AgentSSASSecurityRail create success, mode=inprocess, policy=observe_only
```

Talk to an agent (any task that triggers tool calls), and AgentSSAS starts collecting events and running detection.

## Step 5: Check the Data

After running for a while, check the persisted data:

```text
~/.jiuwenswarm/ssas/
├── ssas_core.db                 # Core database: raw_events / events / alerts tables
├── reports/threat_log/*.json    # OCSF threat logs
└── modules/<module>/result.db   # Results of each detection module
```

```bash
# Check the number of collected events (requires sqlite3)
sqlite3 ~/.jiuwenswarm/ssas/ssas_core.db "SELECT COUNT(*) FROM events;"
```

For more ways to inspect the data, see [View Threat Logs](../how-to/view-threat-logs.md).

## Troubleshooting

**No AgentSSASSecurityRail-related output in the startup log**

- Check whether `~/.jiuwenswarm/config/config.yaml` contains the `ssas` section with `enabled: true`. The configuration file of an existing user is not overwritten by the template, so it must be added manually
- Check whether the dependencies were installed successfully. If the log contains `AgentSSASSecurityRail not loaded` or `agent-ssas not installed`, go back to Step 2 and run `uv sync` again

**The log contains `AgentSSASSecurityRail disabled by config`**

- This means `ssas.enabled` is not set to `true`; modify the configuration and restart

**Startup failure or connection refused in HTTP mode**

- If `mode: http`, make sure the standalone SSAS service is running, `http_endpoint` points to the correct address, and the port is not occupied; if port 8443 is occupied, switch to another port; see [HTTP Service Mode](./04-http-mode.md)

**Undoing the integration**

```bash
cd jiuwenswarm
git checkout -- jiuwenswarm/resources/config.yaml \
  jiuwenswarm/server/runtime/agent_adapter/interface_deep.py pyproject.toml
```

## Common Adjustments

| Need | Change |
| ---- | ------ |
| Turn off SSAS | `ssas.enabled: false` |
| Switch to HTTP mode | `ssas.mode: http` plus `ssas.http_endpoint` pointing to the standalone service (see [HTTP Service Mode](./04-http-mode.md)) |
| Enable active protection | `ssas.decision_policy: active_protection` (critical is blocked, the rest raise alerts) |
| Enable the test module | `ssas.modules.test_detection.enabled: true` |

## Related Resources

- [examples/jiuwenswarm_integration/README.md](../../../examples/jiuwenswarm_integration/README.md): the integration example that accompanies this tutorial
- [patches/](../../../patches/): the two patch files (`jiuwenswarm_ssas_local.patch` / `jiuwenswarm_ssas_online.patch`)
- [Configuration Reference](../reference/configuration.md): all fields of the `ssas` section
