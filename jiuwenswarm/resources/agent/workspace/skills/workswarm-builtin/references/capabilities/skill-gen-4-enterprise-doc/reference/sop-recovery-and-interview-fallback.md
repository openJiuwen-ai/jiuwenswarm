# SOP ingest recovery + structured interview fallback

Use this file when **generating a skill from an SOP** but the system **cannot obtain SOP plain text** on the first try (bad path, missing file, fetch failed, empty body, permission, etc.).

It defines three layers:

1. **Recovery (ordered, user-facing)** — one clear menu so the user picks how to supply material next.
2. **Structured fallback (interview)** — if there is still no document, **build a surrogate SOP** through guided questions so downstream steps see the same **`SOPStructure`-shaped** content as LLM extraction would produce (see **Alignment** below).
3. **Insufficient input** — what to do when the user **does not answer**, **cannot** supply fields, or only knows part of the story (see **Part 3**).

---

## Part 1 — Recovery menu (run first, in order)

After any ingest failure, **do not** silently switch modes. Send **one** short message that:

1. States the failure in plain language (no stack traces to the user unless they ask).
2. Presents **exactly** these options **in this order** (user picks one number or combines 1+2 over turns):

| # | Option | What you do next |
|---|--------|------------------|
| **1** | **Corrected absolute path** | Retry local read / `sop-text` with the path the user gives. Confirm the file exists before promising extraction. |
| **2** | **Paste the SOP** | User pastes full text (or large excerpt). Use **`parse_sop_raw_text`** on that string per [sop-structure-pipeline.md](./sop-structure-pipeline.md). |
| **3** | **URL** | Run `url-fetch` (or equivalent), take page `text` as body, then **`parse_sop_raw_text`**. |
| **4** | **Draft without a document** | Do **not** invent a full SOP by hand. **Default:** **`await skill_gen.sop_fallback.build_intent_fallback_sop(..., invoke_llm_json=...)`** with **`invoke_llm_json`** whenever the runtime can invoke the model; **`build_fallback_sop_structure`** or **`invoke_llm_json=None`** only when the system cannot ([sop-structure-pipeline.md](./sop-structure-pipeline.md)). Then continue skill drafting per [generator-worker-spec.md](./generator-worker-spec.md). **Or** — if the user wants something closer to a real SOP first — start **Part 2** (interview fallback). |

**Suggested wording (adapt tone to channel):**

> I couldn’t load that SOP. Choose how to continue:  
> **(1)** Send a **correct absolute path** to the file.  
> **(2)** **Paste** the SOP text here.  
> **(3)** Send a **URL** to the page or doc.  
> **(4)** Say **draft without a document** — I’ll use only what we’ve already discussed in chat **or** I can walk you through a short **Q&A** to capture a structured substitute (see below).

If the user ignores the menu and pastes text or a URL, treat that as implicitly choosing **(2)** or **(3)** and proceed.

---

## Part 2 — Structured fallback: interview → surrogate SOP

**Goal:** Produce text that is complete enough to run **`parse_sop_raw_text`** + **`invoke_llm_json`** so you stay on the **single extraction path** in [sop-structure-pipeline.md](./sop-structure-pipeline.md), **or** (if the host allows) assemble a JSON object compatible with **`SOPStructure`** and skip re-extraction — **default preference: markdown assembly + `parse_sop_raw_text`** so normalization stays consistent.

**Principle:** The interview is **not** open-ended chat; it **fills fields** under **constraints** so the result maps cleanly to **`SOPStructure`** (`scripts/skill_gen/models.py`).

### Constraints (v1 — tighten later)

| Field | Constraint |
|-------|------------|
| `title` | Short name of the process or policy. Required before finishing. |
| `purpose` | 1–3 sentences: why this exists. Required. |
| `scope` | Who/what/when this applies; explicit “out of scope” if known. Required. |
| `sop_type` | Exactly one of: `procedural` (mostly steps), `knowledge` (mostly rules/policies), `hybrid`. Required. |
| `roles` | List of role names (strings). Empty list allowed only if truly N/A; if N/A, write one sentence in `scope`. |
| `steps` | Ordered list. Each step has: `step_number`, `actor`, `action`, `system` (tool/system or “—”), `output` (artifact or “—”), `notes`. For `knowledge`-heavy SOPs, steps may be few; use `knowledge_items` instead. |
| `knowledge_items` | Atomic rules (one rule per bullet/string). No narrative essays. |
| `decision_points` | “If … then …” branches as short strings. |
| `exceptions` | Escalations, waivers, edge cases. |
| `references` | Links, policy IDs, ticket types, system names. |
| `sections` | Optional outline mirrors; each item `{"heading": str, "summary": str}` is enough for v1. |

**Quality gate before you call extraction:**  
At least **two** of the following must be non-empty: `steps`, `knowledge_items`, `sections` (with non-empty summaries). Otherwise keep asking until the surrogate is usable.

### How to run the interview (interactive, recommended)

Ask **one phase at a time**; summarize back what you captured before moving on.

