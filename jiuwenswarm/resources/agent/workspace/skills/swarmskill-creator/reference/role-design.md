# Role Design Guide

> Read this in **Stage 2** (and revisit during Stage 3 for gate design). This file teaches how to author a single `roles/<id>.md` file that passes both spec compliance and the anti-convergence test.

## The 5 mandatory sections

Every role file starts with an `# Role: <Name>` H1 title, then MUST contain exactly these 5 sections, in order:

1. `## Identity` — POV with mandatory 1-line motto first
2. `## Success Criteria` — what success looks like + focus areas
3. `## Boundary` — Forbidden + Mandatory
4. `## Output Schema` — structured output template
5. `## Inline Persona for Teammate` — pasteable prompt

The validator script enforces section presence. The judgment-call quality of each section is what this guide is about.

---

## Section 1: Identity

### Hard rule: first line is a 1-line motto

The motto is the **#1 anti-convergence mechanism** in the spec. Without it, prose Identity sections cause sibling roles to converge in output. The motto must:

- Be a **single sentence** in `> *"..."*` blockquote-italic format
- Be **first-person** (this is the role speaking)
- Be **mutually antagonistic** with sibling roles in an A-pattern team
- Crystallize a **point of view**, not a job title

**Good mottos**:

```markdown
> *"I am trying to break this code in production."*
> *"I am the user. I want to ship value, not perfect code."*
> *"I care about whether this fits the system five years from now."*
> *"I will refuse the deal if even one term smells wrong."*
> *"I am the data. I will tell you what is true even if it disappoints you."*
```

**Bad mottos** (and why):

| Bad | Why |
|---|---|
| *"I am a senior security engineer."* | Job title, not POV — could be said by 100 different roles |
| *"I find bugs and security issues."* | Capability description — converges with any reviewer role |
| *"I help the user."* | Vacuous — every role helps the user |
| Multi-sentence motto | Dilutes the POV — must be ONE sentence |
| Third-person ("This role looks for...") | Loses the in-character voice |

### After the motto: 0–2 paragraphs of context

Optional. Use these paragraphs to:

- Name the methodology the role applies (STRIDE, OWASP Top 10, SCQA, 5-Whys, etc.)
- Specify the role's "default mode" (skeptical / generous / adversarial / clinical)
- Explain the role's "when called in" — under what conditions does this POV add value

**Do not use these paragraphs to**:
- Repeat the Success Criteria — that's the next section, do not duplicate it here
- List checklists (those go in `Inline Persona`, where the teammate actually sees them)

---

## Section 2: Success Criteria

### Format

```markdown
## Success Criteria

- [Concrete deliverable 1]
- [Concrete deliverable 2]
- [Concrete deliverable 3]

**Focus areas**: [comma-separated list of what this role should prioritize examining]
```

### Quality bar

Each bullet should be **observable** — a reviewer should be able to look at the role's output and check ✓/✗ against it. Avoid:

| Vague | Concrete |
|---|---|
| "Provides good analysis" | "Returns at least 1 concrete failure scenario with evidence" |
| "Considers all angles" | "Coverage spans security / performance / readability — flagged separately" |
| "Helps the user" | "Output uses the schema in § Output Schema, no missing required fields" |

The "Focus areas" line tells the role what to prioritize examining — keep it tight, 5–10 comma-separated items.

---

## Section 3: Boundary

### Format (mandatory split)

```markdown
## Boundary

**Forbidden** (prevent role overlap):
- Do NOT [thing that another role does]
- Do NOT [thing that drifts upstream/downstream in the pipeline]

**Mandatory**:
- You MUST [thing the role tends to skip when it sees nothing obvious]
- You MUST [output discipline that the schema enforces]
```

### Why both halves are required

- **Forbidden** prevents **lateral drift** — in A/B patterns, roles bleed into each other's territory; in C patterns, stages drift upstream/downstream.
- **Mandatory** prevents **silent under-delivery** — adversarial roles default to "looks fine" when they cannot find obvious issues; coverage roles skip slices when the slice seems empty.

