# SOP → structured JSON (single path)

There is **one** supported way to turn SOP plain text into a full **`SOPStructure`** (rich `steps`, `knowledge_items`, `sections`, `sop_type`, decision points, exceptions, references, etc.):

Call **`skill_gen.sop_parser.parse_sop_file`** or **`parse_sop_raw_text`** and pass a required **`invoke_llm_json`** callable. The implementation chooses single-shot vs chunked map-reduce and optional reconcile using **`parse_options`** (`sop_parse_mode`, context limits, chunk sizes—see the functions’ docstrings in `scripts/skill_gen/sop_parser.py`).

**Authoritative extraction prompts** (what the model must return, field semantics, step vs knowledge rules) live **only** in:

- `scripts/skill_gen/sop_parser.py` — full-document extraction template  
- `scripts/skill_gen/sop_chunk_merge.py` — per-chunk and reconcile templates  

Do not maintain a second, competing extraction spec elsewhere.

**Shell helpers:** `scripts/skill_generator_cli.py sop-text` outputs **plain text** extracted from a file (character count or full text). It does **not** emit `SOPStructure` JSON. For HTTP(S) or WeChat pages, use **`url-fetch`**, then run the same **`parse_sop_raw_text`** on the fetched body with **`invoke_llm_json`**.

**No plain-text SOP (intent-only):** If there is no body to parse, follow **[SKILL.md](../SKILL.md)** step 1 **mandatory sequence**: complete **recovery** (one offer of path / URL / paste per **[sop-recovery-and-interview-fallback.md](./sop-recovery-and-interview-fallback.md)** Part 1) **before** calling **`build_intent_fallback_sop`** / **`build_fallback_sop_structure`**—a missing or unreadable file **does not** skip recovery. After recovery, if there is **still** no body, you **must** materialize the fallback in code—do **not** substitute a hand-authored “preview” SOP. **Preferred:** **`await skill_gen.sop_fallback.build_intent_fallback_sop(user_intent=…, skill_name_hint=…, invoke_llm_json=…)`** — **default:** pass **`invoke_llm_json`** whenever the runtime can invoke the model (deterministic skeleton, then **`enrich_fallback_sop_with_llm`** internally). Pass **`invoke_llm_json=None`** only when the system **cannot** call a model for this step, or use **`build_fallback_sop_structure`** on sync-only paths (no LLM). That path returns full **`SOPStructure`** and Markdown **`raw_text`** with the same structural elements as LLM extraction (purpose, scope, roles, procedure steps, rules slot, sections, branches, exceptions, references). It is **not** a substitute for a real policy document—generated skills must say assumptions are provisional until source material arrives.

**Advanced:** **`enrich_fallback_sop_with_llm`** remains for custom pipelines; prefer **`build_intent_fallback_sop`** for the normal intent-only flow.

**Draft artifact:** When the structured path used **`fallback_sop`**, the generated skill package **must** include **`reference/intent-sop-snapshot.md`** (warning + verbatim **`raw_text`**) per **[generator-worker-spec.md](./generator-worker-spec.md)**.

**After `SOPStructure` exists:** Write the target skill’s **`SKILL.md`** (user inputs, processing steps, deliverables, triggers) solely per **[generator-worker-spec.md](./generator-worker-spec.md)** under **`get_agent_workspace_dir() / "skills-draft" / <skill_name>`** (or the host’s equivalent), then in the **same workflow** call **`skills.import_local`** with the **absolute** draft directory path so the package is copied to **`get_agent_skills_dir() / <skill_name>`** and is immediately loadable ([operator-playbook.md](./operator-playbook.md) **Canonical flow**). Use **`force: true`** when overwriting an existing installed skill.