1. **Phase A — Framing**  
   - What is the **title**?  
   - **Purpose** (why)?  
   - **Scope** (who/what/when; out of scope)?  
   - **Type**: procedural / knowledge / hybrid — explain the three in one line if the user is unsure.

2. **Phase B — Roles & systems**  
   - Who is involved (**roles**)?  
   - Which **systems/tools** (for step `system` fields)?

3. **Phase C — Main body**  
   - If **procedural/hybrid**: walk through **steps** in order (“What happens first? Then?”). For each step capture actor, action, system, output, notes.  
   - If **knowledge**: collect **knowledge_items** as separate bullets; add **decision_points** where “if/then” matters.

4. **Phase D — Edge layer**  
   - **Exceptions** and **decision_points** not yet captured.  
   - **References** (policy numbers, URLs, form names).

5. **Phase E — Review**  
   - Show the filled **Markdown assembly** (template below). Ask: “Anything wrong or missing?” One correction round, then proceed to **`parse_sop_raw_text`** on the final string.

### Markdown assembly template (paste answers into this, then `parse_sop_raw_text`)

Use this skeleton so the LLM extractor sees familiar headings. Replace placeholders; remove empty optional blocks if truly unused.

```markdown
# SOP: {{title}}

## Purpose
{{purpose}}

## Scope
{{scope}}

## Type
{{sop_type}}

## Roles
{{bullet_list_roles}}

## Workflow / Steps
{{for each step:}}
### Step {{step_number}} — {{actor}}
- **Action:** {{action}}
- **System / tool:** {{system}}
- **Output / evidence:** {{output}}
- **Notes:** {{notes}}

## Knowledge / rules (non-step)
{{bullet_list_knowledge_items}}

## Decision points
{{bullet_list_decision_points}}

## Exceptions & escalations
{{bullet_list_exceptions}}

## References
{{bullet_list_references}}
```

After assembly, set `source_label` in extraction meta if your caller supports it (e.g. `interview-fallback-v1`).

### Optional JSON skeleton (for tooling or hand-off)

If another tool consumes JSON before markdown assembly, shape must match **`SOPStructure`** fields:

```json
{
  "title": "",
  "purpose": "",
  "scope": "",
  "sop_type": "procedural | knowledge | hybrid",
  "roles": [],
  "steps": [
    {
      "step_number": 1,
      "actor": "",
      "action": "",
      "system": "",
      "output": "",
      "notes": ""
    }
  ],
  "knowledge_items": [],
  "sections": [],
  "decision_points": [],
  "exceptions": [],
  "references": [],
  "raw_text": ""
}
```

Populate `raw_text` with the same content as the **Markdown assembly** when serializing for storage.

---

## Part 3 — User gives no input, or cannot provide information

Use this when: the user **never picks** (1)–(4), **stops replying**, says **“I don’t know”** / **“I can’t share that”** for many fields, or **does not have** access to the real SOP.

### Principles

- **Do not invent** confidential or precise operational detail to fill gaps. Prefer **explicit unknowns** over fake specificity.
- **Align with** [generator-worker-spec.md](./generator-worker-spec.md): for non-SOP / thin context, the default is **draft with reasonable assumptions** and state them clearly — **unless** the gap is **blocking** or **unsafe** (see below).

### A. No response after the recovery menu

1. Send **one** short follow-up: restate that you still need one of (1)–(4), and that **(4)** includes “draft from chat only” if they have no file.
2. If there is still **no substantive reply** (same session / product policy permitting):  
   - **If the thread already contains enough intent** (goal, audience, constraints): proceed with **intent-only** skill drafting per **generator-worker-spec** (“non-SOP skills”), and label limitations in the generated skill (what was **not** verified against a document).  
   - **If there is not enough to draft anything useful:** stop with a **clear, polite closure** — what is missing (e.g. “a path, pasted text, URL, or agreement to draft from chat”) and that they can resume anytime.

Do **not** loop endless reminders in the same session unless the product explicitly wants that.

### B. Interview started but the user cannot answer some questions

- For **missing non-critical** fields (e.g. exact policy ID, secondary system name): insert **`[TBD: …]`** or a one-line **assumption** in the markdown assembly, and mirror that in the generated **`SKILL.md`** (“Assumptions / unknowns”) so reviewers can fix it later.
- For **“I don’t know”** on a whole area: **shrink scope** in `scope` (“Applies only to what we confirmed: …”) and continue if the **quality gate** (Part 2) can still be met with steps or knowledge items from what *was* confirmed.
- If the **quality gate cannot be met** even with TBDs: **do not** call extraction on empty fluff — either **narrow the ask** (one more focused question) or switch to **intent-only** skill with a **narrower** description and explicit “partial capture” wording.

### C. User cannot provide *any* substantive SOP-like material

(e.g. no access, policy forbids export, greenfield idea only.)

