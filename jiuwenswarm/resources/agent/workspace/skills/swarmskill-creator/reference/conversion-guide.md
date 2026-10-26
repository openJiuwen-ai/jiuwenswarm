# Conversion Guide: Single-agent Skill → Swarm Skill

> Read this in **Stage 1b** when the user points at an existing single-agent skill and asks to convert it. After this stage, return to the main SKILL.md workflow at Stage 1a (pattern selection) onward.

## When NOT to convert

Conversion is only worth it if you can answer **"yes"** to at least one of these:

1. **The source skill embeds 2+ personas inside one prompt** (e.g., `adversarial-reviewer` has Saboteur + New Hire + Security Auditor inside one SKILL.md). The single agent cannot truly hold multiple POVs simultaneously — outputs converge. → **A-pattern conversion.**
2. **The source skill has 3+ checklist categories that are independently investigable** (e.g., a security skill with separate auth / input-validation / dependency / config sections). Running these in parallel buys both wall-clock and depth per slice. → **B-pattern conversion.**
3. **The source skill describes a sequential workflow with quality steps** (e.g., a copywriting skill that says "first write a brief, then draft, then edit, then audit"). Single agents cut corners on phase boundaries. Enforcing them via separate roles + gates lifts quality. → **C-pattern conversion.**

If none apply: **stop**. Tell the user the skill is already well-suited to a single agent. Conversion would add overhead without clear benefit.

## Conversion pipeline (5 steps)

### Step 1: Read the source skill end-to-end

Look for these structural signals:

| Signal in source skill | Maps to |
|---|---|
| `## Persona 1 / ## Persona 2 / ...` sections | A-pattern roles (1 per persona) |
| `## Checklist: A / B / C / ...` sections | B-pattern roles (1 per disjoint category) |
| `## Step 1 / ## Step 2 / ...` workflow with explicit deliverables | C-pattern stages (1 per step with handoff) |
| `## Anti-patterns` enumerating common single-agent failures | Each anti-pattern hints at the team value (what the team prevents) |
| `MUST find at least N issues` or similar forced-output rules | These become role-level `Boundary > Mandatory` |
| `Inspection process: 1. ... 2. ... 3. ...` | These become Inline Persona steps |

### Step 2: Articulate "what is lost in single-agent form"

Write a one-paragraph **conversion rationale** answering:

> *"Today, when a single agent runs this skill, what does it systematically miss or under-deliver on?"*

If you cannot answer crisply, conversion is premature. Examples of crisp answers:

- **`adversarial-reviewer` → `pr-review-swarm`**: "The single agent role-plays 3 personas serially, but its outputs converge because it cannot escape its own analytical priors. Independent parallel agents produce genuinely different findings."
- A multi-stage incident-response skill → team form: "The single agent skips the postmortem after mitigation succeeds, because the user's perceived crisis is over. Separating roles + gating mitigation→postmortem enforces the discipline."
- A copywriting skill → team form: "The single agent collapses brief and copy into one pass, losing the audit trail and the discipline of editing without rewriting."

This rationale becomes the Stage 0 justification and gets cited in the new Swarm Skill's SKILL.md `description` (use case) + workflow.md Overview.

### Step 3: Decompose into roles

Apply the heuristics from Step 1 to extract role candidates. Then run the **disjointness test**:

For each pair of role candidates (A, B), ask: *"Could one role's deliverable substitute for the other's?"*

- If yes for any pair → roles overlap → merge or redesign.
- If no for all pairs → decomposition is clean, proceed.

Record each role as a tuple: `(id, motto, success criteria 1-liner, methodology)`.

### Step 4: Pick the pattern

Use [pattern-selection.md](pattern-selection.md). For converted skills, the pattern is usually obvious from Step 1's structural signals:

| Source signal dominant | Pattern |
|---|---|
| Multiple personas in one SKILL.md | A |
| Multiple disjoint checklists | B |
| Multi-step workflow with deliverables | C |
| Personas + checklists | A + B |
| Multi-step workflow with adversarial review at one step | C + A |
| Multi-step workflow with parallel research at one step | C + B |

### Step 5: Generate the new Swarm Skill via the standard pipeline

Continue from main SKILL.md **Stage 2 onward** (Role design → Workflow → Bind → Dependencies → Templates → Validate).

When filling in templates, **port content from the source skill aggressively**:

