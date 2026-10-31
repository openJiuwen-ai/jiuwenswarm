# TTSE Dual-Track Self-Evolution

TTSE (Two-Track Self-Evolution) induces environment facts (FACT) and capability-selection hints (TIP) from dialogue trajectories and injects catalog guidance. It is independent of [Skill-body evolution](SkillSelfEvolution.md): **it does not rewrite SKILL.md or show an approval dialog**.

Builtin TTSE is gated by `react.ttse.enabled` and supports agent/work mode only. The shipped template is **off by default**: set `enabled: true` to mount `TTSERail` when agent-core provides it (Host skips with a warning if missing). Main code sessions enable TTSE exclusively by selecting the plugin; the legacy switch never mounts a code Rail.

Trajectory induction needs LLM/tool spans: when TTSE is on, the Host auto-acquires `agent_observability` (same as skill/symphony evolution) even if that switch is `enabled: false`; otherwise `run_evolution` silently skips with an empty trajectory.

```yaml
react:
  ttse:
    enabled: false          # off by default; controls builtin TTSE in agent/work mode only
    evolve_enabled: true    # induce FACT/TIP from trajectories
    inject_enabled: true    # inject system-prompt guidance
    # Auto-dream (silent bank hygiene; does not hijack the user turn)
    dream_enabled: true
    consult_top_k: 8        # FACT/TIP hits per track for ttse_consult
    consult_retrieve_mode: hybrid  # hybrid | embed | bm25; dump when the pool is small
    embedding:
      api_key: "${EMBED_API_KEY}"
      base_url: "${EMBED_API_BASE}"
      model: "${EMBED_MODEL}"
```

The rule bank lives under the agent workspace, one bank per mode: `.ttse/work/bank.json` for agent mode and `.ttse/code/bank.json` for code mode (never inside the user's project). Induction, consult and dream never cross banks. Disclosure is always `disk_catalog` (guidance in the system prompt; FACT/TIP bodies go through `ttse_consult`). Neither path is a user setting.

Auto-dream knobs `dream_interval` / `dream_min_hours` / `dream_ttl_days` are fixed at `50` / `24.0` / `90` and are **not user-configurable**. The host assembles builtin work instances; the ZIP factory assembles code plugin instances. With `dream_enabled`, TTSE performs hygiene on an existing FACT/TIP bank (TTL prune, near-duplicate merge, low-quality TIP purge), independent of online `induce` / `blame`.

`embedding` is optional; env names follow `EMBED_API_KEY` / `EMBED_API_BASE` / `EMBED_MODEL`. `consult_retrieve_mode` selects scoring (`hybrid` / `embed` / `bm25`); missing or failed embeddings fall back to BM25, and a small pool still dumps. Call `ttse_consult(category=…, query=<situation sentence>)` with both arguments required; use `category=all` for the whole bank.

## Main coding-agent plugin

Run `python scripts/package_ttse_plugin.py` to build `dist/plugins/ttse_2.0.0.zip`. Import, install, then select it in a main code session. Installation alone does not acquire tracing.

TTSE 2.0.0 uses the [generic native plugin runtime](NativePluginRuntime.md), metadata.plugin_runtime contract v1. The host provides declared services and lifecycle; it no longer contains a TTSE-specific SDK or manager. The ZIP's prepare_plugin_context owns configuration/legacy compatibility and bank validation; its from_plugin_context owns TTSEConfig, induction policy and Rail construction.

Precedence is package defaults, config.yaml.ttse, legacy react.ttse options, then plugins.ttse. Runtime requirements acquire separate service leases. The legacy enabled switch never creates or restores a builtin code Rail; work TTSE remains separate.

Unmount waits for real induction/dream completion, clears owned attachments, and releases the mount's lease while retaining the bank. The generic manager handles timeout watch and package-snapshot rollback. Upgrade the host and ZIP together: TTSE 1.x coding_runtime contracts are obsolete. No new core API is required by this host refactor.
