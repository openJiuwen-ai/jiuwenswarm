# Agent Profile Modes (normal / flash) — Local Startup Configuration

The JiuWenSwarm sidecar supports two agent profile modes. A single sidecar process can serve both modes per session without restart:

- **normal**: full rail set, `enable_task_loop=true` (default behavior, no filtering).
- **flash**: trimmed rail set (a small whitelist of core rails), `enable_task_loop=false`, `evolution.skill_create/review_trigger=false`. Suited to lightweight, low-overhead single-round interaction.

Kind resolution priority (highest first; first match wins):

1. Request signal `params.agent_kind` / `metadata.agent_kind` (forwarded by relay; stock relay does not send this).
2. `agent_profiles.breed_map[agent_id]` (mapped per agent_id).
3. `agent_profiles.default` (the **config-baked default**; fallback when the two above miss).
4. None of the above → no profile is activated; global `react` config is used.

> Item 3, `default`, is the key to running the sidecar in flash mode **with zero relay/frontend changes**. Since the stock relay forwards no `agent_kind`, `default` is the only path that works without modifying relay code locally.

---

## What you need to do to start in flash mode locally

### 1. Confirm the sidecar code has the `default` source

The `agent_profiles.default` read in `_resolve_agent_kind` (`jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`) must be present. This change lives on the `feat/per-session-agent-profiles` branch.

### 2. Edit the user-dir config.yaml

The user-dir config overrides `resources/config.yaml`. Edit the **user-dir** copy.

Example path (Windows, dolores deployment):

```
C:\Users\<you>\.office-claw\.jiuwenclaw-dolores\config\config.yaml
```

Add a `default: flash` line under the top-level `agent_profiles:` section:

```yaml
agent_profiles:
  default: flash          # ← add this line: default to flash mode
  normal:
    react:
      enable_task_loop: true
      evolution:
        skill_create: false
        review_trigger: false
  flash:
    react:
      enable_task_loop: false
      evolution:
        skill_create: false      # must match review_trigger, else force-revive fires
        review_trigger: false
    rails:
      keep:
        - _runtime_prompt_rail
        - _response_prompt_rail
        - _stream_event_rail
        - _task_planning_rail
        - _security_rail
        - _heartbeat_rail
        - _subagent_rail
        - _permission_rail
        - _context_processor_rail
        - _ask_user_rail
```

- `default` values: `flash` → default to trimmed mode; `normal` → default to full mode; omit or `null` → no profile activated (uses global `react`, equivalent to normal).
- `default` is the **lowest-priority fallback**: a session whose request carries `agent_kind`, or matches `breed_map`, overrides `default`. So with `default: flash` set, you can still switch individual sessions back to normal.

### 3. Confirm no env var forces an override

`evolution.skill_create` / `evolution.review_trigger` have process-level env overrides (higher priority than config_base). If `SKILL_CREATE=true` or `EVOLUTION_REVIEW_TRIGGER=true` is set, force-revive pulls `enable_task_loop` back to `true`, disabling flash. Before starting locally, confirm these envs are unset or false.

### 4. Restart the sidecar and verify

After restart, check the sidecar log (`service_default/.logs/agent_server.log`):

```
[JiuWenSwarmDeepAdapter] profile=flash dropped rails: ['_skill_credential_injection_rail', ... ]
```

A `profile=flash dropped rails: [...]` line confirms flash is active.

> **Proof**: if the prewarm session (which carries no request signal) prints `profile=flash` at startup, `default: flash` is taking effect purely from config, with no reliance on relay/frontend.

---

## ⚠️ Free-roaming rails: rails the keep/drop list cannot reach

The `rails.keep`/`drop` list **only governs the 31 `_RailBuildInfo` entries in `_build_agent_rails`**. Some rails are not in that table and register via "another path" that the profile's keep/drop list never touches; each must be turned off by its own gate:

| rail | registration path | gate switch | flash handling |
|---|---|---|---|
| SkillEvolutionRail | `_reconcile_evolution_rails` → `configure_skill_evolution_runtime` | `evolution.skill_evolution` (read by `get_evolution_enabled`) | `false` turns it off ✅ (configured) |
| ContextAssembleRail | `_update_agent_rails` direct `register_rail` | agent mode (no config switch) | **keep** — core rail; injects workspace dir/context/tools sections into the system prompt; disabling breaks the agent |
| SkillCreateRail | separate `_build_skill_create_rail` | `skill_create` + `enable_task_loop` | `skill_create:false` (configured) + `task_loop:false` double-gate |
| TaskCompletionRail | openjiuwen framework auto-inject | `enable_task_loop=true` | `task_loop:false` → not injected ✅ |
| ObservabilityRail / AgentTraceBindingRail | framework standing rail (unconditional) | none | not filterable (framework-level) |

**SkillEvolutionRail is the easiest trap**: it is gated by `react.evolution.skill_evolution`, **not** `skill_create`/`review_trigger`. Setting only the latter two to false still leaves SkillEvolutionRail registered with `signal_trigger=True` (the log prints `SkillEvolutionRail configured`). You must set `evolution.skill_evolution: false` explicitly to turn it off.

**Verify SkillEvolutionRail is off**: after restart the `SkillEvolutionRail configured` line should be gone (`grep -c` returns 0).

---

## ⚠️ Pitfall: new config keys must also exist in the template, or they get pruned on restart

At startup the sidecar runs config migration `migrate_config_from_template` (`jiuwenswarm/common/config.py`), a three-way merge where the **`resources/config.yaml` template is the structural authority: keys absent from the template are pruned as deprecated** (`_deep_merge` iterates only template keys; user-only keys are dropped; migration also `yaml.safe_dump`s the file, stripping comments).

Therefore:

- If `default` / `breed_map` is written in the **user dir only** with **no matching active key in the template**, the sidecar prunes it on first restart and flash silently stops working.
- Fix: a new key must also be added to the `resources/config.yaml` template as an **active key** (not just a comment). Put a neutral default in the template (e.g. `default: null`, `breed_map: {}`) and the real value in the user dir. The user value wins during the merge (`result[key] = user[key]` in `_deep_merge`); after the first merge `merged == user_data` so the file is not rewritten again, and the config stably survives.

The template currently contains `default: null` (neutral; existing deployments behave unchanged), so adding `default: flash` in the user dir works stably.

---

## See also

- Profile mechanism implementation and end-to-end verification: see memory `sidecar-per-session-profile-verified`.
- Relay-side `agentKind` passthrough chain (per-session dynamic override, not the local default): see memory `relay-agentkind-chain-implemented`.
