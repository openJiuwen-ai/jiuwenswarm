# Repo-Aware Tools (code_search, find_symbol, symbol_outline, repo map)

Repo-aware tools give Code mode a structured way to understand a checkout: a ranked code search, name-based symbol navigation with a language-server-first / regex-fallback design, and a one-shot repository map injected into the system prompt. They replace the `ls` / `find` / `grep -rn … | head` / `cat` shell pipelines the model otherwise writes by hand, and they carry an explicit rule that the working tree is the only source of answers.

Chinese version: [仓库感知工具](../zh/仓库感知工具.md).

---

## Why

Measured in a SWE-bench Pro pilot (10 tasks, `deepseek-flash`, hermetic evaluation) before this change:

| Observation | Value |
|---|---|
| Tool-call mix | bash 43 %, edit_file 25 %, read_file 20 %, grep 7 %, glob < 1 %, **LSP 0 %** |
| Tool calls per task (median) | 57 (opencode baseline on the same tasks: 19) |
| Tokens per task (median) | 2.6 M (baseline 0.34 M) |
| Tasks that tried `git log --all` or a GitHub fetch to find the fix (gate off) | 7 / 10 |

Most `bash` calls were search pipelines whose raw output stayed in the context. The mounted `lsp` tool was never called, for three compounding reasons:

1. **Hidden by the progressive tool policy.** agent-core's `DeepAgent` applies `_DEFAULT_DIRECT_TOOL_NAMES` (`openjiuwen/harness/deep_agent.py`) as the exposure policy when `progressive_tool_enabled: true`. Every tool not on that built-in list — `lsp`, `list_files`, `enter_worktree`, … — is registered as **DEFERRED** and only reachable through `tool_search` / `tool_call`. The desktop log shows it: `visibility | direct=[ask_user, bash, edit_file, …, grep, read_file, …]` never contains `lsp`.
2. **Not in the prompt.** The Code prompt routed every lookup to `grep`/`glob` and never mentioned `lsp`.
3. **No language server installed.** Neither the benchmark containers nor a fresh developer machine has `pyright-langserver`, `typescript-language-server`, `gopls` or `rust-analyzer`, so the tool would have returned an error even if called.

The new tools declare `exposure=DIRECT` on their cards (an explicit card value always wins over the registration policy), are named in the prompt, and work without any language server.

---

## What is added

| Piece | Where | Purpose |
|---|---|---|
| `code_search` tool | `jiuwenswarm/agents/harness/code/repo_tools/code_search.py` | ripgrep-backed (pure-Python fallback) search; matches grouped by file, ranked, ±N context lines, capped |
| `find_symbol` tool | `repo_tools/symbols.py` | definitions and/or references of a symbol by name; LSP when available, per-language regex otherwise |
| `symbol_outline` tool | `repo_tools/symbols.py` | symbols of one file with line numbers and nesting; LSP `documentSymbol` or regex |
| `RepoMapRail` | `repo_tools/repo_map.py` | one bounded repository map per workspace in the system prompt |
| Shared engine and helpers | `repo_tools/_common.py` | root containment, ripgrep discovery and `--json` parsing, Python fallback, ranking/rendering |
| Adapter wiring | `server/runtime/agent_adapter/interface_code.py` | `_TOOL_BUILD_NAMES` entries, `_build_repo_map_rail` fixed rail |
| Swarm wiring | `agents/swarm/providers/{tools,code_rails}.py`, `registry.py`, `config_specs.py` | `swarm.repo_tools`, `swarm.repo_map` for code team members |
| Prompt | `agents/harness/code/prompt/code_prompt_builder.py` | routing to the new tools; rule against mining git history / the network for the answer (explicit user requests for history stay allowed) |
| Config | `resources/config.yaml` and the two distributed team templates | tool list entries, `modes.code.repo_tools` (incl. `ignored_dirs`), `modes.code.repo_map` |

### Data flow

