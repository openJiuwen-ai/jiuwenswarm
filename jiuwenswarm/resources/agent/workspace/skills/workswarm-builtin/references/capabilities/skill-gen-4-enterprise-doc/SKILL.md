---
name: skill-gen-4-enterprise-doc
description: Turn SOPs, URLs, or stated intent into an installed skill package. Use when the user asks to generate a skill from a document, link, or workflow. Follow SKILL.md, reference/sop-structure-pipeline.md, and reference/generator-worker-spec.md; finish by promoting the draft into the runtime skills directory (see reference/operator-playbook.md) in the same turn.
---

# Skill Generator (enterprise doc → installed skill)

Instructions here, **`reference/`**, and **`scripts/`** (package **`scripts/skill_gen/`**). **You** obtain SOP plain text, run **full structured extraction** exactly as in **[reference/sop-structure-pipeline.md](./reference/sop-structure-pipeline.md)**, author the package under **`get_agent_workspace_dir() / "skills-draft"`** (per **[reference/generator-worker-spec.md](./reference/generator-worker-spec.md)**), then **immediately promote** it so the skill is **installed and loadable** (see **[reference/operator-playbook.md](./reference/operator-playbook.md)** canonical flow).

## What you do (high level)

1. **Plain text**
   - **Mandatory sequence when the user wanted a document but you still have no SOP plain text** (wrong path, **file not found**, fetch failed, empty body, permission error, etc.):
     1. **Recovery before fallback** — **Do not** call **`build_intent_fallback_sop`**, **`build_fallback_sop_structure`**, or generate the installable skill from intent-only fallback **in the same assistant turn** as that failure. A missing file is **only** a failed first ingest; it is **not** “no document” yet. Send **exactly one** recovery message first (single reply): offer a **corrected absolute path**, **URL**, or **paste**—same menu and tone as **[reference/sop-recovery-and-interview-fallback.md](./reference/sop-recovery-and-interview-fallback.md)** Part 1—and **wait** for the user’s next message (or treat recovery as already done if **one** recovery was sent earlier in the thread and the user **declined**, **ignored**, or **supplied** material afterward).
     2. **Fallback only after recovery** — Only when recovery is **complete** (user gave path/URL/paste you can use, or declined/ignored after the **one** ask) **and** there is **still** no parseable SOP body, proceed to **`build_intent_fallback_sop`** / **`build_fallback_sop_structure`** below. **Do not** skip step (1) because you “already know” the path failed.
   - **Local file** → `python3 <ABS>/scripts/skill_generator_cli.py sop-text --sop-file <abs-path> [--out-text <path>]` or read the file in-tool; optional `--print-raw-chars` for length.
   - **HTTP(S) / WeChat** → `url-fetch --url '…' [--out-json <path>]` then use page `text` as the SOP body for extraction.
   - **Pasted only** — use the same string as input to **`parse_sop_raw_text`** (no skipping structured extraction).
   - **No document after recovery** — you **must** build the canonical object in code. **Preferred (one call):** **`await skill_gen.sop_fallback.build_intent_fallback_sop(user_intent=…, skill_name_hint=…, invoke_llm_json=…)`** — **default:** pass **`invoke_llm_json`** whenever the runtime can invoke the model: deterministic skeleton **then** internal LLM refine (**`enrich_fallback_sop_with_llm`**). **Only if** the system **cannot** call a model for this step, pass **`invoke_llm_json=None`** (skeleton only) or use **`skill_gen.sop_fallback.build_fallback_sop_structure`** on sync-only paths (same shape; no enrich). Same **`SOPStructure`** fields as extraction (title, purpose, scope, roles, steps, `knowledge_items`, sections, decision points, exceptions, references, plus synthesized **`raw_text`**). Do **not** call **`parse_sop_raw_text`** on empty text; do **not** hand-write a “representative” or illustrative SOP in prose instead of running the fallback—downstream drafting depends on the real **`SOPStructure`** and **`extraction_meta`**. Treat **`extraction_meta["fallback_sop"]`** as “template only” in the generated skill body. Still **no** real SOP after LLM polish: keep **`fallback_sop`** semantics and do not treat output as audited policy.
