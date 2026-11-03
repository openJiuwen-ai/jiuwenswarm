# Pattern Selection Guide

> Read this in **Stage 1a** of the SKILL.md workflow, before writing any role files. Picking the wrong pattern is the most common authoring error — patterns dictate role count, isolation rules, integration logic, and `bind.md` failure modes.

## The three primitive patterns

### A. Adversarial / Cross-check

**Use when**: a single agent role-playing N personas converges to similar conclusions because it cannot escape its own priors. The value of the team is **disagreement surfaced as evidence**.

**Shape**: 2–4 roles, run **in parallel**, **isolated** (no cross-visibility), Leader **surfaces contradictions** rather than resolving them.

> When adversarial tension needs **mutual visibility** (e.g., structured debate with rebuttal rounds), use the [Debate mixed pattern](#debate-b--cross-exam--c) instead of standalone A.

**Identity test**: each role's 1-line motto should be **mutually antagonistic**. If two mottos could be spoken by the same person, the roles will converge.

**Example mottos that work** (from `pr-review-swarm`):
- Code reviewer: *"I am looking for what this code does well and how to ship it."*
- Adversarial critic: *"I am trying to break this change before it reaches production."*
- Architect: *"I care about whether this fits the system five years from now."*

**Example mottos that fail** (would converge):
- Reviewer A: *"I look for bugs and security issues."*
- Reviewer B: *"I look for security issues and bugs."*

**Integration rule**: ≥2 roles agree → MUST-FIX. 1 role flags + others silent → SHOULD-FIX. Roles directly contradict → surface verbatim, **do not mediate**.

**Examples**: `pr-review-swarm`, `paper-peer-review-swarm`, `contract-review-swarm` (buyer counsel vs seller counsel + neutral reviewer).

### B. Parallel decomposition

**Use when**: the task naturally splits into N independent sub-tasks that can run concurrently and the integration is non-trivial. The value of the team is **wall-clock speedup + breadth coverage**.

**Shape**: 2–N roles (often variable count via `count: [min, max]` in frontmatter), run **in parallel**, each role works on a **disjoint slice** of the input/problem space, Leader **integrates** by composition (not by judgment).

**Disjointness test**: each role must have a non-overlapping "assignment" (an angle, a category, a region, a layer). If two roles could produce the same finding, the decomposition is broken.

**Example assignments that work** (from `research-to-ppt-swarm`):
- Angle researcher #1: market dynamics
- Angle researcher #2: technology landscape
- Angle researcher #3: regulatory environment

**Example assignments that fail** (overlap):
- Researcher #1: "all things related to the topic"
- Researcher #2: "key information about the topic"

**Integration rule**: collect outputs by slice, deduplicate cross-slice findings, flag gaps where coverage is missing.

**Examples**: pure-B is rare in practice; usually combined with A or C. See `systematic-debug-swarm` (B for parallel hypothesis generation across distinct theory classes).

### C. Specialization pipeline

**Use when**: the task has **sequential expert stages** with **strict handoff contracts**, and blurring stage boundaries causes regressions (e.g., the editor rewriting the strategist's brief, the mitigator skipping triage). The value of the team is **enforced discipline + quality gates**.

**Shape**: 3–5 roles, run **sequentially**, each stage **sees prior stage output** but **cannot rewrite it**, **quality gates between stages** with explicit pass/fail criteria.

**Boundary test**: each role must have an explicit `**Forbidden**: do NOT redo upstream work` clause. Without it, roles drift back into earlier stages.

**Example stage chain** (from `marketing-copy-swarm`):
- Stage 1 brief-strategist → produces strategy brief (gate: brief approved)
- Stage 2 copywriter → produces draft (gate: draft matches brief)
- Stage 3 copy-editor → enhances draft (gate: no rewrites of brief or draft structure)
- Stage 4 conversion-auditor → audits final (gate: meets conversion criteria or kicks back)

**Gate rule**: a stage outputs a structured deliverable; the next stage refuses to start unless the gate passes; failed gates trigger explicit retry / kick-back paths declared in `bind.md` Failure Handling.

**Examples**: `marketing-copy-swarm`, `incident-response-swarm`, `seo-growth-swarm`.

## Mixed patterns (when to combine)

Most non-trivial Swarm Skills combine 2 patterns. The combinations and their use cases:

### A + B (parallel adversarial)

**Use when**: you need both adversarial breadth AND parallel coverage. Multiple parallel investigators with non-overlapping assignments + an adversarial reviewer at the end.

**Example**: `systematic-debug-swarm` (3 parallel hypothesis-generators with forced theory-class diversity + 1 evidence collector + 1 adversarial root-cause-analyst + 1 fix-validator).

**Example**: `security-audit-swarm` (3 parallel auditors: threat-modeler / vulnerability-scanner / dependency-auditor + 1 adversarial attack-chain-synthesizer who must connect their findings into exploit paths).

### B + C (parallel research → pipeline finalization)

**Use when**: parallel breadth is needed early, then a sequential editorial pipeline finalizes the output.

**Example**: `research-to-ppt-swarm` (2–4 parallel angle-researchers → 1 content-curator → 1 slide-designer).

### C + A (pipeline with adversarial gate)

**Use when**: a sequential pipeline benefits from an adversarial reviewer at one or more gates.

**Example**: `design-review-swarm` (analyst → critic → architect, where critic is the adversarial slot).

**Example**: `travel-planning-swarm` (planner-builder produces itinerary; budget-optimizer and experience-maximizer adversarially critique it; logistics-validator ratifies feasibility).

### C + B + A (full stack)

**Use when**: a hard quality gate at entry, then parallel multi-method analysis, then adversarial critique. Reserved for high-stakes analysis pipelines.

**Example**: `data-analysis-swarm` (Stage 0: data-quality-auditor as hard gate → Stage 1: 3 parallel analysts (descriptive / inferential / causal) → Stage 2: 1 adversarial critique-reviewer).

### Debate (B → Cross-exam → C)

**Use when**: roles need to first produce independent positions (isolation prevents premature alignment), then **rebut each other's specific claims** with mutual visibility, then a separate role synthesizes the resolution preserving unresolved disputes.

**Shape**: 2–N roles produce `POSITION` in parallel isolation → each role receives others' prior-round output and produces structured `CROSS_EXAM` in parallel with mutual visibility → 1 synthesizer/judge produces `DECISION`.

**Key distinction from standalone A**: pure A keeps roles permanently isolated — the value is independent judgment that never cross-contaminates. Debate **phase-scopes** the isolation: Round 1 isolated, Round 2+ mutually visible. The adversarial value comes from **structured clash on specific claims**, not permanent blindness.

**Rounds**: Debate supports 1–N cross-exam rounds (each round = all roles receive prior round's outputs and produce structured responses). Round count is a **resource/quality tradeoff** — declare it in `bind.md`, not here. Most scenarios converge in 1–2 rounds.

**Visibility semantics**: the Swarm Skill declares **who sees whose output at which phase**. How this is delivered is a **framework-level implementation choice**, not a Swarm Skill concern — but with a **recommended preference order**: (1) **direct peer-to-peer exchange** (most efficient, lowest information distortion); (2) shared blackboard / shared state; (3) Leader-relay (fallback when the framework does not support peer communication). Frameworks SHOULD implement the highest-priority mechanism they support.

**Example**: `china-ecommerce-compare-debate-swarm` (3 platform experts produce isolated position papers → cross-exam with mutual visibility → value-synthesizer produces verdict with unresolved disputes preserved).

## Decision tree

```
Q1. Does a single agent systematically miss something on this task today?
    NO  → Stop. Build a single-agent skill with create-skill instead.
    YES → Q2.

Q2. What does it miss?
    (a) Adversarial blind spots / convergent self-review                → A
    (b) Breadth / parallel coverage of independent slices               → B
    (c) Discipline at handoffs in a multi-stage pipeline                → C
    (d) Adversarial exchange requiring mutual visibility (debate/rebuttal)
                                                                        → Mixed: Debate (B → Cross-exam → C)

Q3. Does it miss only ONE of (a)/(b)/(c)?
    YES → Use that single pattern.
    NO  → Combine: A+B / B+C / C+A / C+B+A.
          Order matters — pipelines flow stages first, then place A or B
          inside specific stages where the value is highest.

Q4. How many roles? (authoring defaults, not hard limits)
    A pattern: 2-4 roles  (past 4, contradictions combinatorially explode)
    B pattern: 2-N roles  (use count: [min, max]; only real cap is bind.md token budget)
    C pattern: 3-5 stages (past 5, stages tend to be mergeable)
    Mixed:    4-6 roles  (past 6, integration becomes the bottleneck)

Q5. How does Leader integrate outputs?
    A: surface contradictions, do NOT mediate
    B: compose by slice, flag coverage gaps
    C: enforce gates, kick back on failure
    Mixed: stage-by-stage; the pattern of the LAST stage dictates final integration
```

## Anti-patterns

### "Council of clones"

Three roles with the same Identity prose and only nominal name differences. Their outputs converge. Symptom: roles all say "looks good" or all flag the same trivial issues.

**Fix**: rewrite Identity mottos to be mutually antagonistic. Re-read [role-design.md](role-design.md) § Anti-Convergence Techniques.

### "Pipeline that nobody follows"

C-pattern declared, but stages have no quality gates. Result: stages bleed into each other; the editor rewrites the brief; the mitigator skips triage.

**Fix**: every stage transition needs a gate definition with explicit pass/fail criteria, and every role's `Boundary` must have `**Forbidden**: do NOT redo upstream stage X` clauses.

### "Decomposition without disjointness"

B-pattern declared, but roles overlap on the problem space. Result: 3 researchers all return the same top-3 findings.

**Fix**: assign each role a non-overlapping slice **at dispatch time** (the Leader picks slices from a fixed list and passes the assignment as a parameter to the Inline Persona).

### "Mixed pattern justified by ambition"

Combining A+B+C because "more is better". Result: 6 roles, 3 integration nodes, and nobody can debug it.

**Fix**: only combine patterns when each one is independently justified by Stage 0. If C+B alone solves the problem, do not add A.

## How to record the pattern in the Swarm Skill

The spec **does not have a `pattern: A/B/C` frontmatter field** — declaring the pattern as a string would duplicate what the mermaid diagram already expresses, and the diagram is more precise. Instead, the pattern is expressed implicitly through:

1. **`workflow.md` mermaid diagram** — parallel branches = A or B, sequential chain = C, mixed shapes = mixed.
2. **`roles[]` purpose lines** — adversarial roles say "adversarial", "critic", "challenger"; pipeline stages number themselves or chain explicitly.
3. **`bind.md` Behavioral Constraints** — A-pattern teams have "teammates MUST NOT see each other's output"; C-pattern teams have "no rewriting upstream stage output".

You may add a brief `## Pattern` note in `workflow.md` `## Overview` if it aids the reader, but it is not required by the spec.