```
model ── tool call ──► CodeSearchTool / FindSymbolTool / SymbolOutlineTool
                            │  root = adapter root resolver (runtime workspace → project_dir → cwd)
                            │  every path is contained inside that root
                            ├─ LspBridge.available(file)?  (find_symbol / symbol_outline only)
                            │      yes → call_lsp_tool(workspaceSymbol | findReferences | documentSymbol)
                            │      no  → regex heuristics per language
                            ▼
                     CodeSearchEngine
                        ├─ ripgrep --json --no-require-git -C N  (OPENJIUWEN_RG → SDK bundled → PATH)
                        └─ pure-Python walk + re                 (no ripgrep, or ripgrep cannot run; worker thread, same time budget)
                            ▼
                     FileHits → rank (matches ↓, source > tests > vendor, shorter path) → cap → render
                            ▼
model ◄── ToolOutput.data.content (text) + structured fields (files/matches/engine/truncated)

RepoMapRail.before_model_call ── first call per workspace root ──► cached_repo_map(root)
                                                                     └─► PromptSection "code_repo_map" (priority 60)
```

The tools execute ripgrep directly with `cwd` pinned to the search path; they do not go through the sys_operation shell. Path containment is enforced in `contain_path`: any `path` / `file_path` that resolves outside the workspace root is rejected with a readable error.

---

## Tools

### `code_search`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `pattern` | string | required | regex (ripgrep/Rust syntax, also valid Python `re`) or literal text |
| `path` | string | workspace root | sub-directory or file, relative to the root |
| `glob` | string or list | none | ripgrep-style globs, `!` excludes (`*.py`, `src/**/*.ts`, `!**/tests/**`) |
| `language` | string | none | python, javascript, typescript, go, rust, java, kotlin, csharp, c, cpp, ruby, php, swift, scala, shell, markdown, yaml, toml, json |
| `literal` | bool | false | match plain text |
| `case` | smart / sensitive / insensitive | smart | smart = insensitive unless the pattern has an uppercase letter outside a regex escape (`\S` does not count) |
| `context` | int 0–10 | 3 (config) | context lines around each match |
| `max_results` | int 1–200 | 40 (config) | total matches across files |

Output (model view):

```
5 matches in 3 files
== src/pkg/cart.py (2 matches)
       5:     def total(self):
  >    6:         return compute_total(self.items)
       7:
…
```

Structured fields: `engine` (`ripgrep` | `python`), `total_matches`, `total_files`, `shown_matches`, `truncated`, `timed_out`, `files[{path, matches[{line, text}]}]`.

Ranking: files with more matches first; among equals, source files before test directories before vendored/generated code; then shorter paths. `.gitignore` is honoured even in checkouts without `.git` (`--no-require-git`). On top of that, `modes.code.repo_tools.ignored_dirs` (default: version control, dependency and tool caches, editor metadata — `.git`, `node_modules`, `__pycache__`, `.venv`, `.tox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.next`, `.cache`, `.idea`, `.vscode`, `.jiuwenswarm`, …) is pruned at every depth. Build outputs such as `dist`, `build`, `target` and `logs` are **not** on that list because real projects use those names for source; put them in `.gitignore` or in `ignored_dirs` if you want them skipped. An invalid regex is returned as a tool error, never as "0 matches". Any other ripgrep failure falls back to the built-in search for that call, with a note in the header: a ripgrep that cannot start or crashes (killed by a signal — for example a quarantined binary that macOS kills on launch — or an exit code other than 0/1/2) is also set aside for 10 minutes, while an ordinary ripgrep error with no output (exit code 2) is retried on the next call. If ripgrep still produced matches together with an error, those matches are kept and the header carries a warning. Lines are clipped to 300 characters, files over 2 MB are skipped, ripgrep is killed after 30 s and the result is marked partial.

