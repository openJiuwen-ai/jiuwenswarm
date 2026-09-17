import type { CommandContext } from "../types.js";

export type ScopeKey = "project" | "personal" | "both";

export interface ExistingFiles {
  jiuwenswarmMd: boolean;
  jiuwenswarmLocalMd: boolean;
  claudeMd: boolean;
  claudeLocalMd: boolean;
  agentsMd: boolean;
  openjiuwenMd: boolean;
  cursorRules: boolean;
  copilotInstructions: boolean;
}

export interface BuildInitPromptArgs {
  rootDir: string;
  scopeKey: ScopeKey;
  existing: ExistingFiles;
}

// ---------------------------------------------------------------------------
// Language resolution
// ---------------------------------------------------------------------------

export function resolveLanguage(ctx: CommandContext): "zh" | "en" {
  if (ctx.preferredLanguage === "en" || ctx.preferredLanguage === "zh") {
    return ctx.preferredLanguage;
  }
  const lang =
    typeof process !== "undefined" ? (process.env.LANG ?? "") : "";
  return /^zh/i.test(lang) || /CN$/i.test(lang) ? "zh" : "en";
}

// ---------------------------------------------------------------------------
// Prompt builder
// ---------------------------------------------------------------------------

export function buildInitPrompt(args: BuildInitPromptArgs): string {
  return buildInitPromptEn(args);
}

// ---------------------------------------------------------------------------
// English (authoritative source)
// ---------------------------------------------------------------------------

