# Shell network policy and deny-only project policy

This page covers two additions to the existing permission system
([Tool Permissions & Security](ToolPermissionsSecurity.md)):

* a **shell network policy**: whether shell commands may reach the network;
* a **project policy file**, `<project>/.jiuwenswarm/permissions.yaml`, that can
  only **deny**. It is enforced by the Code-mode guard rail and is never merged
  into the permission engine.

Nothing changes for a default configuration without a project file: the
interactive shell network policy is `allow`. Headless runs (process CLI and the
implement driver) default to `deny`.

## 1. Why

* agent-core's permission engine has three pipelines: tool/command rules
  (Pipeline A), file paths including paths extracted from shell commands
  (Pipeline B), and URLs for **fetch tools** (Pipeline C, `net_guard`). Nothing
  covered network access from the **shell**: `curl`, `wget`, `pip install`,
  `git clone|fetch|push`, `npm install` and similar ran unchecked.
* The permission engine is off by default (`permissions.enabled: false`), and a
  headless run has nobody to answer an `ask`.
* There was no way for a repository to declare its own restrictions.
* In SWE-bench-style runs the agent repeatedly tried to fetch upstream fixes
  from GitHub; the harness had to blackhole the network at the container level.

## 2. Threat model

The adversaries are (a) the agent itself acting on injected instructions and
(b) a repository that ships a hostile `.jiuwenswarm/permissions.yaml`.

* A project file must never **loosen** anything the user configured. It is
  authored by whoever controls the repository, not by the user.
* The agent must not be able to change the project file (or delete it) from
  inside the session.
* Headless runs must not silently reach the network by default.

This is a policy guard, not a sandbox (see §6).

## 3. Shell network policy (user/global configuration)

`permissions.shell_network: allow | ask | deny` (default `allow`) and
`permissions.headless.shell_network` (default `deny`, headless runs only). The
effective level is the **strictest** of the configured level, the headless level
(when headless) and the project file's `shell_network: deny`.

Detection matches a network-capable command **at command position**, case
insensitively (macOS file systems run `CURL` as `curl`). A command position is
the start of the text, a line break, or a point after `;` `&` `|` `(` `{` `!`
`` ` `` `$(`, after the shell keywords `if` / `elif` / `then` / `else` / `do` /
`while` / `until`, after a `case` pattern (`in a)` or `;; b)`), or inside a
quote that follows `-c` or `eval` (so `bash -c 'curl …'` counts but
`echo 'pip install x'` and `grep -r 'npm install' docs` do not). Before the
command it accepts `VAR=value` assignments, a directory prefix, and chains of
launchers **with their option values**: `sudo -u a`, `doas -u x`, `env -u X`,
`timeout -s KILL 25`, `nice -n 5`, `ionice -c 3`, `stdbuf -oL`, `watch -n 5`,
`xargs -I {}`, `exec` (also `exec -a name`), `nohup`, `time`, `command -p`,
`builtin`, `chronic`, `unbuffer`, `busybox`. `command -v curl` only looks the
tool up and is not a launcher. `$(which curl)` and `` `which wget` `` are
resolved to the named tool. The same launcher and keyword list is used by
`deny_commands` (§4), so both agree on what the command word is.

**Quoting and escapes.** The command is matched as written and once more
normalised: a backslash before a character is dropped, empty quote pairs are
removed, and quotes around a single word without spaces are removed. So
`\curl`, `c\url`, `p''ip`, `"curl"` and `'c'url` are all seen as `curl` / `pip`.
Quoted text with spaces stays an argument.

Covered classes: curl, wget, HTTP clients with a URL or host argument, also
after options or an HTTP method (`http GET https://…`, `lynx -dump https://…`;
`http`/`https`/`httpie`, `xh`, `curlie`, `aria2c`, `lynx`, `links`, `elinks`,
`w3m`), netcat/telnet/socat, ssh/sftp/ftp,
scp/rsync to a remote, `/dev/tcp` and `/dev/udp` redirections, git
clone/fetch/pull/push/ls-remote/remote update/submodule update, svn/hg
checkouts, pip (any `pipX.Y`, `pythonX.Y -m pip`), pipx/uvx, uv
add/sync/pip/`run --with`, poetry/pdm add/install, conda/mamba/micromamba
install, npm/yarn/pnpm/bun installs and npx/dlx, deno with a URL, gem/cpan,
system package managers, go/cargo fetches, docker/podman pull/push/login, the gh
CLI.