A role with only Forbidden tends to output empty findings. A role with only Mandatory tends to encroach on siblings. Both halves work as opposing forces.

### Examples

**A-pattern role** (`pr-review-swarm` adversarial-critic):

```markdown
**Forbidden**:
- Do NOT critique style or readability — that's the Code Reviewer's job.
- Do NOT propose architectural redesigns — that's the Architect's job.

**Mandatory**:
- You MUST find at least 1 concrete failure scenario.
  If you found nothing, you didn't look hard enough — recheck error paths and edge cases.
```

**C-pattern role** (`marketing-copy-swarm` copy-editor):

```markdown
**Forbidden**:
- Do NOT rewrite the strategy brief — the brief-strategist's output is the source of truth.
- Do NOT change the message hierarchy from the copywriter's draft.
- Do NOT add new value propositions not in the original brief.

**Mandatory**:
- You MUST preserve the brief's audience, tone, and CTA intent.
- You MUST track every edit as accept/reject so the audit stage can verify provenance.
```

---

## Section 4: Output Schema

### Format

```markdown
## Output Schema

\`\`\`markdown
## Role: <Role Name>

### <Section 1>
- [...]

### <Section 2>
- [...]

### Verdict / Decision / Output
- <enum: BLOCK / SHIP / KICK-BACK / ...>
\`\`\`
```

### Why a schema (not free text)

- **Leader integration depends on parseable structure** — A-pattern Leaders count agreements, B-pattern Leaders compose by slice, C-pattern Leaders check gates. All three need predictable output shapes.
- **Schemas force the role to commit** — a role that returns "here are some thoughts" lets the Leader off the hook. A role that must produce a `Verdict: BLOCK / SIGNIFICANT-RISK / ACCEPTABLE-RISK / LOW-RISK` is forced to take a position.

### Schema design rules

1. Always include a **Verdict / Decision / Output** terminal section that uses an **enum**, not free text. The Leader uses this for routing/integration.
2. Use **JSON** instead of Markdown if the output is consumed programmatically (rare for Swarm Skills — most consumers are LLMs reading Markdown).
3. Match the schema 1:1 with what `Inline Persona` tells the teammate to produce. Drift between the two = teammate output that doesn't match the schema = Leader integration failure.

---

## Section 5: Inline Persona for Teammate

### Why this section exists

> Most adopting agents do NOT auto-load role files when dispatching teammates. The Leader must extract this section verbatim and inline it into the dispatch prompt. **Without this section, Swarm Skills do not run on the majority of adopting agents.**

### Format

```markdown
## Inline Persona for Teammate

\`\`\`
ROLE: <Role Name> in a Swarm Skill.

[1-2 sentences: who you are, what your default mode is]

[1-2 sentences: what you must produce, the Mandatory + Forbidden in plain prose]

INPUTS YOU WILL RECEIVE:
- <input 1>: {INPUT_1_PLACEHOLDER}
- <input 2>: {INPUT_2_PLACEHOLDER}

OUTPUT FORMAT (use exactly this structure):
## Role: <Role Name>
### <Section 1>
- ...
### Verdict
- <enum value>
\`\`\`
```

### Quality rules

1. **Self-contained** — the teammate has no access to the parent context, the role file, the workflow, or the bind file. Everything needed to perform the role must be in this prompt.
2. **Use placeholders** — `{PR_DIFF}`, `{TOPIC}`, `{ANGLE_ASSIGNMENT}`, `{PRIOR_STAGE_OUTPUT}` are filled in by the Leader at dispatch time. Make them obvious.
3. **Reproduce the Output Schema verbatim** — do not paraphrase; the teammate's output must match what the Leader expects.
4. **Keep it < 50 lines** — longer personas indicate the role is doing too much; consider splitting.

---

## Anti-convergence techniques

When 2+ roles in an A-pattern team risk producing similar outputs, apply these techniques:

### Technique 1: Forced theory class assignment