### `find_symbol`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | one identifier |
| `kind` | definition / references / all | definition | what to list |
| `file_path` | string | none | pins the language; with `line`, enables exact LSP references |
| `line` | int | none | 1-based line of the symbol in `file_path` |
| `language` | string | from `file_path` | language when no file is given |
| `path` | string | root | restrict the search |
| `max_results` | int | 20 (config) | per section |

Engine selection:

| Situation | Definitions | References | Outline |
|---|---|---|---|
| LSP manager initialised **and** a server answers for the file type | `workspaceSymbol(query=name)` | `findReferences` when `file_path` + `line` are given, else regex | `documentSymbol` |
| No language server for the file type, or LSP not initialised | regex definition patterns for the language (union of all languages when unknown) | word-bounded search, definition lines tagged `[definition]` | indentation-aware regex outline |
| LSP call fails or times out (`lsp_timeout`) | regex | regex | regex |

Every result header names the engine, e.g. `Definitions of compute_total (ripgrep): 2` or `(lsp)`. The LSP path only uses a server that is **already running** for the file's extension (it reads `LSPServerManager.get_status()` and never calls `get_or_start_server`); starting servers stays with `LspRail`, so a `find_symbol` call can never block on a server start-up.

Definition patterns are maintained per language in `symbols.py::_DEFINITION_PATTERNS` and must stay valid for both the Rust regex engine (ripgrep) and Python `re`: no look-around, no back-references.

### `symbol_outline`

| Argument | Type | Default |
|---|---|---|
| `file_path` | string | required |
| `max_symbols` | int | 200 (config) |

```
src/pkg/cart.py: 5 symbols (regex)
  L6     function compute_total
  L10    class Cart
  L11      method __init__
  L14      method total
```

Regex outlines exist for python, javascript/typescript, go, rust, java/kotlin/csharp/scala, c/cpp, ruby, shell and markdown (headings); other languages get a generic keyword pass. Binary files and directories are rejected.

### Repo map

`RepoMapRail` builds the map once per workspace root (per-process TTL cache of 5 minutes) and adds it as the `code_repo_map` prompt section, priority 60 (right after the session guidance). It stays byte-stable within a session so prompt caching keeps working, and is replaced when the root changes (project switch, worktree enter/exit).

Content, in order, trimmed to `max_chars`: header with file count and language mix; manifests at the root; packages (pyproject / package.json / go.mod / Cargo.toml / Python packages outside test dirs); test directories; detected commands (`pytest` when configured, `ruff`, `mypy`, `tox`, `nox`, npm/pnpm/yarn scripts, `go test`, `cargo test`, `mvn`, `gradle`, `cmake`, `rspec`, Makefile targets) with a "verify before relying on them" note; the directory tree to `depth` with per-directory file counts and, at the leaf level, up to 12 file names; a closing line pointing the model at the tools. Dot-directories, `IGNORED_DIRS` and `.gitignore` entries are skipped. On the JiuwenSwarm checkout itself (1 174 files) the map is 5 997 characters and builds in about 10 ms.

### Read policy

`read_file` and `grep` are refused when they target a path the user has denied. The repo tools search the whole repository, so a denied file cannot be refused up front without refusing every search. Instead, each result is filtered per file (`repo_tools/read_policy.py`), and the policy is re-read on every call, so hot-reloaded permissions and edited project policies apply at once.

