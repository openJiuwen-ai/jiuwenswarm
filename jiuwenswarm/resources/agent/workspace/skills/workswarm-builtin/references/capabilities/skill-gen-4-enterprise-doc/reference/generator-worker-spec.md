# Generator Worker Spec

This document defines how the generator worker should turn SOPs or freeform intent into **executable, usable, output-producing skills**.

Read this file when the worker needs to:

- draft a new skill from an SOP or freeform intent
- decide what the generated `SKILL.md` must make explicit
- infer realistic user inputs and deliverables

For structured SOP → `SOPStructure`, read [sop-structure-pipeline.md](./sop-structure-pipeline.md). For paths and the canonical install flow, read [operator-playbook.md](./operator-playbook.md).

## Core standard: generated skills must be executable

The skill generator is not producing summaries of SOPs. It is producing skills that a future agent can load and **use**.

Every generated skill should make it obvious:

1. what the user may provide
2. what the agent must do
3. what the agent must deliver back

When the SOP explicitly specifies user inputs, required data, or final deliverables, copy those requirements into the generated skill clearly and concretely.

When the SOP does **not** explicitly name them, infer them conservatively from:

- the workflow steps
- the business object being processed
- the systems mentioned
- the final step or business outcome
- the kinds of files, tables, approvals, or reports implied by the SOP

### Examples

- If an SOP says operational data should be compiled into a **spreadsheet** (e.g. “export to Excel” or equivalent wording), the generated skill should explicitly name the inputs the user may supply (e.g. source records, exports, attachments) and state that the final deliverable may need to be a spreadsheet-class file (e.g. Excel or equivalent) aligned with the SOP.
- If an onboarding SOP requires account creation plus manager approval, the generated skill should explicitly say that the agent may need employee identifiers, manager details, and target systems, and that the output may be a completed request package, checklist, or structured confirmation.
- If a policy SOP does not produce a file but does require a formal response, the generated skill should still define a concrete response structure, named fields, approval outcomes, or escalation result.

Do not leave inputs and outputs implicit if the downstream user would otherwise have to guess how to use the generated skill.

### Non-negotiable checklist (every draft)

Use this as a hard gate before you treat a generated `SKILL.md` as done:

0. **Machine-parseable start (standard skill layout)** — This is the **normal industry pattern** for skill `SKILL.md` files: **YAML frontmatter first**, then Markdown. The **first non-empty line** of the file must be `---`, then `name` and `description` (and optional allowed keys), then a closing `---`, then the body. **Do not** prepend chit-chat, headings, or code fences before the opening `---` (a single markdown fence is only acceptable if the **first line inside the fence** is still `---`). JiuwenClaw’s draft pipeline and `validate_skill` depend on this shape.
1. **Executable and outcome-oriented** — The skill tells the runtime agent *what to do* and *what to produce*, not a passive summary of the SOP.
2. **User inputs** — List concrete materials the user might supply (files, IDs, URLs, images, pasted text, forms, tickets, etc.). If the SOP names them, mirror that language; if not, infer from steps and business object.
3. **Deliverables** — Name the expected end state: file type (e.g. `.xlsx` / `.csv`), ticket fields, approval summary, structured reply, checklist, etc. Prefer real artifacts over vague “a response”.
4. **SOP is explicit** — When the SOP states inputs or outputs, **copy them faithfully** into the skill (no softening into generic prose).
5. **SOP is silent** — **Infer conservatively** from workflow, roles, systems, and final-step wording; state assumptions explicitly so reviewers can correct them.
6. **Spreadsheet / tabular outcomes** — When the SOP implies consolidation, reporting, line-item tables, or phrasing like “spreadsheet / Excel / tabular summary”, the skill must state that the agent may need to produce a **spreadsheet-class deliverable** (e.g. Excel or equivalent) and what columns or fields it should reflect.

## What the worker is doing (conceptually)

1. Ingest the source SOP from a local file, a URL, pasted raw text, or a structured SOP object. When **no** document text exists after brief recovery offers, **by default** use **`await skill_gen.sop_fallback.build_intent_fallback_sop(user_intent=…, skill_name_hint=…, invoke_llm_json=…)`** with **`invoke_llm_json`** whenever the runtime can invoke the model: full **`SOPStructure`** (and synthesized **`raw_text`**) plus one internal LLM refinement (**`enrich_fallback_sop_with_llm`**). **Only if** the system **cannot** invoke a model for that step, use **`invoke_llm_json=None`** with that same call, or **`skill_gen.sop_fallback.build_fallback_sop_structure`** (skeleton only). Label the draft as template-driven until the user supplies a canonical source—still not a substitute for attaching a real policy document.
2. Build or consume structured SOP data such as title, purpose, scope, steps, decision points, exceptions, roles, and references.
3. Draft an executable `SKILL.md` that turns the SOP into a usable agent workflow.

