# Default skill directory paths per harness

When `skvm-jit` needs to pass `--skill=<dir>` to `skvm jit-optimize`, it must
resolve the on-disk directory that holds the skill's `SKILL.md`. The agent
should already know which harness it is running inside; look up the matching
section below and probe the listed paths in order. If none contain a
`SKILL.md` for the skill name you are looking for, ask the user — do not
guess.

This file is loaded on demand from Step 1 of the parent `SKILL.md`. It is not
a skill itself.

## Claude Code

1. Project-scoped:  `./.claude/skills/<name>/`
2. User-scoped:     `~/.claude/skills/<name>/`
3. Plugin-provided: `~/.claude/plugins/<plugin>/skills/<name>/`

Source of truth: Claude Code skill-loader documentation. The first match wins
in the order above.

## opencode

1. Project-scoped:  `./.opencode/skills/<name>/`
2. User-scoped:     *(confirm with user — not derivable from the adapter code)*

The `.opencode/skills/<name>/` layout is what `src/adapters/opencode.ts`
writes in discover mode, so it is the path opencode's own loader reads from.

## openclaw

1. User-scoped:     *(confirm with user — the adapter writes to a workspace-
   local `skills/<name>/` directory under `/tmp/skvm-openclaw/<agentId>/`,
   which is a bench artifact, not the harness's persistent skill root)*

## hermes

1. User-scoped:     `~/.hermes/skills/<name>/`

Source of truth: `src/adapters/hermes.ts` line 249 — `hermesHome/skills/<name>`.
Hermes has no documented project-scoped location.

## jiuwenswarm

Not supported. jiuwenswarm runs in inject-only mode: skill content is prepended
to the prompt and has no persistent on-disk copy. `skvm-jit` cannot submit
feedback for a jiuwenswarm run because there is no `--skill=<dir>` to pass.
Tell the user and stop.

## bare-agent

Not applicable for runtime feedback. `bare-agent` is SkVM's own minimal
harness and only runs inside `skvm bench`; it is not a production harness an
end-user task would run under. If you find yourself here, you are almost
certainly running inside a bench and should not be triggering `skvm-jit`
yourself — the bench orchestrator owns that flow.
