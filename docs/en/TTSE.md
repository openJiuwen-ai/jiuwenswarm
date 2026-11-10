# TTSE Dual-Track Self-Evolution

TTSE (Two-Track Self-Evolution) induces environment facts (FACT) and capability-selection hints (TIP) from dialogue trajectories and injects catalog guidance. It is independent of [Skill-body evolution](SkillSelfEvolution.md): **it does not rewrite SKILL.md or show an approval dialog**.

Single-agent TTSE is gated by `react.ttse.enabled` and mounts `TTSERail` in **agent mode only** (code does not mount it). In the shipped template this switch is **on by default**: agent mode mounts `TTSERail` and puts `ttse_consult` in the first-turn schema (no `tools_search` required). Set `enabled: false` to turn off single-agent TTSE only.

Expert-team TTSE is gated separately by `react.ttse.team_enabled` and is independent of `enabled`. A missing key stays **off**. Set it to `true` to mount `TEAM_TTSE` on the leader and `MEMBER_TTSE` on members.

```yaml
react:
  ttse:
    enabled: true           # single agent; mount TTSERail in agent mode
    team_enabled: false     # expert team; independent of enabled, off by default
    evolve_enabled: true    # induce FACT/TIP from trajectories
    inject_enabled: true    # inject system-prompt guidance
    # Auto-dream (silent bank hygiene; does not hijack the user turn)
    dream_enabled: true
    dream_interval: 50
    dream_min_hours: 24.0
    dream_ttl_days: 90
    consult_top_k: 8        # FACT/TIP hits per track for ttse_consult
    consult_retrieve_mode: hybrid  # hybrid | embed | bm25; dump when the pool is small
    embedding:
      api_key: "${EMBED_API_KEY}"
      base_url: "${EMBED_API_BASE}"
      model: "${EMBED_MODEL}"
```

The rule bank is always `workspace/.ttse/bank.json`. Disclosure is always `disk_catalog` (P:45 writes guidance only; FACT/TIP bodies go through `ttse_consult`). Neither path is a user setting. `embedding` is optional; env names follow `secret_registry` (`EMBED_API_KEY` / `EMBED_API_BASE` / `EMBED_MODEL`). When all three resolve to non-empty values, consult uses BM25+embedding hybrid recall; otherwise it falls back to BM25.

Auto-dream performs hygiene on an existing FACT/TIP bank (TTL prune, near-duplicate merge, low-quality TIP purge). It is independent of online `induce` / `blame`. The four `dream_*` fields above override the defaults.

`consult_retrieve_mode` selects scoring (`hybrid` / `embed` / `bm25`); missing or failed embeddings fall back to BM25, and a small pool still dumps. Call `ttse_consult(category=…, query=<situation sentence>)` with both arguments required; use `category=all` for the whole bank.

## Main agent consultation order

When TTSE injection is enabled and the tool is available, the main-agent prompt
requires a standalone `ttse_consult` as the first tool call for each new non-chat
task, before loading skills, creating todos, or using business tools. Use `all`
when the category is uncertain, and consult even with an empty catalog. Continue
after no matches or failure; task continuations and approval resumes reuse results.
Small talk, unavailable tools, and explicit user opt-outs are exempt. This is a
prompt constraint, not runtime enforcement, and requires the updated agent-core guidance.

## Subagent consultation

When the parent has usable TTSE, all subagents (general-purpose, research, browser,
statusline-setup and custom agents) query its current bank. No separate subagent bank
is created. Dependencies are no longer filtered by the `agent` mode name, so Flash
and other non-Code instances can also use them. A parent without TTSE provides no
consultation capability. Both `task_tool` and the persistent subagent runtime install a lightweight
consultation rail through their common creation entry point. The tool is visible
in the first model call, including when progressive tool loading is enabled.

The subagent prompt requires exactly one standalone `ttse_consult` as the first
tool call for each new non-chat task when the tool is available. Read its result
before loading skills, making an execution plan, creating todos or calling other
tools, even when the user specifies a skill or the catalog is unmatched or empty.
Small talk, unavailable tools and explicit user opt-outs are exempt.
If no catalog is attached, the prompt says so and uses `category=all` for the lookup.
The runtime supplies
the tool, guidance and shared category catalog without retrieving experience bodies.
The model initiates the lookup; this is a prompt constraint, not runtime enforcement.
Choose a relevant category, or
`all` if uncertain, and build query from subtask and environment keywords rather
than copying the full request. Continue on no matches or failure. Reuse results
for the same task, tool-result continuations and approval resumes; consult again
for more specific experience if needed. Subagents do not start their own induction,
blame or Auto-dream.

Logs mark model-initiated lookups with `source=model`. Task summaries include
`consult_calls` for the current invocation; zero calls do not imply that earlier
invocations of the same task have no lookup results.

The existing TTSE/injection switches and explicit tool disabling apply, including
custom agents' `disallowed_tools: [ttse_consult]`. Live children synchronize changes
at their next execution boundary. Tool resources are independent while the bank
is shared. Code instances remain excluded; this consultation entry point does not
enable parent TTSE for other modes.
