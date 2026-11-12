# Community Skill Search Guide

How to find, evaluate, and install community skills for Swarm Skill roles. Read this file when executing the Post-generation community enrichment step.

## 1. Keyword Derivation

The #1 failure mode is bad search queries. Do NOT freestyle keywords — derive them systematically from the role spec.

### Extraction sources (in priority order)

For each role that needs a community skill, read these fields and extract terms:

1. **`purpose` field** (SKILL.md frontmatter) — the most compressed description of what the role does. Extract the **verb** (generate, parse, render, analyze, review, audit) and the **object** (diagram, pptx, security, performance).
2. **`Success Criteria`** (roles/\<id\>.md) — look for deliverable types and quality dimensions. Example: "Produces an Excalidraw JSON diagram" → search terms: `excalidraw`, `diagram`.
3. **`Output Schema`** (roles/\<id\>.md) — if the output is a specific file format, that format IS the search term. Example: output is `.pptx` → search `pptx` or `presentation`.

### Query formulation rules

- **Structure**: `<capability-verb> <domain-noun>` — e.g., `generate pptx`, `review security`, `parse csv`.
- **Always generate 2–3 query variants** per role. Use synonyms and specificity levels:
  - Specific: `excalidraw diagram`
  - Broader: `diagram generation`
  - Domain: `architecture visualization`
- **English only** — all major registries index in English. Translate Chinese terms before searching.
- **Avoid generic terms alone** — `code`, `review`, `write` are too broad. Always pair with a domain noun.
- **Include output format when applicable** — `pptx`, `docx`, `xlsx`, `pdf`, `svg`, `mermaid` are high-signal terms that dramatically narrow results.

### Worked example

Role: a "diagram-renderer" whose purpose is "Render architecture diagrams as Excalidraw JSON files."

| Source | Extracted terms |
|---|---|
| purpose | render, architecture, diagram, excalidraw, JSON |
| Success Criteria | "valid Excalidraw JSON", "auto-layout" |
| Output Schema | `.excalidraw` file |

Generated queries:
1. `excalidraw diagram` (specific)
2. `architecture diagram` (broader)
3. `canvas design` (alternative — Excalidraw skills sometimes use "canvas" in their name)

## 2. Multi-Source Search Strategy

Do NOT rely on a single registry. Use the two CLI sources **in parallel**, then cross-verify on web platforms.

### Tier 1: CLI Search (parallel — run both)

| Source | CLI | Search command | Install command | Strength |
|---|---|---|---|---|
| **skills.sh** | `npx skills` (Node) | `npx skills find '<query>'` | `npx skills add <owner/repo@skill> -g -y` | Largest install-count dataset, security scores |
| **SkillNet** | `skillnet` (Python: `pip install skillnet-ai`) | `skillnet search '<query>' --mode vector` | `skillnet download <url> -d ./skills` | **Semantic vector search** — finds results even when keywords don't exactly match |

**Why both**: skills.sh uses keyword matching and may miss relevant skills when your query terms don't match the skill's title/description verbatim. SkillNet uses vector similarity and finds semantically related skills even with imperfect queries. They index overlapping but not identical skill sets.

**Windows note**: on Windows, wrap `npx` commands in `powershell -Command "..."` to avoid empty output. `skillnet` (Python) works natively in any terminal.

### Tier 2: Web Verification + Broader Search

After CLI search returns candidates, or if CLI results are poor, use these web platforms:

