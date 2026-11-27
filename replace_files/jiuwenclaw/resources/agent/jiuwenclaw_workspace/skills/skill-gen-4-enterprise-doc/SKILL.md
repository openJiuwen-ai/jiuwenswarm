---
name: skill-gen-4-enterprise-doc
description: Turn SOPs, URLs, or stated intent into an installed skill package. Use when the user asks to generate a skill from a document, link, or workflow. Follow SKILL.md, reference/sop-structure-pipeline.md, and reference/generator-worker-spec.md; finish by promoting the draft into the runtime skills directory (see reference/operator-playbook.md) in the same turn.
---

# Skill Generator (enterprise doc → installed skill)

Instructions here, **`reference/`**, and **`scripts/`** (package **`scripts/skill_gen/`**). **You** obtain SOP plain text, run **full structured extraction** exactly as in **[reference/sop-structure-pipeline.md](./reference/sop-structure-pipeline.md)**, author the package under **`get_agent_workspace_dir() / "skills-draft"`** (per **[reference/generator-worker-spec.md](./reference/generator-worker-spec.md)**), then **immediately promote** it so the skill is **installed and loadable** (see **[reference/operator-playbook.md](./reference/operator-playbook.md)** canonical flow).

## What you do (high level)

1. **Plain text**
   - **Local file** → `python3 <ABS>/scripts/skill_generator_cli.py sop-text --sop-file <abs-path> [--out-text <path>]` or read the file in-tool; optional `--print-raw-chars` for length.
   - **HTTP(S) / WeChat** → `url-fetch --url '…' [--out-json <path>]` then use page `text` as the SOP body for extraction.
   - **Pasted only** — use the same string as input to **`parse_sop_raw_text`** (no skipping structured extraction).
2. **Structured SOP (required)** → **`skill_gen.sop_parser.parse_sop_file`** or **`parse_sop_raw_text`** with **`invoke_llm_json`** (single path; **sop-structure-pipeline.md**).
3. **Draft package** → under **`get_agent_workspace_dir() / "skills-draft" / <skill_name>`** (when the host exposes that helper) or the equivalent **agent-workspace** path from the system prompt: **`SKILL.md`** (YAML frontmatter first) plus optional **`reference/`**, per **generator-worker-spec.md**. Create **`skills-draft`** next to the runtime **`skills/`** folder if it does not exist.
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
| [reference/generator-worker-spec.md](./reference/generator-worker-spec.md) | **Always**: how to write a high-quality generated skill — **Writing a high-quality `SKILL.md`** (required structure) plus **Skill writing guide**. |
| [reference/operator-playbook.md](./reference/operator-playbook.md) | CLI, paths, **`skills.import_local`**, canonical flow, directory table, `SKILL.md` frontmatter rules. |

## Human approval gates

Do **not** modify this meta-skill’s **`SKILL.md`**, **`reference/sop-structure-pipeline.md`**, **`reference/generator-worker-spec.md`**, **`reference/operator-playbook.md`**, or other bundled **`reference/*.md`** while generating a **user** skill. Edit only the **target** skill under the workspace-relative **`skills-draft/<name>/`** staging tree or the installed **`skills/<name>/`** tree (resolve with **`get_agent_workspace_dir()`** / **`get_agent_skills_dir()`** when available).

## Communicating with the user

- Prefer plain language for skill names and outcomes.
- Infer **`skill_name`** (kebab-case) from title or URL when the user did not specify.