function buildInitPromptEn({ rootDir, scopeKey, existing }: BuildInitPromptArgs): string {
  const scopeLine = SCOPE_DESCRIPTION_EN[scopeKey];
  return `Set up a minimal JIUWENSWARM.md (team-shared) and optionally JIUWENSWARM.local.md (personal) for this repository.

These files are auto-loaded into every coding-mode session by ProjectMemoryRail, so they must be CONCISE — only include what the assistant would get wrong without them.

## CRITICAL Constraints (read first, do not violate)

1. **All file operations MUST use absolute paths rooted at: \`${rootDir}\`**
   Never use relative paths. When writing or editing, always construct \`${rootDir}/<filename>\`.
2. **Do NOT use the \`coding_memory_read\` / \`coding_memory_write\` / \`coding_memory_edit\` tools in this command.** Those are for session-level auto-memory, a different system. /init produces static project documents via the file write tools only.
3. **Existing files pre-detected in workspace root**:
  - JIUWENSWARM.md: ${yesNo(existing.jiuwenswarmMd)} ${existing.jiuwenswarmMd ? "— you MUST read it first, propose a diff, then use `ask_user` with `questions` to ask the user whether to apply. Example: `ask_user(query='Update JIUWENSWARM.md?', questions=[{question: 'JIUWENSWARM.md already exists. What would you like to do?', header: 'Update', options: [{label: 'Apply update', description: 'Merge the proposed changes into the existing file'}, {label: 'Skip (keep current)', description: 'Leave the file unchanged and continue'}], multi_select: false}])`. If user chooses 'Apply update', use Edit to apply the diff; if 'Skip', leave the file unchanged and continue. NEVER silently overwrite." : ""}
   - JIUWENSWARM.local.md: ${yesNo(existing.jiuwenswarmLocalMd)} ${existing.jiuwenswarmLocalMd ? "— propose additions via Edit only, never overwrite." : ""}
   - Legacy reference files (do NOT delete or rewrite; you may link to them): CLAUDE.md=${yesNo(existing.claudeMd)}, CLAUDE.local.md=${yesNo(existing.claudeLocalMd)}, AGENTS.md=${yesNo(existing.agentsMd)}, OPENJIUWEN.md=${yesNo(existing.openjiuwenMd)}, .cursorrules=${yesNo(existing.cursorRules)}, .github/copilot-instructions.md=${yesNo(existing.copilotInstructions)}
4. **When the explore sub-agent runs bash commands**, always prefix with \`cd ${rootDir} && ...\` or use \`git -C ${rootDir}\` — sub-agent CWD is not guaranteed to equal \`${rootDir}\`.
5. **Always prefer \`task_tool\` with \`subagent_type: "explore_agent"\` when it is available.** If \`task_tool\` is unavailable for this turn, silently FALL BACK to \`glob\` / \`grep\` / \`read_file\` / \`bash\` yourself.
6. **Default to a single \`task_tool\` / \`explore_agent\` call.** If the repository is clearly large, a monorepo, or one pass does not gather enough signal, you may split the work across multiple explore sub-agents; only parallelize when there is a clear benefit, to avoid duplicate scanning and noisy result merging.

## Step 1: Scope (already answered)

User chose: **${scopeKey}** — ${scopeLine}

## Step 2: Explore the codebase

Preferred path — invoke \`task_tool\` with:
\`\`\`
subagent_type: "explore_agent"
task_description: |
  Thoroughly explore the repository at ${rootDir}. Use "very thorough" exploration.
  Read these key files if present (use absolute paths):
    - Manifests: package.json, Cargo.toml, pyproject.toml, go.mod, pom.xml, build.gradle*, setup.py
    - Docs: README.*, CONTRIBUTING.*, ARCHITECTURE.*, docs/
    - Build/CI: Makefile, justfile, .github/workflows/*, .gitlab-ci.yml, azure-pipelines.yml
    - AI tool configs: JIUWENSWARM.md, CLAUDE.md, AGENTS.md, OPENJIUWEN.md,
                       .jiuwen/rules/*, .claude/rules/*, .cursor/rules/*,
                       .cursorrules, .github/copilot-instructions.md,
                       .windsurfrules, .clinerules, .mcp.json
    - Config: .jiuwen/settings*.json (read-only; do not rewrite)
  Detect and report back concisely:
    - Build / test / lint / format commands (especially non-standard ones)
    - Primary languages, frameworks, package manager
    - Project structure (monorepo, multi-module, single-package)
    - Code style rules differing from language defaults
    - Non-obvious gotchas, required env vars, workflow quirks
    - Branch / PR / commit message conventions
    - Run \`git -C ${rootDir} worktree list\` and mention if multiple worktrees exist
  Note anything you CANNOT figure out from code alone — these become interview questions.
\`\`\`

Fallback (no task_tool): do the same yourself with \`glob\` and \`read_file\`; focus on the manifest + README first, then Makefile / CI configs.

## Step 3: Fill gaps + build proposal

Gather info code can't answer. Use the \`ask_user\` tool with structured \`questions\` parameter.

The \`ask_user\` tool supports a \`questions\` parameter for presenting selectable options:
\`\`\`
ask_user(
  query="Brief description of what you're asking",
  questions=[
    {
      question: "The full question text",
      header: "ShortTag",
      options: [
        {label: "Option A", description: "What option A means"},
        {label: "Option B", description: "What option B means"},
      ],
      multi_select: false,
    }
  ]
)
\`\`\`

Use selectable options when they help clarify the question, or ask open-ended questions to gather free-form input. The user can always choose "Other" for custom input.

For scope \`project\` / \`both\`: ask about team practices —
  non-obvious commands, branch/PR conventions, env setup, testing quirks, common pitfalls.
  Skip items already obvious from README or manifests. Do not mark any answer as "recommended" — this is about the team's actual workflow.

For scope \`personal\` / \`both\`: ask about the user —
  role, familiarity with this codebase, sandbox URLs / accounts, communication preferences, specific tooling setup on their machine.

**Synthesize a proposal** combining Step 2 findings and Step 3 answers. Because skills and hooks are outside the current scope, ALL items become JIUWENSWARM.md notes (team) or JIUWENSWARM.local.md notes (personal). Present as a plain-text list, one line per item, grouped by target file. Ask for confirmation before proceeding.

**Build the preference queue** from the accepted proposal:
\`[{type: "note", target: "JIUWENSWARM.md" | "JIUWENSWARM.local.md", content: "..."}]\`
Steps 4–5 consume this queue.

## Step 4: Write JIUWENSWARM.md (if scope is project or both)

Target: \`${rootDir}/JIUWENSWARM.md\`

${existing.jiuwenswarmMd ? "File EXISTS — read it, propose a merged diff, use `ask_user` with `questions` to get user confirmation (options: 'Apply update' / 'Skip (keep current)'), then apply via Edit if confirmed. DO NOT use Write to overwrite silently." : "File is absent — use Write to create it."}

Consume queue entries whose \`target == "JIUWENSWARM.md"\`.

**Content test**: for each candidate line, ask "Would removing this cause the assistant to make mistakes?" If no, cut.

**Include**:
- Build / test / lint / format commands the assistant can't guess
- Code style rules that deviate from language defaults
- Testing quirks (e.g., "run single test with \`pytest -k ...\`")
- Repo etiquette (branch naming, PR conventions, commit message style)
- Required env vars, setup steps
- Important parts from existing AI coding tool configs if they exist (AGENTS.md, .cursor/rules, .cursorrules, .github/copilot-instructions.md, .windsurfrules, .clinerules) — extract key rules, not just link to them
- Non-obvious gotchas, architectural decisions worth knowing
- A brief **See also** section. Use plain markdown links for short references, or \`@path/to/file\` includes when a longer source document should stay authoritative:
    ${legacyIncludesEn(existing)}

**Exclude**:
- File-by-file structure or component lists (assistant can discover)
- Standard language conventions (assistant already knows)
- Generic AI etiquette / prompt engineering advice
- Long inline reference material — link to it rather than inline
- Commands already obvious from manifests (e.g., "npm test")
- Frequently-changing information — reference the source with \`@path/to/doc.md\` so the latest version is always loaded
- Generic advice like "write clean code" or "handle errors" — only include specific, actionable rules

**Specificity rule**: "Use 2-space indentation in TypeScript" is better than "Format code properly."

**No invented sections**: Do not make up headings like "Common Development Tasks" or "Tips for Development" — only include information expressly found in files you read.

**Prefix** the file with:
\`\`\`
# JIUWENSWARM.md

This file provides guidance to JiuwenSwarm (and any compatible AI coding assistant) when working with code in this repository.
\`\`\`

For monorepos: mention that subdirectory \`JIUWENSWARM.md\` is supported — ProjectMemoryRail walks up from cwd, so per-package docs are welcome.

For rule organization at team scale: suggest creating \`.jiuwen/rules/<topic>.md\` — these are auto-scanned, and may use frontmatter \`paths:\` to scope rules by the current working subtree / workspace.

## Step 5: Write JIUWENSWARM.local.md (if scope is personal or both)

Target: \`${rootDir}/JIUWENSWARM.local.md\`

${existing.jiuwenswarmLocalMd ? "File EXISTS — propose additions via Edit, never overwrite." : "File is absent — use Write to create it."}

Consume queue entries whose \`target == "JIUWENSWARM.local.md"\`.

Include: user's role, familiarity, personal URLs / accounts, communication preferences, tool setup specific to the user's machine.

**After writing**, idempotently update \`${rootDir}/.gitignore\`:
  1. Read \`.gitignore\` if it exists (use absolute path).
  2. Check whether each of the two lines below is already present (exact line match).
  3. Append only the missing ones:
       - \`JIUWENSWARM.local.md\`
       - \`.jiuwen/settings.local.json\`
  4. If \`.gitignore\` does not exist, create it with those two lines.

## Step 6: Summary

Briefly recap which files were written and the 3–5 most important items in each.

Remind the user:
- These files are auto-loaded into every coding session by ProjectMemoryRail.
- They're a starting point — feel free to edit by hand; changes take effect next turn.
- Re-run \`/init\` anytime to refresh based on new findings.

Then suggest optimizations as a short checklist, only those relevant to this repo:
- If tests are missing / sparse: suggest setting up a framework so the assistant can verify its own changes.
- If no formatter / lint config was found: suggest adding one with a one-line reason.
- If Step 2 found legacy AI config files (CLAUDE.md, AGENTS.md, etc.) not referenced in JIUWENSWARM.md: suggest consolidating via plain links or follow-up cleanup.
- **Always include**: "Run \`/compact\` after reviewing to trim this init session from history."
`;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const SCOPE_DESCRIPTION_EN: Record<ScopeKey, string> = {
  project: "write only JIUWENSWARM.md (run Step 4).",
  personal: "write only JIUWENSWARM.local.md (run Step 5).",
  both: "write both files (run Step 4 and Step 5).",
};

function yesNo(b: boolean): string {
  return b ? "EXISTS" : "absent";
}

function legacyIncludesEn(existing: ExistingFiles): string {
  // 当前方案：不用 @path 展开；写普通 markdown 链接
  const parts: string[] = [];
  if (existing.claudeMd) parts.push("[CLAUDE.md](./CLAUDE.md)");
  if (existing.agentsMd) parts.push("[AGENTS.md](./AGENTS.md)");
  if (existing.openjiuwenMd) parts.push("[OPENJIUWEN.md](./OPENJIUWEN.md)");
  if (existing.cursorRules) parts.push("[.cursorrules](./.cursorrules)");
  if (existing.copilotInstructions)
    parts.push(
      "[.github/copilot-instructions.md](./.github/copilot-instructions.md)",
    );
  return parts.length
    ? `"See also: ${parts.join(", ")}."`
    : `"(No legacy AI config files detected.)"`;
}