| Source skill content | Goes to |
|---|---|
| Persona descriptions | `roles/<id>.md` `## Identity` (rewrite first line as 1-line motto!) |
| Per-persona "Inspection Process" or steps | `roles/<id>.md` `## Inline Persona for Teammate` |
| `MUST find` rules / forced output rules | `roles/<id>.md` `## Boundary > Mandatory` |
| Output format templates | `roles/<id>.md` `## Output Schema` |
| Workflow / sequence descriptions | `workflow.md` `## Detailed Steps` |
| Anti-patterns / pitfalls | `bind.md` `## Behavioral Constraints` (if team-level) or `## Failure Handling` (if recovery-related) |
| Tool lists (`gh`, `grep`, `python`) | `roles[].tools` in SKILL.md frontmatter + `dependencies.yaml` `tools` segment |
| Required Skills (cross-references) | `roles[].skills` in SKILL.md frontmatter + `dependencies.yaml` `skills` segment with `source: <URL>` if external |

## Worked example: `adversarial-reviewer` → `pr-review-swarm`

This is the canonical conversion. Walk through it as a template.

### Source structure

A real public single-agent skill `adversarial-reviewer` (an engineering-swarm skill that runs 3 review personas serially inside a single SKILL.md):

```
---
name: adversarial-reviewer
description: Reviews PRs by running 3 internal personas...
---

## Persona 1: The Saboteur
Mindset: ...
Priorities: ...
Review Process: ...
MUST-Find Rule: ...

## Persona 2: The New Hire
Mindset: ...
Priorities: ...
...

## Persona 3: The Security Auditor
Mindset: ...
Priorities: ...
...

## Severity Table
- BLOCK / SIGNIFICANT-RISK / ACCEPTABLE-RISK / LOW-RISK

## Output Format
...
```

### Conversion steps applied

1. **Read end-to-end** → 3 distinct personas with distinct mindsets, distinct priorities, distinct MUST-find rules. Severity table is shared. Output format is shared.

2. **Articulate loss** → "A single agent serially role-playing 3 personas converges to similar findings because it cannot drop its priors between persona switches. Independent parallel agents produce genuinely different findings; running them in isolation is the value."

3. **Decompose** → 3 roles emerge naturally: `code-reviewer` (forward review of quality/test coverage; New Hire-flavored), `critique-adversarial` (Saboteur), `architect` (Security Auditor + cross-cutting). Disjointness check: each role has a distinct domain (quality / risks / architecture). PASS.

4. **Pattern** → A (parallel adversarial). Add Architect for cross-cutting view.

5. **Generate**:
   - Each persona's Mindset → role's `## Identity` 1-line motto (rewrite the prose Mindset into a single quotable sentence).
   - Each persona's Priorities → role's `## Success Criteria` "Focus areas" line.
   - Each persona's MUST-Find Rule → role's `## Boundary > Mandatory`.
   - Severity Table → shared `## Output Schema` Verdict enum across all 3 roles.
   - Output Format → role-specific `## Output Schema` template.
   - The 3 personas' Inspection Process content → each role's `## Inline Persona for Teammate`.
   - New file: `workflow.md` with mermaid showing parallel dispatch + Leader integration node.
   - New file: `bind.md` with `max_parallel_teammates: 3`, `total_token_budget: 200k`, "Leader does not write review content", "Teammates MUST NOT see each other's output".
   - New file: `dependencies.yaml` with `gh` (required) and `grep` (required) — preserved from source skill's tool list.

### Result

`pr-review-swarm`. The conversion preserves all source content while adding:

- **True parallelism** (3 isolated agents instead of 1 serial agent)
- **Anti-overlap boundaries** (each role's `**Forbidden**:` block names the other roles)
- **Quality gates** (workflow surfaces contradictions verbatim instead of letting one agent silently mediate)
- **Resource accounting** (`bind.md` makes the 3× token cost explicit)

---

## Conversion compliance checklist

Before declaring a conversion done, verify:

- [ ] **Stage 0 justification written** (1 paragraph: "what is lost in single-agent form")
- [ ] **Pattern picked** with reference to source signals (Step 1 table)
- [ ] **All source persona Mindsets** rewritten as 1-line mottos
- [ ] **All source MUST-Find rules** ported into role `Boundary > Mandatory`
- [ ] **All source Output Formats** preserved in role `Output Schema`
- [ ] **All source tools** declared in `roles[].tools` + `dependencies.yaml > tools`
- [ ] **All source cross-referenced Skills** declared in `roles[].skills` + `dependencies.yaml > skills`
- [ ] **`bind.md` Behavioral Constraints** explicitly state isolation rule (A-pattern teams)
- [ ] **Validator passes** on the new Swarm Skill directory
- [ ] **Optional: `MIGRATION.md`** at the root of the new Swarm Skill, documenting the source skill, the conversion rationale, and the team-vs-single delta (useful for the user to understand what they gained)