- **Default:** **intent-only** skill from chat + stated constraints, per **generator-worker-spec** (“Interview and research (for non-SOP skills)”). No surrogate markdown pretending to be a full SOP unless the user **opts in** to a **minimal** interview (title + purpose + one workflow sentence).
- **Do not** fabricate a long procedural SOP to pass **`parse_sop_raw_text`** when the user has not supplied or confirmed that content.

### D. Blocking or high-risk gaps

If missing information makes a **safe or compliant** skill impossible (e.g. regulated workflow, irreversible actions, no actor or approval path), **do not** ship a confident procedure. Options:

- **Stop** and name the **minimum** fact needed to continue, or  
- Produce a **stub / checklist skill** that only lists **questions to resolve** and **no** automated procedure until the user fills in — still useful, and honest.

---

## After recovery or fallback

Continue the **normal** skill-gen flow from [SKILL.md](../SKILL.md): structured extraction (unless you intentionally skip with a host-approved fast path), draft under `skills-draft/`, **`skills.import_local`**.

---

## Provenance: what was generated, and can the user tell?

### What the pipeline produces

- The **main deliverable** is an **installed skill** (`SKILL.md` + optional `reference/`, etc.), not a canonical “new SOP” file for the organization.  
- Internally, **`SOPStructure`** (JSON-shaped data) is produced by **`parse_sop_file` / `parse_sop_raw_text`** from **plain text** — that text may be a **real document**, **fetched HTML text**, **a paste**, or **markdown assembled from the interview** (surrogate).  
- A **separate user-visible SOP file** is **not** created by default. If you want one (e.g. `reference/captured-sop.md`), create it **explicitly** as part of the draft package and point to it from `SKILL.md`.

### Can the user tell real vs recovery vs fallback?

**In the skill file:** yes, when validation is used. Run **`skill_generator_cli.py validate-skill --skill-dir …`** (or **`skill_gen.validator.validate_skill`**) before import; it **requires** the section and a known **`source:<tag>`** (see `skill_gen.provenance.SKILL_PROVENANCE_SOURCES`). To build the block in code, use **`skill_gen.provenance.render_provenance_section()`**.

**Normative (generator):** Every skill produced through this meta-skill **must** include **`## Source and provenance`** in the **generated** `SKILL.md` body (after frontmatter), with:

1. **Exactly one** primary line **`source:<tag>`** chosen from the table below.  
2. **Immediately below**, at least **one plain-language sentence** (validated: ≥20 characters of prose after ignoring machine lines). A **non-technical reader** should understand **whether this skill mirrors a real document** or came from **paste, URL, guided Q&A, or chat only** — without having to decode `source:*`. Use **`render_provenance_section(..., detail="…")`** for the sentence(s).

**Optional machine lines** (do not count as the user summary): `interview_version:…`, `prior_attempts:…`.

| Tag | When to use (technical) | Example plain-language sentence for **`detail=`** (adapt to facts) |
|-----|-------------------------|----------------------------------------------------------------------|
| **`source:document_file`** | Plain text from a **local file** read successfully. | “This skill was generated from the SOP file `onboarding.md` on disk, then run through the normal extractor.” |
| **`source:document_url`** | Plain text from a **fetched page**. | “This skill was generated from the policy text fetched from the URL you provided (not from an internal file share).” |
| **`source:document_paste`** | Plain text **pasted** in chat. | “This skill was generated from SOP text you pasted into the chat (not from a file path or link).” |
| **`source:interview_surrogate`** | **Part 2** interview + assembled markdown → extraction. | “No full SOP file was used. The workflow was captured through guided questions and then normalized by the same extractor used for documents.” |
| **`source:intent_only_chat`** | **Part 3** / non-SOP path; **no** document through extraction. | “This skill was drafted from our conversation only; it does not claim to match a published SOP word-for-word.” |
| **`source:packaged_reference`** | Pre-installed template skill. | “This is a bundled reference skill shipped with the workspace, not produced from your documents in this session.” |

If **multiple** sources were tried (e.g. file missing, then paste worked), the **plain-language sentence** should say what **actually fed extraction** (e.g. “The first path failed; the skill reflects pasted text.”). The **`source:`** line still reflects the **primary** input to `parse_sop_raw_text` only. Add **`prior_attempts:`** if useful.

**Optional:** repeat the gist in YAML **`description`** so routing metadata also hints at provenance.

**Validator:** `skill_gen.provenance.verify_skill_md_provenance` rejects a section that has only `source:*` with no readable explanation.

---

## Versioning

This document is **v1**. When you change field constraints or phases, bump a short note at the bottom (`v1 → v2`) so agents can cite which interview version produced a surrogate SOP.

**v1.1** — Added **Part 3** (no response, partial answers, cannot provide material, blocking gaps).  
**v1.2** — Added **Provenance** (what is generated + required `## Source and provenance` on generated skills).  
**v1.3** — Provenance enforced by **`skill_gen.validator`** + **`validate-skill`** CLI; added **`source:packaged_reference`** and **`render_provenance_section()`**.  
**v1.4** — Required **plain-language summary** under provenance (end-user clarity); example sentences per `source:*` tag.