| Source | When it applies | What is hidden |
|---|---|---|
| Permission engine: effective Global ⊕ User ⊕ Session permissions, evaluated as a `read_file` of each file | Only when `permissions.enabled` is true, the same condition under which `read_file` is checked | A `deny` from tool/argument rules; `deny` or `ask` from the path layer (`file_guard`, including the package's sensitive paths such as `.env*` and `.ssh`) |
| Deny-only project policy `<project>/.jiuwenswarm/permissions.yaml` (shell-policy package) | Whenever that package is present and `permissions.project_policy.enabled` is not false | `deny_read` and `deny_paths` entries |

- **Results:** matches in hidden files are dropped. Whenever a read policy is active, the header carries the fixed notice `files denied by the read policy are left out of these results`, whether or not anything was dropped. No count or name of hidden files is ever shown: a count would let a search for a guessed string reveal whether a denied file contains it. LSP answers are filtered the same way by file, and an LSP answer that is empty after filtering is treated exactly like an empty one, so both produce the same text.
- **Explicit paths:** a `path` or `file_path` argument that points at a hidden file, or at a directory whose whole content is denied, is refused with `not readable under the read policy`. `symbol_outline` of a hidden file is refused.
- **Repo map:** hidden files and fully denied directories are neither listed nor read. The map is rebuilt when the policy changes; an unchanged policy keeps the prompt byte-stable.
- **Symlinks:** files are checked at their lexical and resolved paths, so a symlink inside the project cannot expose a denied target.
- **Fail closed:** an error while evaluating one file hides that file. An error while building the policy, or a project policy file that exists but cannot be used, refuses the search and withholds the map.
- **Why `ask` hides:** a search cannot ask for approval per file. The model can still `read_file` the file, which asks the user as usual.

---

## Configuration

All keys live under `modes.code` in `config.yaml` (and the two distributed team templates). Defaults are those shipped in `jiuwenswarm/resources/config.yaml`.

```yaml
modes:
  code:
    tools:
      - …
      - code_search          # remove a name to switch that tool off
      - find_symbol
      - symbol_outline

    repo_tools:
      code_search:
        max_results: 40      # 1–200, total matches per call
        context: 3           # 0–10 context lines
        rg_path: null        # explicit ripgrep binary; null = OPENJIUWEN_RG → SDK bundled → PATH
      symbols:
        max_results: 20      # per section of find_symbol
        max_outline_symbols: 200
        lsp_timeout: 10.0    # seconds; an LSP call over this falls back to regex
      ignored_dirs:          # directory names pruned at every depth by all three tools and the repo map;
        - .git               # the full list replaces the default (VCS + caches + editor dirs);
        - node_modules       # dist/build/target/logs are deliberately absent — use .gitignore
        - __pycache__        # (see the template for the complete default list)

    repo_map:
      enabled: true          # false: RepoMapRail is not mounted, nothing is injected
      depth: 2               # 1–4
      max_chars: 6000        # ≈ 1.5k tokens
```

Off-behaviour: with the three tool names removed and `repo_map.enabled: false`, the only remaining change is the prompt text (+856 chars, ≈210 tokens), which tells the model to fall back to grep/glob when the tools are absent.

The swarm code profile reads the same keys through `config_specs._repo_tools_params` / `_repo_map_params`; `swarm.repo_tools` builds only the tools that are listed in `modes.code.tools`.

Environment: `OPENJIUWEN_RG` (or `ICODE_RG`) points at a ripgrep binary.

**Without ripgrep** the built-in pure-Python engine answers automatically. It runs in a worker thread, so a scan never stalls other sessions or streaming replies, and it stops at the same 30 s time budget as ripgrep or after 20 000 files. When either limit cuts a scan short, the result header says so ("built-in search (no ripgrep) stopped at its file/time limit after N files; results may be incomplete …") instead of reporting a silent "no matches". It is noticeably slower than ripgrep on large trees, so install ripgrep for source runs:

| Platform | Command |
|---|---|
| macOS | `brew install ripgrep` |
| Debian / Ubuntu | `sudo apt install ripgrep` |
| Fedora | `sudo dnf install ripgrep` |
| Windows | `winget install BurntSushi.ripgrep.MSVC` or `choco install ripgrep` |
| Any (Rust) | `cargo install ripgrep` |

Or point `OPENJIUWEN_RG` (or `modes.code.repo_tools.rg_path`) at a binary you already have. On macOS a downloaded binary that still carries the quarantine flag is killed on launch; remove it with `xattr -d com.apple.quarantine /path/to/rg`. The tools then fall back to the built-in search on their own and log one warning.

---

## How to confirm it is working

Agent-server log at agent creation:

```
[JiuwenSwarmCodeAdapter] Tool code_search registered from config
[JiuwenSwarmCodeAdapter] Tool find_symbol registered from config
[JiuwenSwarmCodeAdapter] Tool symbol_outline registered from config
[JiuwenSwarmCodeAdapter] RepoMapRail create success (depth=2 max_chars=6000)
```

ProgressiveToolRail visibility line (the new names must appear under `direct=`):

```
[ProgressiveToolRail][DEBUG] visibility | meta=['tool_call', 'tool_search'] | direct=['ask_user', 'bash', 'code_search', 'edit_file', 'find_symbol', …, 'symbol_outline', …]
```

First model call of a workspace:

```
[RepoMapRail] injected map for /path/to/repo: 5997 chars, 1174 files
```

In a session, ask "where is `X` defined?" — the tool result header shows the engine, e.g. `Definitions of X (ripgrep): 1` or `(lsp)`. The session history (`history.jsonl`) records `chat.tool_call` entries named `code_search` / `find_symbol` / `symbol_outline`.

Disabled state: `[JiuwenSwarmCodeAdapter] RepoMapRail disabled by modes.code.repo_map.enabled`; a tool name missing from `modes.code.tools` simply does not appear in the registration lines.

---

## Limitations

- Regex heuristics are approximations: dynamically generated names, macros and unusual formatting can be missed; the engine label lets the model weigh the result. Install a language server (`npm i -g pyright`, `typescript-language-server`, `gopls`, `rust-analyzer`) for exact results; `LspRail` picks it up automatically.
- `find_symbol` references through LSP need a position (`file_path` + `line`); without one the word-bounded search is used.
- The read policy covers the permission engine and the project policy. It does not cover user hooks; a PreToolUse hook sees the search arguments, not each result file.
- The repo map is a snapshot; files created during the session appear in searches but not in the map until the cache expires or the workspace changes.
- The Python fallback engine and the repo map apply `.gitignore` with git semantics — nested `.gitignore` files, `!` negation, anchoring, directory-only patterns, `**`, and `.git/info/exclude` — and a test checks they ignore the same files as ripgrep. Not covered: the user's global excludes file (`core.excludesFile`). Fallback smart-case treats an uppercase letter outside a regex escape as case-sensitive, which matches ripgrep for common patterns but not every edge case.
- `find_symbol`/`symbol_outline` use LSP only when a matching server is already running; on a machine without `LspRail`-started servers they always use the regex heuristics.
- The prompt rule is scoped to answer-hunting; it does not stop `git status`/`git diff` or a user's explicit request for blame/log. The `--implement` headless driver (separate change) should carry a stricter, unconditional version in its continuation hint; it is not part of this change.

---

## Tests

`tests/unit_tests/agents/harness/code/repo_tools/` — 134 tests (code_search 28, symbols 51, repo map 12 plus 1 against a real agent-core DeepAgent, read policy 24, fallback hardening 18): both search engines (ripgrep tests skip when no binary is found; set `OPENJIUWEN_RG` to run them), ranking and ignores, globs and language filters, containment, truncation, rg JSON parsing, definition patterns per language, fake LSP present / absent / broken, outlines per language, repo map builder, cache and rail. The fallback-hardening tests check that the built-in search keeps the event loop responsive, reports file and time limits, takes over when ripgrep crashes, cannot start or fails without output (fake `rg` scripts), keeps pattern errors as errors, and applies nested `.gitignore` rules the same way ripgrep does. The read-policy tests run tool calls through agent-core's real ability manager with JSON-string arguments and evaluate the shipped permission template with the real engine. `tests/unit_tests/agentserver/test_code_adapter_repo_tools.py` (12) covers adapter registration, config, and that the adapter and the swarm providers wire the session's permissions into every repo tool and the map; `tests/unit_tests/agents/harness/code/test_code_prompt_repo_tools.py` (3) covers the prompt; the swarm assembly suite covers `swarm.repo_tools` / `swarm.repo_map`.