| Source | URL | Use for |
|---|---|---|
| **LLMBase** | [llmbase.ai/skills/](https://llmbase.ai/skills/) | Verify install counts + security ratings (Safe/Medium/Critical); Top Sources ranking |
| **SkillsMP** | [skillsmp.com](https://skillsmp.com) | Broadest GitHub index (425K+); AI-powered search; category filtering |
| **LobeHub** | [lobehub.com/skills](https://lobehub.com/skills) | Community feedback; alternative categorization |
| **SkillNet Web** | [skillnet.openkg.cn](http://skillnet.openkg.cn/) | Browse curated collections; skill relationship graph |
| **skills.sh Leaderboard** | [skills.sh](https://skills.sh/) | Quick scan of top-installed skills by category |

### Tier 3: Curated Lists (high signal-to-noise fallback)

When CLI + web search return no good matches, browse these human-curated lists:

| Source | URL | Coverage |
|---|---|---|
| **awesome-agent-skills** | [github.com/heilcheng/awesome-agent-skills](https://github.com/heilcheng/awesome-agent-skills) | Cross-agent (Claude, Cursor, Copilot), tutorials + directories |
| **awesome-claude-skills** | [github.com/ComposioHQ/awesome-claude-skills](https://github.com/ComposioHQ/awesome-claude-skills) | Claude ecosystem deep-dive |
| **Antigravity awesome-skills** | [github.com/antigravity-ai/awesome-skills](https://github.com/antigravity-ai/awesome-skills) | 1200+ skills organized by **role bundles** (Web Wizard, Security Engineer, etc.) — particularly useful for Swarm Skill roles since the bundles map to role archetypes |

### Search procedure (step by step)

```
1. Derive 2–3 query variants per role (§1 above)
2. Run BOTH Tier 1 CLIs in parallel with each query variant
   - npx skills find '<query>'
   - skillnet search '<query>' --mode vector
3. Collect unique candidates from both sources
4. If < 2 candidates found → try Tier 2 web platforms with the same queries
5. If still < 2 candidates → browse Tier 3 curated lists by role archetype
6. Apply quality gates (§3 below) to all candidates
7. Apply role-fit test (§4 below) to surviving candidates
```

## 3. Quality Gates

Do NOT install a skill just because it appeared in search results. Every candidate MUST pass these gates:

### Gate 1: Install Count

| Threshold | Action |
|---|---|
| ≥ 10K installs | **Strong signal** — use with confidence |
| 1K–10K installs | **Moderate** — acceptable if source is reputable |
| 100–1K installs | **Weak** — only if no better alternative exists AND source passes Gate 2 |
| < 100 installs | **Reject** — too risky for a dependency |

Where to check: skills.sh leaderboard, LLMBase skill page, `npx skills` output (shows install count).

### Gate 2: Source Reputation

Prefer skills from known, maintained sources:

**Trusted sources** (non-exhaustive):
- `vercel-labs/agent-skills` — React, Next.js, web design
- `anthropics/skills` — frontend design, document processing (pdf, pptx)
- `microsoft/azure-skills` — cloud, infrastructure
- `firebase/agent-skills` — Firebase ecosystem
- `supabase/agent-skills` — database, backend
- `coreyhaines31/marketingskills` — marketing, SEO, copywriting
- `remotion-dev/skills` — video generation
- `google-labs-code/stitch-skills` — Google design system
- `expo/skills`, `flutter/skills` — mobile development

**Caution signals**:
- Unknown author with no GitHub profile
- Repository with < 50 stars
- No commits in the last 6 months
- Skill description is vague or generic

### Gate 3: Security Rating

Check the security rating on LLMBase or skills.sh:

| Rating | Action |
|---|---|
| **Safe** | Install freely |
| **Medium** | Acceptable — review the skill content before installing |
| **Critical** | Do NOT install unless explicitly approved by the user. Flag in the enrichment summary |

### Gate 4: Freshness

Check the source repository's last commit date. Skills based on framework best practices become stale quickly.

| Last updated | Action |
|---|---|
| Within 3 months | Current |
| 3–6 months | Acceptable |
| > 6 months | Flag as potentially stale — verify the content still applies |

## 4. Role-Skill Fit Test

After a candidate passes quality gates, evaluate whether it actually fits the role. Ask:

1. **Capability match**: Does this skill provide a capability the role's `Success Criteria` requires? If the skill teaches "React best practices" but the role produces "security audit reports," it's not a fit — even if both mention "code."
2. **Removal test**: If you removed this skill, would the role's output quality **significantly** drop? "Significantly" means: the role would produce noticeably worse deliverables, not just slightly less polished ones. If the answer is "no," don't install.
3. **Overlap test**: Does this skill's capability overlap with another role's assigned skill? Cross-role skill sharing is a code smell — each skill should enhance exactly one role.
4. **Format necessity**: If the role outputs a specific file format (PPTX, XLSX, Excalidraw JSON), the skill MUST handle that format's generation/manipulation. A skill that only provides "guidelines" without generation capability is not sufficient.

## 5. Enrichment Summary Format

After completing the search, present results in this format:

```
## Community Skill Search Results

| Role | Query used | Candidates found | Selected | Install count | Security | Reason |
|---|---|---|---|---|---|---|
| <role-id> | <best query> | <N total> | <skill name or "none"> | <count> | <Safe/Med/Crit> | <1-line fit rationale> |

### Install commands
<for each selected skill, the exact install command>

### Skipped candidates
<for each rejected candidate: name, rejection reason (failed which gate)>
```

This format gives the user full transparency on what was searched, what was found, and why each decision was made.