## Drafting from an SOP

Build **one** structured representation of the SOP first: full **`SOPStructure`** via **`parse_sop_file` / `parse_sop_raw_text` + `invoke_llm_json`** only, as defined in **[sop-structure-pipeline.md](./sop-structure-pipeline.md)** (prompts live in `scripts/skill_gen/sop_parser.py` and `sop_chunk_merge.py`—do not fork a second extraction spec).

Then the authoring model should use both that **structured summary** and the **full SOP text** (`raw_text` on the structure) to:

1. Keep classification and field boundaries aligned with the extracted `sop_type`, `steps`, `knowledge_items`, `sections`, branches, and exceptions.
2. Assemble the draft skill input from structured sections plus raw text where fidelity matters.
3. Generate a `SKILL.md` tailored to that SOP type.

The structured object keeps the draft organized; the raw text keeps it faithful to the source.

### Intent-only snapshot file (`reference/intent-sop-snapshot.md`)

When **`extraction_meta["fallback_sop"]`** is true (intent-only template from **`build_intent_fallback_sop`** / **`build_fallback_sop_structure`**, not extracted from a policy file), the draft under **`skills-draft/<skill_name>/`** **must** include **`reference/intent-sop-snapshot.md`**.

Contents:

1. A **short warning** at the top (plain Markdown): this file is **not** an authoritative SOP; it records the **synthesized** procedure text used for drafting; treat as **provisional** until the user attaches a real document.
2. The **verbatim** **`SOPStructure.raw_text`** from the same object you pass into skill authoring (no paraphrase).

Do **not** drop this file to save tokens; it is the audit trail for what the fallback produced besides the generated **`SKILL.md`**.

### Same-session install (required for SOP → new skill)

When the target package is written under **`get_agent_workspace_dir() / "skills-draft" / <skill_name>`** (staging next to the runtime **`skills/`** tree), the driving agent **must** complete promotion in the **same workflow**: call **`skills.import_local`** with the **absolute** path to that directory so the skill appears under **`get_agent_skills_dir() / <skill_name>`** and is immediately loadable. See **[operator-playbook.md](./operator-playbook.md)** (**Canonical flow** and **Install the new skill**). This is not a separate optional step for the end user.

## Creating a skill

### Capture intent

Start by understanding what the user wants. The current conversation might already contain context (e.g., “turn this SOP into a skill”). Extract steps, deliverables, and constraints **and proceed** — **do not** require explicit human confirmation before drafting unless a **blocking** input is missing.

Key questions (infer when possible):

1. What should this skill enable the agent to do?
2. When should this skill trigger? What user phrases or contexts should load it?
3. What are the expected user inputs?
4. What is the expected output format or deliverable?

### Draft `skill_name`

- **`skill_name`** (or synonym fields **`name`** / **`target_skill`**) decides the draft and installed folder name: **`get_agent_workspace_dir() / "skills-draft" / <skill_name>`** → **`get_agent_skills_dir() / <skill_name>`** (resolve with the host’s path helpers when available).
- It also enters the generated `SKILL.md` frontmatter and influences later triggering together with `description`.
- Do not default to asking the user to name the skill before you can start.
- If the user already gave a fixed name, pass it through unchanged.
- If the user did not provide a name, choose a short, precise, kebab-case name before drafting.
- Omit `skill_name` only as a fallback; backend auto-slugging is weaker than an explicit choice.

## Interview and research (for non-SOP skills)

When creating a skill from scratch (no SOP), proactively clarify edge cases, input/output formats, example files, success criteria, and dependencies **when missing**. **Default:** draft with reasonable assumptions and refine when the user returns with feedback or clarifications, instead of blocking on a long interview unless the domain is unsafe or ambiguous.

If the environment exposes **MCP tools** or **web/doc search**, use them to reduce guesswork: look up APIs, internal conventions, or similar skills — in parallel when practical. Bring summarized findings back so the user does not have to spell out everything from memory.

## Writing a high-quality `SKILL.md`

This section is the **single normative home** for what the draft package’s **`SKILL.md`** must contain and how it should read — i.e. how this worker writes a **high-quality** skill document, not merely a valid file. It merges (1) the **required fields and body headings** the generator must produce from an interview or from **`SOPStructure` + raw SOP text**, and (2) the **Skill writing guide** material (directory anatomy, progressive disclosure, safety, patterns, tone) that applies to every skill this worker authors. The subsections below are **additive**: they **concretize** the **Non-negotiable checklist (every draft)** and the **Drafting from an SOP** rules; they do **not** relax or replace them.

### Required frontmatter and body structure

Based on the user interview or SOP parsing, the generated skill needs these components:

