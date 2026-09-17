# Skill Generator Operator Playbook (scripts + main agent)

**`scripts/skill_generator_cli.py`** prepends **`scripts/`** to `sys.path` and imports **`skill_gen`** from **`scripts/skill_gen/`**. Those CLI entrypoints do not import the **`jiuwenswarm`** package.

## Commands (always absolute script path)

```bash
python3 /ABS/…/skill-gen-4-enterprise-doc/scripts/skill_generator_cli.py sop-text \
  --sop-file /ABS/path/SOP.md [--out-text /ABS/out.txt] [--print-raw-chars]

python3 /ABS/…/skill-gen-4-enterprise-doc/scripts/skill_generator_cli.py url-fetch \
  --url 'https://…' [--out-json /ABS/pages.json]
```

- **`sop-text --print-raw-chars`**: prints extracted character count only.
- **`sop-text` without `--out-text`**: prints full UTF-8 text to stdout.
- **`url-fetch`**: JSON array of pages (`text`, `title`, `source_url`, …).

Full **`SOPStructure`** JSON is produced only via **`parse_sop_file` / `parse_sop_raw_text` + `invoke_llm_json`** in the agent runtime ([sop-structure-pipeline.md](./sop-structure-pipeline.md)).

## Install the new skill (same session as draft)

After writing the draft tree with a valid **`SKILL.md`** under **`get_agent_workspace_dir() / "skills-draft" / <skill_name>`** (or the host’s equivalent **agent-workspace** path), **immediately** promote it:

- **RPC:** **`skills.import_local`** with **`{ "path": "<absolute path to that draft folder>" }`**. Add **`"force": true`** if **`get_agent_skills_dir() / <skill_name>`** already exists and should be replaced.
- Result: tree copied to **`get_agent_skills_dir() / <skill_name>`** and skill indexes refreshed — the skill is **ready to use** without a separate user-facing “Import” step.

This replaces a manual **apply** / **Import** handoff: the driving agent performs import as part of completing the SOP→skill task.

## Path resolution (portable)

Do **not** assume a fixed home-directory prefix or legacy **`…/workspace/…`** segments. JiuwenSwarm (and forks) may relocate data roots via config or environment; always prefer **whatever path helpers the host exposes** in the system prompt or tool context.

When **`get_agent_workspace_dir()`** / **`get_agent_skills_dir()`** / **`get_agent_root_dir()`** are named in the prompt, use them to build **absolute** paths for drafts, imports, and file tools. If only a virtual tree (e.g. `agent/workspace/...`) is shown, map it through the host’s documented root before writing to disk.

- **Draft staging:** **`skills-draft/<skill_name>/`** lives **under** the agent workspace directory (sibling to the runtime **`skills/`** folder), i.e. **`get_agent_workspace_dir() / "skills-draft" / <skill_name>`**. Create **`skills-draft`** if missing.
- **Installed skills:** user-installed packages land under **`get_agent_skills_dir() / <skill_name>`** (in stock JiuwenSwarm this is **…/workspace/agent/workspace/skills/<skill_name>**, not **`…/agent/skills/`**).

## Path conventions (typical product layout)

Use **`get_agent_root_dir()`**, **`get_agent_workspace_dir()`**, **`get_agent_skills_dir()`** from the system prompt when available.

| Location | Role |
|----------|------|
| **`get_agent_skills_dir() / "skill-gen-4-enterprise-doc"`** | This meta-skill as **installed** (or the built-in copy resolved via **`get_builtin_skills_dir()`** — treat as read-only unless the product explicitly allows edits). |
| **`get_agent_workspace_dir() / "skills-draft" / <skill_name>`** | Where you write the **new** skill tree before **`skills.import_local`**. |
| **`get_agent_skills_dir() / <skill_name>`** | **Installed** target skill after **`skills.import_local`**. |

**`workdir` for bash tools:** If the environment restricts subprocess cwd, invoke **`python3`** with absolute paths without relying on `cd`.

