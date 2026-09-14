# TTSE Dual-Track Self-Evolution

TTSE (Two-Track Self-Evolution) induces environment facts (FACT) and capability-selection hints (TIP) from dialogue trajectories and injects catalog guidance. It is independent of [Skill-body evolution](SkillSelfEvolution.md): **it does not rewrite SKILL.md or show an approval dialog**.

It is gated by `react.ttse.enabled` and applies to **agent mode only** (code / team do not mount it). The shipped template is **on by default**: agent mode mounts `TTSERail` and puts `ttse_consult` in the first-turn schema (no `tools_search` required). Set `enabled: false` to turn it off.

```yaml
react:
  ttse:
    enabled: true           # mount TTSERail in agent mode
    evolve_enabled: true    # induce FACT/TIP from trajectories
    inject_enabled: true    # inject system-prompt guidance
    # Auto-dream (silent bank hygiene; does not hijack the user turn)
    dream_enabled: true
    dream_interval: 50
    dream_min_hours: 24.0
    dream_ttl_days: 90
    embedding:
      api_key: "${EMBED_API_KEY}"
      base_url: "${EMBED_API_BASE}"
      model: "${EMBED_MODEL}"
```

The rule bank is always `workspace/.ttse/bank.json`. Disclosure is always `disk_catalog` (P:45 writes guidance only; FACT/TIP bodies go through `ttse_consult`). Neither path is a user setting. `embedding` is optional; env names follow `secret_registry` (`EMBED_API_KEY` / `EMBED_API_BASE` / `EMBED_MODEL`). When all three resolve to non-empty values, consult uses BM25+embedding hybrid recall; otherwise it falls back to BM25.

Auto-dream performs hygiene on an existing FACT/TIP bank (TTL prune, near-duplicate merge, low-quality TIP purge). It is independent of online `induce` / `blame`. The four `dream_*` fields above override the defaults.