- **name**: Skill identifier in kebab-case (must match the skill folder name when installed)
- **description**: When to trigger, what it does. This is the primary triggering mechanism — include both what the skill does **and** specific contexts for when to use it. All “when to use” information belongs here, not scattered only in the body. Agents tend to **undertrigger** skills. Counter that with slightly “pushy” descriptions. Example: instead of a narrow title alone, add concrete user phrases and scenarios that should load this skill (aligned to the SOP’s real callers), e.g. “Use when the user mentions …, …, or … even if they do not use the formal process name.”
- **compatibility** (optional): Rarely needed — only when the skill truly depends on specific tools, models, or external services. If omitted, assume standard Jiuwenclaw agent capabilities.
- **the rest of the skill body**: Procedures, formats, examples, safety notes

The generated `SKILL.md` should make the following explicit with clear Markdown headings:

1. **User inputs / data** — What the user may provide (files, fields, paths, URLs, IDs, images, tables, documents).
2. **Processing** — How the agent applies the SOP (aligned to sections, not a verbatim dump).
3. **Deliverables** — Expected artifacts or outputs (file names, table columns, currencies, validation, ticket fields, approval summaries, spreadsheet outputs, exported data, answer structure).

If the SOP mandates a spreadsheet, form, ticket, report, approval package, or another structured output, the generated skill should say so explicitly. If the SOP does not say so but strongly implies one, infer it conservatively and write it explicitly.

### Pointing to bundled `reference/` in generated skills

If the generated skill ships extra markdown under `reference/`, the `SKILL.md` body must say **when** to open each file (e.g., “Read `reference/exceptions.md` only when the user mentions overrides or appeals.”). Avoid dumping long annexes into SKILL.md when a short pointer plus a dedicated file keeps the default context lean.

### Skill writing guide

The following subsections spell out **directory anatomy**, **progressive disclosure**, **safety expectations**, **reusable writing patterns**, and **tone**. They elaborate the same **`SKILL.md`** artifact described in **Required frontmatter and body structure** and **Pointing to bundled `reference/` in generated skills** above; they do not introduce a second, looser standard for the output file.

#### Anatomy of a skill

```text
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description required)
│   └── Markdown instructions
└── Bundled Resources (optional)
    ├── scripts/    - Executable code for deterministic/repetitive tasks
    ├── reference/  - Docs loaded into context as needed
    └── assets/     - Files used in output (templates, icons, fonts)
```

#### Progressive disclosure

Skills use a three-level loading system:

1. **Metadata** (name + description) — Always in context (~100 words)
2. **SKILL.md body** — In context whenever the skill triggers (<500 lines ideal)
3. **Bundled resources** — As needed (unlimited; scripts can execute without loading full file contents into the prompt)

These word counts are approximate — go longer when necessary.

**Key patterns:**

- Keep SKILL.md under 500 lines; if approaching the limit, add a `reference/` layer and **explicit pointers** (when to read which file)
- For large reference files (>300 lines), include a table of contents at the top of that file

**Domain organization**: When a skill supports multiple domains/variants, organize by variant:

```text
cloud-deploy/
├── SKILL.md (workflow + selection)
└── reference/
    ├── aws.md
    ├── gcp.md
    └── azure.md
```

The agent should read only the relevant reference file for the user's context.

#### Principle of lack of surprise

Skills must not contain malware, exploit code, or instructions that weaken system security. Do not help build misleading skills, skills that facilitate unauthorized access, or skills aimed at data exfiltration. A skill's intent and behavior should match what the user was told it does. Legitimate roleplay or training scenarios are fine; deceptive or harmful use is not.

#### Writing patterns

Prefer the imperative form in instructions.

**Defining output formats:**

```markdown
## Report structure
ALWAYS use this exact template:
# [Title]
## Executive summary
## Key findings
## Recommendations
```

**Examples pattern** — Include examples where useful:

```markdown
## Commit message format
**Example 1:**
Input: Added user authentication with JWT tokens
Output: feat(auth): implement JWT-based authentication
```

#### Writing style

Explain to the model *why* things matter rather than relying only on heavy-handed MUSTs. LLMs respond well to reasoning. If the user's feedback is terse, infer the underlying goal and reflect it in the instructions. If you find yourself writing ALWAYS or NEVER in all caps everywhere, treat it as a yellow flag — reframe with rationale.

Use theory of mind to keep the skill **general**, not overfit to one chat. Draft, re-read, tighten.

## Why this file exists

This file exists so the worker has one place to learn:

- how to turn structured SOP input into an executable skill
- how to write explicit user inputs and deliverables
- how to preserve progressive disclosure and output usefulness when authoring `SKILL.md`
- how to improve generated skills without drifting away from the source SOP