## SOP files

- Any **absolute** path readable at runtime is fine.
- Without **openjiuwen** `AutoFileParser`, prefer **`.md` / `.txt`**; binary office/PDF needs that dependency.

## After extraction: drafting

1. Obtain **`SOPStructure`** per **[sop-structure-pipeline.md](./sop-structure-pipeline.md)**.
2. Produce **`SKILL.md`** per **[generator-worker-spec.md](./generator-worker-spec.md)** under **`get_agent_workspace_dir() / "skills-draft" / <skill_name>`** (create **`skills-draft`** if needed). If **`extraction_meta["fallback_sop"]`**, also write **`reference/intent-sop-snapshot.md`** (see **generator-worker-spec** — *Intent-only snapshot file*).
3. Run **`skills.import_local`** as above.

---

# Paths and canonical flow

This section is the same document as **# Skill Generator Operator Playbook** above the horizontal rule (CLI, **`skills.import_local`**, path helpers, drafting). Rules for generated **`SKILL.md`** text and for LLM extraction live in **[generator-worker-spec.md](./generator-worker-spec.md)** and **[sop-structure-pipeline.md](./sop-structure-pipeline.md)**.

## Canonical flow (single pipeline)

1. **SOP plain text** — local file (`sop-text` / read in-tool), URL body (`url-fetch`), or pasted text in chat.
2. **Full `SOPStructure`** — **`parse_sop_file` / `parse_sop_raw_text` + `invoke_llm_json`** only ([sop-structure-pipeline.md](./sop-structure-pipeline.md)).
3. **Draft package** — **`SKILL.md`** (and optional `reference/`, `evals.json`, …) under **`get_agent_workspace_dir() / "skills-draft" / <skill_name>`** ([generator-worker-spec.md](./generator-worker-spec.md)). When intent-only fallback was used, include **`reference/intent-sop-snapshot.md`**.
4. **Promote in the same workflow** — **`skills.import_local`** with **`path`** = **absolute** path to the draft directory (folder containing `SKILL.md`). Copies to **`get_agent_skills_dir() / <skill_name>`** (same root the runtime uses for user-installed skills) and refreshes indexes. Use **`force: true`** to replace an existing installed skill of the same name.

Optionally run **`scripts/skill_gen/validator.py`** on the draft directory before **`skills.import_local`**.

## Directory roles

| Path | Role |
|------|------|
| **`get_agent_skills_dir() / <name>`** (conceptually **`<agent_workspace>/skills/<name>/`**) | Installed skills (`SKILL.md`, optional `reference/`, `scripts/`, …). Runtime loads user-installed trees from here. |
| **`get_agent_workspace_dir() / "skills-draft" / <skill_name>`** | Staging: write the new package here, then **`skills.import_local`**. |
| **`jiuwenswarm/.../workspace/skills/skill-gen-4-enterprise-doc/`** (repo) | Built-in meta-skill template (`get_builtin_skills_dir()`). |
| **`skill-gen-4-enterprise-doc/scripts/`** | `skill_generator_cli.py` (`sop-text`, `url-fetch`); **`scripts/skill_gen/`** — Python extraction (`parse_sop_*`, prompts in `sop_parser.py`, `sop_chunk_merge.py`). |

**`with_skill`–style evaluation** (if used) loads skills from the runtime **skills root** (**`get_agent_skills_dir()`** or the host-documented equivalent) only — promote before expecting the new skill to participate.

## Generated `SKILL.md` — frontmatter

| Rule | Detail |
|------|--------|
| **First line** | `---` (start YAML frontmatter). |
| **Required keys** | `name` (kebab-case), `description` (no `<` / `>` in text). |
| **Then** | Closing `---`, then Markdown body (inputs, processing, deliverables, …). |

Same layout as other Jiuwenswarm skills; **`validate_skill`** in `scripts/skill_gen/validator.py` checks frontmatter and basic structure.
