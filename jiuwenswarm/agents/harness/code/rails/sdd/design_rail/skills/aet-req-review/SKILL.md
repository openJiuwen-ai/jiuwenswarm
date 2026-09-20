---
name: aet-req-review
description: |
  Gate pipeline for requirement deliverables — enforces automated quality gates and interactive user validation before handover. Trigger when: (1) structured PRD/spec/SDD reviews are required, (2) executing a Stage demanding review-then-revision cycles, (3) workflows necessitate HCritic automated checks coupled with user-in-the-loop revision.
disable-model-invocation: true
metadata:
  pattern: pipeline
  stages: 4
  sub_patterns: [reviewer]
---

<role>

**Triage**: orchestrate the gate pipeline — enforce gate cycles, manage user-interactive revision cycles, and route state transitions based on gate outcomes.

</role>

<guideline>

- The pipeline comprises: a SubAgent gate loop → a user-interactive revision step → a conditional re-gate loop.
- **Triage**: halt any gate loop exceeding 2 iterations → request user intervention (options: ignore and proceed OR continue gate loop).
- Gate Routing: `fully passed` → skip next gate iteration; `conditionally passed` → skip next gate iteration; `failed` → mandatory next gate iteration.
- Post-revision: Systematically verify that chain impacts (terminology, cross-references) are consistently updated across the entire document.

</guideline>

<instruct>

### [R1] SubAgent Gate

- Validate input completeness by confirming path existence ONLY — DO NOT read the content.
- Delegate the deliverable and Review Materials to a Subagent for gate evaluation. Use `task_tool` with an available subagent type from the system's "Available subagent types" list. (Reference the prompt template in <example>).
- If multiple deliverables exist, parallelize delegation to independent subagents. Final routing decisions MUST follow the worst-case result principle.

### [R2] Deliverable Revision (via Subagent)

- CRITICAL: Do NOT read the deliverable content yourself — delegate the revision to a Subagent to avoid context explosion from large file reads.
- Delegate to a Subagent (use `task_tool` with an available subagent type), providing:
  - The gate results from R1 (issue list with severity and location)
  - The deliverable file absolute path
  - Instructions: read the deliverable, address each gate issue sequentially, apply fixes via `write_file` (in-place overwrite), and return a brief revision summary.
- **Diagnostic** (pass to Subagent): never apply superficial text patches — trace back to the deliverable's generation methodology, identify impacted steps, and re-execute from scratch if necessary. Fix the source material, not just the document surface.
- The Subagent must ensure document-wide consistency for all chain impacts (terminology, cross-references) and NEVER introduce new non-conformities.
- After the Subagent returns, use its revision summary for R3 — do NOT read the deliverable yourself.

### [R3] User Gate & Revision

- CRITICAL: Before calling `ask_user`, you MUST output the complete review results as visible content (not just in reasoning). The output MUST include:
  - Gate grade (fully passed / conditionally passed / failed)
  - List of issues found (with severity and location)
  - Revisions applied (what was fixed)
  - Final score if applicable
- After outputting the review results as content, call `ask_user` with ONLY the confirmation question.
- If the user requests modifications:
  - Apply the requested revisions to the deliverable in-place.
  - Ensure document-wide consistency for all chain impacts.
- If the user approves (no modifications needed):
  - Proceed directly to [R4].

> ask_user query: "是否需要修改？如有其他意见请说明，无需修改请直接确认。"

### [R4] End & Advance

- Output a brief review summary: grade, issue count, fix count.
- Output a clear transition message telling the user what stage is next:
  > "文档审查完成。审查结果：[grade]，发现问题 [X] 个，已修复 [Y] 个。现在进入下一阶段：[下一阶段名称]。"
  - The next stage name is specified in the methodology frame above — use it in the message.
- **CRITICAL**: After outputting the transition message, call the `sdd_advance` tool to proceed to the next stage. Do NOT guess or skip.
- Halt all further gate-pipeline actions after calling `sdd_advance`.

</instruct>

<example>

## SubAgent Delegation Prompt Template (Used in [R1])

```text
You are an authoritative Quality Assurance Reviewer. Your objective is to rigorously evaluate the deliverable against the specified gates.

## Input
- Review Materials: <path_to_review_materials>
- Deliverable: <path_to_deliverable>

## Task
1. Strictly adhere to the criteria, methodology, and checklists defined in the **Review Materials**.
2. Scrutinize the **Deliverable** to identify all logical flaws, missing requirements, inconsistencies, and non-conformities.
3. Output a definitive gate grade. It MUST be exactly one of the following: [fully passed, conditionally passed, failed].
4. If a SubAgent returns any grade outside [fully passed, conditionally passed, failed] or returns malformed output, treat the result as 'failed' and request a SubAgent rerun; if the SubAgent is unresponsive after one retry, escalate to user intervention with error details.
5. Provide a structured, exhaustive list of all identified issues, accompanied by specific and actionable remediation recommendations.
```

</example>

<constraint>

- NEVER fabricate content — all deliverable text must strictly trace back to Skill methodology or user input.
- **Relentless**: never bypass a failed gate — enforce the fix-and-reassess loop (within maximum iteration limits).
- Halt: any gate loop exceeding 2 iterations → halt, request user intervention. The user may choose to either ignore and proceed to the next stage OR continue the gate loop (resetting the iteration counter).
- ALWAYS enforce the worst-case result principle for multi-deliverable routing.
- ERROR RECOVERY: If a Skill or SubAgent crashes after processing begins, capture partial results, set state='error', notify the user with the error log, and offer choices: retry (maximum one automatic retry), skip-to-end, or abort. Never silently swallow errors or continue without user acknowledgment.
- **Self-contained**: Do NOT attempt to load any external skills via skill toolkit — the methodology is inlined. Use the built-in `ask_user` tool for all user interactions.

</constraint>

<patch>

- Do not dynamically compress **critical sub-agent findings** or **important code snippets** your exploration before the design is finalized — doing so risks losing essential details that degrade design quality.
- Dynamic Review Materials: If the Review Materials are executable scripts/tools rather than static documents, NEVER execute them directly. Mirroring the strict content-blindness rule, explicitly delegate tool execution to the SubAgent to derive the gate results.

</patch>

<input>

1. **Deliverable Path to be Reviewed**
   - If the path is missing -> abort with fatal error. The gate pipeline cannot proceed without a deliverable.

2. **Review Materials Path**
   - Encompasses `reviewer role` and `checklist` (may be unified into a single document).
   - If the path is missing -> prompt the user: "No Review Materials found. Options: (1) provide materials, (2) proceed with limited checks (treat all checks as 'failed'), (3) abort."

</input>

<output>

- The in-place modified deliverable (DO NOT generate or output to new files).

</output>

<condition>

### Initial Gate Loop

- IF R1 gate PASS (`fully` OR `conditionally`) THEN execute R2 revisions -> proceed directly to R3.
- IF R1 gate FAIL AND R1 iteration count < 2 THEN execute R2 revisions -> return to R1 for re-gate.
- IF R1 gate FAIL AND R1 iteration count >= 2 THEN halt -> request user intervention (options: ignore and proceed to R3 OR continue gate loop with reset iteration counter).

### User Gate

- IF R3 user makes ZERO modifications THEN proceed directly to R4.
- IF R3 user MAKES modifications THEN optionally re-gate (ask user: "是否需要复审？" with options ["需要复审", "跳过复审"]).
  - IF "需要复审" THEN return to R1 for re-gate.
  - IF "跳过复审" THEN proceed to R4.

</condition>
