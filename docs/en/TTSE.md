# TTSE Dual-Track Self-Evolution

TTSE (Two-Track Self-Evolution) induces environment facts (FACT) and capability-selection hints (TIP) from dialogue trajectories and injects catalog guidance. It is independent of [Skill-body evolution](SkillSelfEvolution.md): **it does not rewrite SKILL.md or show an approval dialog**.

It is gated by `react.ttse.enabled` and applies to **agent mode only** (code / team do not mount it). The shipped template is **off by default**: set `enabled: true` to mount `TTSERail` when agent-core provides it (Host skips with a warning if missing).

Trajectory induction needs LLM/tool spans: when TTSE is on, the Host auto-acquires `agent_observability` (same as skill/symphony evolution) even if that switch is `enabled: false`; otherwise `run_evolution` silently skips with an empty trajectory.

```yaml
react:
  ttse:
    enabled: false          # off by default; true mounts TTSERail in agent mode
    evolve_enabled: true    # induce FACT/TIP from trajectories
    inject_enabled: true    # inject system-prompt guidance
    # Auto-dream (silent bank hygiene; does not hijack the user turn)
    dream_enabled: true
    embedding:
      api_key: "${EMBED_API_KEY}"
      base_url: "${EMBED_API_BASE}"
      model: "${EMBED_MODEL}"
```

The rule bank is always `workspace/.ttse/bank.json`. Disclosure is always `disk_catalog` (guidance in the system prompt; FACT/TIP bodies go through `ttse_consult`). Neither path is a user setting.

Auto-dream knobs `dream_interval` / `dream_min_hours` / `dream_ttl_days` are Host-fixed at `50` / `24.0` / `90` and are **not user-configurable**. With `dream_enabled`, TTSE performs hygiene on an existing FACT/TIP bank (TTL prune, near-duplicate merge, low-quality TIP purge), independent of online `induce` / `blame`.

`embedding` is optional; env names follow `EMBED_API_KEY` / `EMBED_API_BASE` / `EMBED_MODEL`. When all three resolve to non-empty values, consult uses BM25+embedding hybrid recall; otherwise it falls back to BM25.
