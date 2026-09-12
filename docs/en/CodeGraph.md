# Code Graph retrieval

Code Graph gives the Coding Agent a set of index-based retrieval tools (focused: `resolve_symbol`, `find_code_symbols`, `search_source_text`, `inspect_code_structure`, `focus_code`) so it can locate symbols, call relations, and file structure before editing. It is off by default; behavior stays the original grep / read / edit path.

The index is shared by **canonical absolute path**: sessions with the same `project_dir` share one current graph and do not fork per chat. Different clones / worktrees get different graphs because their real paths differ. Closing a session only drops a reference; it does not delete the shared index.

`profile` turns the graph on or off; `agent` chooses who owns the tools. Plan and Explore never receive them.

Changing yaml **is not enough**. `graph` also needs `tree-sitter-language-pack`. `uv sync` does not install that package; install it yourself:

```bash
uv pip install tree-sitter-language-pack
```

Grammars download during the following `jiuwenswarm-init` / `jiuwenswarm-start`. Queries never download. With `profile: off` (the template default) the agent does not mount graph tools, does not build an index, and does not hide grep. Turning it off does **not** refresh or invalidate an existing graph; the graph just sits idle. `/status` shows `absent` while `off`, so it does not look like the graph is still in use. The on-disk checkpoint remains; opening `graph` again checks whether files changed in the meantime. If the parser cannot be installed or registered, graph tools are not mounted and the security table matches the off state. If a repo exceeds a cap (too many new files later, or RSS / disk during build), the graph is `UNAVAILABLE`: drop the old graph, restore grep, and ask to raise the matching file, source-byte, RSS, or disk cap. When the graph can finish indexing, grep / glob are removed.

With `profile: graph` and a session that already has `project_dir`, **indexing starts in the background as soon as the chat opens**. There is no project path at process start, so the graph cannot be built earlier.

## How to turn it on

Source Web / desktop GUI: left **More → Configuration → Other → Code Graph**. Set **Profile** to `graph` (the retrieval surface is fixed to focused; the UI cannot choose classic). The same page can change the mount point and build caps. After save, the next turn of the current Code chat can use the graph (`off`→`graph` mounts the focused five tools and hides grep immediately). Choosing `code_agent` as the mount point opens that subagent automatically; if the current session was opened on Root, start a new chat.

The Security tools table lists `resolve_symbol`, `find_code_symbols`, `search_source_text`, `inspect_code_structure`, and `focus_code` so you can set allow / ask / deny. Those tools are not mounted while `profile: off`.

You can also edit the product config (repo template: `jiuwenswarm/resources/config.yaml`; a typical local path: `~/.jiuwenswarm/config/config.yaml`):

```yaml
code_graph:
  profile: "graph"   # off or graph
  agent: "root"      # root or code_agent (product yaml defaults to root)
  max_files: 5000
  max_source_bytes: 40MB
  max_build_rss_mb: 4096
  max_cache_size_mb: 2048
```

File count and source volume decide whether a repo is admitted. RSS and disk are hard stops during build / refresh: if real usage exceeds the cap, stop immediately, drop the graph, and restore grep. An admitted repo waits until the **new** graph is ready; time is not a cap. Symbol count, edge count, and estimated bytes are not stop conditions.

If file count or source bytes are over the cap, or RSS / disk is not enough: first drop **this repo's old graph**. If that is still not enough, drop other repos from oldest to newest; do not delete a cache that a window is reading or building. Only after that, restore grep and ask to raise the matching cap. Saving config hot-reloads: raising a cap rebuilds; leaving it unchanged keeps grep. Sessions that share a `project_dir` share this current graph.

Sessions with the same `project_dir` share one graph. `write_file` / `edit_file` refresh immediately and keep only the current checkpoint (`updated_at` on `active.json` follows that write). IDE / shell edits are picked up by the watcher plus a pre-query check. `/status` uses the same token: `stale` means the disk changed but the graph has not refreshed yet. Version directories, dependency directories, coverage reports, pack artifacts, and frontend build caches are skipped by default. Manual-style text (`.md` / `.rst` / `.txt`) is not indexed by default; `search_source_text` still searches function bodies. A single file over 1MB is skipped.

Instance config (`~/.jiuwenswarm-instances/<name>/config/config.yaml`) is copied from the repo template only during `jiuwenswarm-init`. Later template edits do not overwrite an existing instance; align caps by hand, or run `jiuwenswarm-init -f --name <name>`.

`profile` accepts only `off` or `graph`; any other value is treated as `off`.

`agent` accepts only `root` or `code_agent`. The product template defaults to `root`: changing `profile` to `graph` is enough; you do not need to enable `code_agent`. If `agent` is omitted, the adapter still mounts on `code_agent` (the previous eval default). Choosing `code_agent` opens that subagent automatically; you do not need to also set `react.subagents.code_agent`.

## What you can do after it is on

Known class / function: `resolve_symbol` → `focus_code`.

Unknown exact name: `find_code_symbols` for candidates, then `focus_code`.

Exact literals (errors, config keys, decorators): `search_source_text`.

File or class structure: `inspect_code_structure`.

Call / inheritance relations: `focus_code` with `include_relations`. Product `graph` has **no** standalone `find_callers` / `read_symbol` / `select_code_context`.

After locating, the same mount point continues with `edit_file` / `write_file` and tests. Product mode has **no** `submit_code_context`. The retrieval surface is not a yaml knob: `graph` is focused; `off` is the original tools.

## Difference from eval scripts

Testers run ContextBench with `scripts/eval/`, which injects locate-exam prompts and mounts `submit_code_context` to produce `<PATCH_CONTEXT>`. That is not the product user path and **must not reuse the product yaml**. ContextBench source and gold parquet are **not in this repo**; set `CONTEXTBENCH_ROOT` on the test machine (or clone ContextBench as the sibling `../ContextBench`).

- Different task: eval is a locate exam (submit context); product locates, then `edit_file` / runs tests.
- Different hidden tools: eval also hides `bash` / `edit_file` / `write_file` (and `task_tool` when `--graph-agent root`) so Root cannot skip the graph. Product only hides `grep` / `glob` while the graph is available, and restores them on `UNAVAILABLE`. Eval must not fall back to grep, or ablation is polluted.
- Different default mount: eval `--profile graph` still defaults to `code_agent` (unlike the product yaml template `agent: root`) so earlier experiment numbers stay comparable.

See `scripts/eval/README.md`.

## Related docs

- Configuration panel: [Configuration](Configuration.md)