2. **Structured SOP (required)** → Either **`skill_gen.sop_parser.parse_sop_file`** / **`parse_sop_raw_text`** with **`invoke_llm_json`** when you have SOP plain text, **or** when you do not: **`await skill_gen.sop_fallback.build_intent_fallback_sop(..., invoke_llm_json=…)`** with **`invoke_llm_json`** whenever the runtime can invoke the model (default: skeleton + internal enrich); **`invoke_llm_json=None`** or **`build_fallback_sop_structure`** only when the system cannot (single path; **sop-structure-pipeline.md**).
3. **Draft package** → under **`get_agent_workspace_dir() / "skills-draft" / <skill_name>`** (when the host exposes that helper) or the equivalent **agent-workspace** path from the system prompt: **`SKILL.md`** (YAML frontmatter first) plus optional **`reference/`**, per **generator-worker-spec.md**. Create **`skills-draft`** next to the runtime **`skills/`** folder if it does not exist.
   - **Intent-only snapshot (required when fallback)** — If **`extraction_meta["fallback_sop"]`** is true (intent-only / template path), you **must** add **`reference/intent-sop-snapshot.md`** in that draft folder. Start with a short warning block (this is **not** an authoritative enterprise SOP; synthesized from stated intent and optional LLM refine; provisional until a real document is attached). Then include the verbatim **`SOPStructure.raw_text`** from the structured object you used for drafting so reviewers can see exactly what the fallback produced.
4. **Install in the same workflow** → Call **`skills.import_local`** with **`path`** = **absolute** path to that draft directory (folder containing `SKILL.md`). Use **`force: true`** if **`get_agent_skills_dir() / <skill_name>`** already exists and should be replaced. Do **not** stop after step 3 and ask the user to import manually; promotion is **part of this skill’s default completion**.

After step 4, the new skill lives under **`get_agent_skills_dir() / <skill_name>`** (same directory tree the runtime loads user-installed skills from) and is available like any other installed skill.

Do not silently shrink **generator-worker-spec** when writing **SKILL.md**.

## Invoking `skill_generator_cli.py` safely

- Always use the **absolute path** to `scripts/skill_generator_cli.py`.
- **`httpx`** / **`beautifulsoup4`** for `url-fetch`. Rich file formats need **openjiuwen** `AutoFileParser` when installed; otherwise use `.md` / `.txt` SOPs.

Path tables and **`skills.import_local`**: **[reference/operator-playbook.md](./reference/operator-playbook.md)**.

## Normative `SKILL.md` format (YAML frontmatter first)

The generated skill’s **`SKILL.md`** must start with **`---`** YAML frontmatter, then **`---`**, then the body. Optional: **`scripts/skill_gen/validator.py`** on the draft path before import.

## When to open reference files

| File | When |
|------|------|
| [reference/sop-structure-pipeline.md](./reference/sop-structure-pipeline.md) | **Always**: `SOPStructure` extraction. |
| [reference/sop-recovery-and-interview-fallback.md](./reference/sop-recovery-and-interview-fallback.md) | **Always** before intent-only fallback: recovery menu wording and ordering (Part 1). |
| [reference/generator-worker-spec.md](./reference/generator-worker-spec.md) | **Always**: how to write a high-quality generated skill — **Writing a high-quality `SKILL.md`** (required structure) plus **Skill writing guide**. |
| [reference/operator-playbook.md](./reference/operator-playbook.md) | CLI, paths, **`skills.import_local`**, canonical flow, directory table, `SKILL.md` frontmatter rules. |

## Human approval gates

Do **not** modify this meta-skill’s **`SKILL.md`**, **`reference/sop-structure-pipeline.md`**, **`reference/generator-worker-spec.md`**, **`reference/operator-playbook.md`**, or other bundled **`reference/*.md`** while generating a **user** skill. Edit only the **target** skill under the workspace-relative **`skills-draft/<name>/`** staging tree or the installed **`skills/<name>/`** tree (resolve with **`get_agent_workspace_dir()`** / **`get_agent_skills_dir()`** when available).

## Communicating with the user

- Prefer plain language for skill names and outcomes.
- Infer **`skill_name`** (kebab-case) from title or URL when the user did not specify.