Enforcement:

| Situation | `deny` | `ask` |
|---|---|---|
| interactive, engine off (`permissions.enabled: false`) | refused by `ShellPolicyGuardRail` | not enforced (no approval channel); logged once |
| interactive, engine on | refused by the guard rail; the engine also gets a deny rule `shell_network_policy` | the engine prompts through rule `shell_network_policy`, unless that rule could loosen the user's baseline (below) |
| headless (any engine setting) | refused by the guard rail | refused (nobody can answer) |

**Where the refusal applies.** The engine-off rows above (and every `deny`
refusal by the guard rail) apply only where `ShellPolicyGuardRail` is mounted:
**Code mode**, interactive and the **headless process CLI**. The general agent
modes and the swarm/team member assembly do not mount it. There,
`shell_network` is enforced only through the engine rule, so **with the engine
off (`permissions.enabled: false`) a general agent mode does not enforce
`shell_network` at all, not even `deny`**. With the engine on, the engine's
deny or ask rule applies in those modes as well. The engine rule matches the
command as written; the normalised forms above (`\curl`, `p''ip`) are seen only
by the guard rail, so an interactive engine `ask` does not prompt for them.

**The engine rule never loosens.** agent-core evaluates rule hits before tool
levels and defaults, and several non-deny hits combine as the strictest. A
`deny` rule can therefore only make a decision stricter. An `ask` rule could
loosen exactly one case: a shell call that no other rule hits, whose tool has no
level of its own, while `defaults["*"]` is `deny` (a tool-level `deny`
short-circuits before any rule). In that case the `ask` rule is **not** added
and the user's own `deny` baseline decides (logged at info level). Tool levels
in dict form (`bash: {"*": deny}`) are read the way the engine reads them.

The engine rule is built only from the user/global configuration, never from
the project file, and it is never written into the User/Session overlays (the
overlay extraction does not copy `rules`).

The refusal the model sees:

```json
{"success": false, "error": "permission_denied", "policy": "shell_network",
 "command_class": "pip install",
 "detail": "Network access from the shell is disabled (pip install) by permissions.shell_network. Work from the files already in the project; ..."}
```

## 4. Project policy file (deny-only)

`<project root>/.jiuwenswarm/permissions.yaml`, the project root of the running
turn. Either top-level keys or a `permissions:` section:

```yaml
deny_paths:                  # every access: read, write and exec
  - secrets                  # a directory: everything below it
  - ".env*"                  # a glob, relative to the project root
  - {path: build, axes: [write]}
deny_read:  ["../**"]        # outside the project, one level up
deny_write: ["/etc/**"]
deny_exec:  [scripts/release.sh]
deny_commands:
  - "rm -rf *"               # plain pattern: * matches anything, plus any arguments
  - "re:^docker\\s+push\\b"  # re: a regular expression
shell_network: deny          # only "deny" is accepted
writable_roots: [src, tests] # path-writing file tools may only write here
```

**Supported keys only.** Keys of the user/global permission schema, which in a
project file could only grant, are ignored and logged: tool levels, command
rules, `defaults`, `approval_overrides`, `file_guard`, `net_guard`, any key
starting with `allow` or `ask`, and `shell_network: ask|allow`. **A project
cannot express ask or allow.**

**Malformed files fail closed.** Anything that would silently drop a deny makes
the whole file unusable, exactly like a file that is not YAML: a top level that
is not a mapping (for example a list), an unknown key such as a misspelling
(`deny_path`, `deny-commands`, `shellnetwork`), a value of the wrong type
(`deny_paths: 42`), a malformed entry, a bad `re:` pattern, or a
`shell_network` value that is not a level. File and shell tools are then
refused (`project_policy_invalid`) with one line that names the file, the
problem, and the way out:

```text
The project policy file <root>/.jiuwenswarm/permissions.yaml cannot be used (unknown key 'deny_path'); file and shell tools are refused until the user fixes or removes it, or sets permissions.project_policy.enabled: false in config.yaml to ignore project files.
```