Each role is assigned a **fixed category** at dispatch time. Used in `systematic-debug-swarm`:

- Hypothesis-generator-1 → "Data/State theories only"
- Hypothesis-generator-2 → "Concurrency/Timing theories only"
- Hypothesis-generator-3 → "Environment/Configuration theories only"

The Inline Persona contains: *"You are restricted to {THEORY_CLASS} hypotheses. Do not propose hypotheses outside this class."*

### Technique 2: Antagonistic mottos

Make each role's Identity motto explicitly contradict its siblings. See the "Bad mottos" table above for what to avoid.

### Technique 3: Different methodologies, same input

Each role applies a **different framework** to the same input:

- Threat-modeler → STRIDE
- Vulnerability-scanner → OWASP Top 10
- Dependency-auditor → CVE database + license audit

The methodology constrains the output shape, preventing convergence.

### Technique 4: Adversarial dispatch order (rare)

For C-patterns with an adversarial gate, dispatch the adversary **after** seeing the prior stage's output, with explicit instruction to disagree. Used in `data-analysis-swarm` critique-reviewer: *"Your job is to find the analysis's failure modes, not to validate it."*

---

## Gate design (for C-patterns and pipeline stages)

A quality gate sits between two stages and decides whether stage N's output is good enough to pass to stage N+1.

### Gate components

Every gate has 4 parts (declared in `workflow.md` Detailed Steps):

1. **Trigger** — when does the gate run? (Always after stage N completes; sometimes only on certain stage outcomes.)
2. **Pass criteria** — explicit, observable, ideally count-based (e.g., "brief contains all 5 mandatory sections").
3. **Fail action** — kick back to which stage? Retry how many times? Escalate to user when?
4. **Bypass conditions** (optional) — when can the user override the gate?

### Gate examples

**Brief gate** (`marketing-copy-swarm`, between Stage 1 and Stage 2):

> **Pass criteria**: brief contains audience definition, value proposition, message hierarchy, CTA, success metrics. All 5 sections must be present and ≥3 sentences each.
> **Fail action**: kick back to brief-strategist with the missing sections listed. Max 2 retries; on 3rd failure, surface to user with partial brief and ask how to proceed.

**Data quality gate** (`data-analysis-swarm`, before Stage 1 starts):

> **Pass criteria**: Data Quality Score (DQS) ≥ 0.7 across completeness, consistency, validity. GO verdict from data-quality-auditor.
> **Fail action** (NO-GO): halt the pipeline. Return DQS report to user with specific defects. Do not allow analysts to start.

### Gate anti-patterns

| Anti-pattern | Symptom | Fix |
|---|---|---|
| "Looks good to me" gate | Subjective; passes everything | Replace with count-based / enum-based criteria |
| Gate without retry policy | Pipeline halts on first failure | Add max retries + escalation rule |
| Gate that the next stage ignores | Stage N+1 starts even on FAIL | `bind.md` must declare "Stage N+1 MUST refuse to run if gate FAIL" |
| Gate redundant with `Boundary` | Forbidden rule in role file + Pass criteria in workflow say the same thing | Pick one — Boundary if it's per-role behavior, gate if it's per-stage transition |

---

## Putting it together: a role file checklist

Before declaring a role file done, verify:

- [ ] First line of `## Identity` is a `> *"..."*` motto (1 sentence, first-person, distinctive POV)
- [ ] Motto is mutually antagonistic with sibling roles in A-patterns / disjoint in B-patterns / non-overlapping in C-patterns
- [ ] `## Success Criteria` has 3–6 observable bullets + a "Focus areas" line
- [ ] `## Boundary` has BOTH `**Forbidden**:` and `**Mandatory**:` blocks
- [ ] `## Output Schema` has a terminal `Verdict / Decision / Output` enum
- [ ] `## Inline Persona for Teammate` is self-contained, uses placeholders, reproduces the schema, < 50 lines
- [ ] No section duplicates content from another section
- [ ] Role file size: ~80–200 lines (significantly more = role doing too much; less = role under-specified)