**Paths.** A relative entry is anchored at the project root with `pathlib`, so
`.env*` stays `<root>/.env*` and `../**` becomes `<parent of root>/**`. The
literal part before the first wildcard is resolved (`..` collapsed, symlinks
followed; `/etc` is `/private/etc` on macOS), because the rail compares against
resolved call paths. An entry without wildcards is a prefix: the path itself
and everything below it. Globs support `**/`, `**`, `*`, `?` and `[…]`; a glob
that matches a directory also covers everything below it. On a
case-insensitive file system (macOS default) comparisons are case-folded.

**Commands.** Each simple command of a shell line is checked: the line is split
on `;` `;;` `&` `|` `&&` `||` `(` `)` `{` `}` `` ` `` `$(` and newlines (so
subshells, groups and `case … ) … ;;` bodies are commands of their own). Each
line is split three ways: as written, with all quotes removed (so
`bash -c 'rm -rf x'` yields `rm -rf x`), and normalised as in §3 (so `\rm`,
`r''m` and `"rm"` yield `rm`). Then `VAR=value` prefixes, the keywords `if`,
`elif`, `then`, `else`, `do`, `while`, `until` and `!`, the launchers of §3
(`sudo`, `env`, `timeout N`, `command -p`, `builtin`, `exec`, `stdbuf`,
`busybox`, `xargs`, …) plus `bash -c` and `eval`, and a directory prefix on the
command word are stripped. A plain pattern matches a segment case-insensitively
with any further arguments; an `re:` pattern is searched in the whole line and
in each segment.

**Where it is enforced.** Only in the Code-mode guard rail
(`ShellPolicyGuardRail`), which runs before agent-core's permission rail
(priority 95 vs 90) and only ever refuses:

| Check | Refusal (`policy`) |
|---|---|
| project file unusable (unreadable, not YAML, over 64 KB, a symlink resolving outside the project, not a regular file, or malformed as above) | `project_policy_invalid`, for file and shell tools until it is fixed |
| write to the policy file (see §5) | `project_policy_file` |
| any access of the call hits a path deny: file-tool arguments with their read/write/exec action, `apply_patch` targets as writes, paths agent-core's extractor finds in shell commands (redirections, path-aware commands, interpreter scripts as exec), and the guard's own reader extraction (below) | `project_deny_paths` |
| a shell command matches `deny_commands` | `project_deny_commands` |
| a path-writing file tool writes outside `writable_roots` | `writable_roots` |
| a network command while the project says `shell_network: deny` | `shell_network` |

**Shell readers.** agent-core's extractor misses many commands that read files,
so the guard adds its own best-effort extraction on the normalised command:

* every non-option argument of a known reader is a read: `cat`, `head`, `tail`,
  `less`, `sed`, `awk`, `base64`, `xxd`, `od`, `hexdump`, `strings`, `grep`,
  `rg`, `cut`, `sort`, `diff`, checksum tools, `tar`, `zip`, `jq`, `ls`,
  `find`, `tree`, `du`, `source`, and similar (`cp` and `rsync` are left to agent-core's extractor, which knows which argument is written). The script or
  pattern argument of `sed`, `awk`, `grep`, `rg` and `jq` is skipped unless it
  was given with `-e` (a `-f FILE` script file is a read);
* every `REV:path` argument of `git` is a read (`git show HEAD:secret/key`);
* a command word containing `/` is an exec of that path (`./secret/tool`);
* `cd DIR` changes the base for later relative paths in the same line
  (`cd secret && cat key`);
* globs in these arguments are expanded against the file system (at most 64
  matches).

**Scope.** Project files are enforced in **Code mode**, interactive and
**headless process CLI** runs. **Other agents ignore them**: the general agent
modes and the swarm/team member assembly do not mount this rail.

**The engine never sees the project file.** The engine snapshot is built from
Global ⊕ User ⊕ Session configuration only (plus the user/global shell-network
rule of §3). The effective decision is therefore the stricter of "the user's
engine decision" and "the project's denies": the engine's decision is identical
with or without a project file, and the guard rail can only refuse.

**Caching and cost.** The result is cached by `(path, mtime, size)` (and the
symlink target's), usable or not, so an unchanged invalid file is neither
re-read nor re-reported on every call. The parsed file's globs and command
patterns are compiled once into combined regular expressions. Measured on an Apple-silicon laptop with a 200-entry file (100 prefixes, 100
globs) plus 40 command patterns: about 0.2–0.5 ms per tool call (median), and
about 12 ms once whenever the file changes.

`permissions.project_policy.enabled: false` in the `permissions` section of
`config.yaml` ignores project files.

## 5. Policy file protection

File tools may not write `.jiuwenswarm/permissions.yaml` in any mode, with or
without a project file (the resolved target is compared, so symlinks and `..`
do not help; case-folded on case-insensitive file systems). `apply_patch` bodies
are parsed for their targets:

* codex-style `*** Update File:`, `*** Add File:`, `*** Delete File:` and
  `*** Move to:` headers, in any case and with leading blanks;
* unified-diff `--- a/…`, `+++ b/…`, `diff --git`, `rename from/to` and
  `copy from/to` lines;
* CRLF and CR-only newlines;
* git's quoted paths with C escapes and `\ooo` octal escapes decoded;
* nested string arguments (lists, dictionaries).

A body that mentions `.jiuwenswarm` or `permissions.yaml` and either yields no
parsed target, or mentions it on a header-like line that did not parse, is
refused (fail closed). Mentions inside hunk content lines are allowed.

Shell commands are refused in every mode when they:

* redirect into the file (`>`, `>>`, `>|`, `&>`, including `cat /dev/null >` and
  `: >`), or `tee`, `sed -i`, `perl -i`, `rm`, `unlink`, `mv`, `cp`, `truncate`,
  `ln` (also `-sf`), `chmod`, `chown`, `install`, `dd of=`, `shred`, `rsync`,
  `git checkout|restore|rm|mv` it;
* remove, move, link, copy into or re-permission the `.jiuwenswarm` directory
  itself (`rm -rf .jiuwenswarm`, `rm .jiuwenswarm/*`, `mv .jiuwenswarm x`,
  `cp evil.yaml .jiuwenswarm/`, `chmod 000 .jiuwenswarm`);
* `find` the directory or the file with `-delete`, `-exec` or `-ok`;
* `cd` into `.jiuwenswarm` (spelled out) and then write `permissions.yaml`;
* write through an **alias**: every word of a simple command that also has a
  writing verb is resolved against the project root (symlinks followed, `..`
  collapsed), so `ln -s .jiuwenswarm cfg` followed later by
  `rm cfg/permissions.yaml` is refused when the link exists.

The user edits the file outside the session.

**Writable roots.** `writable_roots` (relative to the project root, or absolute)
restrict every path-writing file tool: `write_file`, `edit_file`,
`write_text_file`, `write`, `search_replace` and `apply_patch`. The set is
derived from the permission capability table, so a new path-writing tool is
covered automatically. The target path is resolved (symlinks followed, `..`
collapsed) and must lie inside one of the roots.

## 6. What is enforced, and what is best effort

* **Enforced:**
  * a project file cannot change the permission engine's decision, and the guard
    rail only refuses, so the combined decision is never looser than the user's;
  * project path denies on file-tool arguments and `apply_patch` targets;
  * the repo-aware tools (`code_search`, `find_symbol`, `symbol_outline`): their
    `path`, `file_path` and include-`glob` prefixes are checked as reads, and a
    directory argument is checked for everything below it;
  * agent-core's search and listing tools (`grep`, `glob`, `list_files`): a
    `path` that is a denied directory, or a directory everything below which is
    denied (`secret` for `secret/**`), is refused up front. A search or listing
    of a parent directory runs, and the guard then drops every result that
    belongs to a read-denied path from both the model's text and the structured
    result: `grep` lines in every output mode (including context lines and
    ripgrep's `path-N-text` form), counts, and file lists, `glob` matches, and
    `list_files` entries whose own path is denied (a directory such as `secret/`
    stays listed; its contents do not). Symlinks into a denied directory are
    hidden too. The result then ends with a fixed line, "(Files denied by the
    read policy in .jiuwenswarm/permissions.yaml are left out of these
    results.)". The line is added whether or not anything was dropped, so a
    search for a guessed string cannot reveal whether a denied file contains it.
    If the filter fails, the results are withheld rather than passed through;
  * a shell reader aimed at such a directory (`grep -rn x secret`,
    `rg x secret`, `ls secret`) is refused the same way;
  * a shell command that reads a whole tree is refused when a read-denied path
    lies inside that tree: `grep -r`/`-R`/`--recursive`/`-d recurse` (short
    clusters such as `-rn` included), `rg`, `ag`, `ack` and `ugrep` (they
    recurse by default; with no path they search the working directory, unless
    something is piped into them), `git grep`, `find … -exec`/`-ok`, `fd -x`/`-X`,
    a lister (`find`, `fd`, `rg --files`, `git ls-files`, `ls`) feeding `xargs
    CMD`, `tar c`, `zip -r`, `7z a`, `cp -r`/`-a`, `scp -r`, `rsync -r`/`-a`,
    `ditto` and `mv`. `cd`/`pushd`, `git -C` and quoted `bash -c '…'` bodies are
    followed. "Inside" means an existing denied area below the tree root
    (`secret/` for `secret/**`), or a file below it that a glob deny matches
    (`.env*`, `**/*.pem`; found by a bounded walk that fails closed on very large
    trees). The same search rooted in a folder with nothing denied below it
    (`grep -rn x calc`) runs. The refusal points to the filtered `grep` tool.
    This closes the fallback seen in the desktop re-test, where the model
    answered a filtered `grep` with `cd <project> && grep -rn KEY .`;
  * `git_prepare` commits (the listed `paths`, or every changed tracked file,
    staged included, with `all_tracked`) and `git_publish` pushes (the commits
    not yet on any remote-tracking branch) are refused when they contain a file
    denied for reading or writing;
  * writable roots for every path-writing file tool;
  * file-tool writes to the policy file;
  * user/global and project `shell_network: deny` for the command classes of §3.
* **Best effort, not a sandbox:**
  * an interpreter one-liner (`python -c "import urllib…"`), a compiled binary, a
    script file that downloads, an alias or shell function, a variable holding
    the command name (`$CMD example.org`), or a tool the detector does not know
    can still reach the network;
  * path denies on shell commands depend on agent-core's path extractor plus the
    guard's reader extraction (§4). Not seen: paths built from variables or
    command substitution (`cat $(echo secret)/key`, `d=secret; cat $d/key`), a
    `cd` through a variable, readers not in the list, files opened by an
    interpreter (`python -c "open(…)"`, `node -e`) or a script (a literal quoted
    path may be caught incidentally, but this is not reliable), a tree walk the
    recursive-read check above does not recognise (a loop over `$(find …)`, a
    script or interpreter that walks directories, a reader it does not know),
    and a symlink in an otherwise clean folder pointing into a denied area, read
    by a command that follows links (`grep -R`, `rg -L`, `find -L`). Names of
    denied files can still be listed (`find .`, `ls -R`, `rg --files`); only
    their contents are protected. The guard cannot filter shell output, so the
    base `grep` tool, which is filtered, is the safe way to search a project
    with read denies;
  * `code_search` / `find_symbol` over a parent of a denied path (for example
    the whole repository) cannot be refused up front without refusing every
    search; filtering denied files out of their results is the tools' job (the
    repo-tools MR);
  * `run_tests` is not a shell tool, so the shell network policy (including the
    headless default `deny`) does not apply to it: test code can still reach the
    network. Use a network-less container when that matters;
  * `deny_commands` is pattern matching on the command text. Quoting, escapes,
    launchers and keyword positions are normalised (§4), but variables holding
    the command name (`g=git; $g push`), aliases, shell functions, scripts,
    interpreters (`python -c "shutil.rmtree(…)"`), `find … -exec rm`, and
    equivalent spellings of the arguments (`rm -fr`, `rm -r -f`,
    `git -C . push`) are not matched unless the pattern covers them (use
    `re:` patterns for argument variants);
  * shell writes (`echo > file`, `sed -i`) are not checked against
    `writable_roots`;
  * shell changes to the policy file or its directory are not recognised when
    they go through an interpreter or a script (`python -c "open(…,'w')"`,
    `node -e`, a shell script file), a path held in a variable or built by command
    substitution (`f=.jiuwenswarm/permissions.yaml; : > $f`), a `cd` through a
    variable, alias or function followed by a relative name, a wildcard that
    matches the name without spelling it (`rm .jiuwen*/*`,
    `find . -name '*.yaml' -delete`), a symlink created in the same command
    (the alias check only sees links that already exist), removal of an ancestor
    directory (`rm -rf ../project`), or `git clean` / `git stash -u` removing
    the file when it is untracked;
  * the policy file can also be rewritten by commands that write many files
    without naming it: `tar xf`, `unzip`, `git apply`, `patch`, `rsync -a src/ .`,
    `git reset --hard`, `git checkout -- .`, and interactive editors such as
    `vim` or `ex`. These can only replace the project's **own** denies (the
    project file cannot loosen the user's configuration), so the user's
    settings stay in force.
* **Failure handling:** any error inside the guard rail's checks refuses the call
  with `"policy": "policy_check_error"` and logs the exception; it never lets the
  call through unchecked. If the guard rail itself cannot be built, the Code
  adapter logs an ERROR and mounts a fail-closed guard instead, which refuses
  shell tools and every file tool (`"policy": "policy_guard_unavailable"`) with
  a message to check `config.yaml` and the log. The session stays usable for
  everything else. This was chosen over failing the whole agent build, which
  would lock the user out, and over dropping the guard, which would silently
  drop the protection. The network heuristic is not run at all when the
  effective level is `allow`, or `ask` interactively.
* **Time of check vs. time of use:** paths are resolved when the tool call is
  checked. A symlink swapped in between the check and the write (for example by
  a background shell job) is not caught.
* **Case-insensitive file systems:** the rail compares case-folded on a
  case-insensitive project root. agent-core's engine compares paths
  case-sensitively, so the **user's own** file-guard rules can still be sidestepped
  by a case variant on such a file system; that is an agent-core limitation
  outside this change.
* **OS-level isolation is not added.** No public extension point was found that
  wraps the command agent-core's shell tool executes (for `sandbox-exec` /
  `bwrap`) without changing agent-core, and JiuwenBox execution is already
  governed by `execution_provider_contract.py`. For hard isolation run headless
  jobs in a container without network, as the benchmark harness does.

## 7. Configuration reference

| Key | Default | Meaning |
|---|---|---|
| `permissions.shell_network` | `allow` | shell network policy for interactive sessions |
| `permissions.headless.shell_network` | `deny` | policy for headless runs (process CLI / implement driver); combined with the above by strictness |
| `permissions.project_policy.enabled` | `true` | read `<project>/.jiuwenswarm/permissions.yaml` |

## 8. How to confirm it is working

* Adapter log at startup: `[JiuwenSwarmCodeAdapter] ShellPolicyGuardRail create success (headless=True engine=False shell_network=allow)`.
* Every refusal logs `[ShellPolicyGuardRail] refused <tool>: policy=<policy> headless=<bool>`, and the tool result is a JSON object like the one in §3 with that `policy`.
* Ignored project entries log `[permissions.project] <path>: ignored (project files are deny-only; supported keys: ...)`.
* An unusable project file logs `[permissions.project] The project policy file <path> cannot be used (...)` once per change of the file.
* A guard that could not be built logs `[JiuwenSwarmCodeAdapter] ShellPolicyGuardRail create failed; shell and file tools are refused this session` at ERROR level.
* A guard error returns `"policy": "policy_check_error"` and logs
  `[ShellPolicyGuardRail] policy check error; call refused` with the traceback.
* With the engine on, a network command is decided by the rule
  `tiered_policy:rules:deny:rules[shell_network_policy]` (or `...:ask:...`). The
  rule targets the engine's `shell` category, so it applies to every tool in the
  effective `categories.shell` list. A skipped `ask` rule logs
  `[permissions.shell_network] 'ask' rule not added: the shell baseline is defaults deny`.

## 9. Limitations

* Detection is pattern-based (see §6); it is a guard against the common cases,
  not a network sandbox.
* Only Code mode (interactive and headless process CLI) enforces project files;
  other agents and the swarm/team member assembly ignore them. With the engine
  off, those modes do not enforce `shell_network` either (§3).
* Projects cannot ask for approval or grant anything; they can only deny.
* `shell_network: ask` without the permission engine is not enforced
  interactively. An engine `ask` rule loses to an earlier "always allow" the user
  granted for the same command, because agent-core checks `approval_overrides`
  before non-deny rules; only `deny` is unconditional.
