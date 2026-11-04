# Process CLI Session Queries

> **Goal**: Find full local Process CLI session IDs from a terminal and continue a task in a new process.
>
> **Chinese Version:** [中文版](../zh/进程式CLI会话查询.md)

---

## 1. List local sessions

After installing a JiuwenSwarm package containing this feature, run:

```cmd
jiuwenswarm-process --list-sessions
```

The command prints full IDs, last message times, modes, titles, and project directories, then exits. It includes both Agent and Team sessions owned by Process CLI, ordered by last message time descending and ID for ties. It does not start an Agent/Runtime, call a model, or write back session metadata.

Columns align by the terminal display width of the current page, with at least two spaces between columns and `会话ID` as the first header. Full IDs, titles, and project directories are preserved; lines may wrap when they exceed the terminal width.

The list covers all Process CLI sessions in the current data directory without automatic filtering by the terminal working directory. Other channels are excluded. Archived and ephemeral sessions follow the existing local catalog filters.

The default data directory is `%USERPROFILE%\.jiuwenswarm` on Windows. If configured, `JIUWENSWARM_DATA_DIR` overrides it. Queries and resumed tasks must use the same data directory.

## 2. Search and pagination

| Option | Value | Default and behavior |
| --- | --- | --- |
| `--list-sessions` | Flag | Prints one page; an empty result exits with 0 and an explanatory message. |
| `--latest-session` | Flag | Prints only the full ID of the most recently active session to stdout. No match reports to stderr and exits with 1. |
| `--search` | Up to 200 characters | Matches ID, title, project ID, project directory, or mode. All whitespace-separated terms must match, ignoring case. Available for both queries. |
| `--limit` | Integer, 1–200 | Page size; defaults to 20. List queries only. |
| `--offset` | Non-negative integer | Number of matches to skip; defaults to 0. List queries only. |

```cmd
jiuwenswarm-process --list-sessions --search "project name" --limit 10 --offset 0
jiuwenswarm-process --list-sessions --search "project name" --limit 10 --offset 10
jiuwenswarm-process --latest-session --search "project name"
```

Queries are standalone commands. Do not combine them with a prompt or task options such as `--session`, `--cwd`, `--mode`, `--output`, or `--timeout`. Configure those options when running the resumed task. Invalid arguments, conflicting selectors, and query modifiers without a selector exit with 2.

## 3. Resume a task

Run a task and wait until it exits:

```cmd
jiuwenswarm-process "Remember the passphrase BLUE_2026. Reply only with it." --mode agent.work.normal --timeout 120
```

Find its session:

```cmd
jiuwenswarm-process --list-sessions
jiuwenswarm-process --latest-session
```

Copy the full ID into a new command:

```cmd
jiuwenswarm-process "Reply only with the passphrase I asked you to remember." --session "REPLACE_WITH_FULL_SESSION_ID" --mode agent.work.normal --timeout 120
```

The new process should restore the conversation and reply with `BLUE_2026`. Do not use abbreviated IDs. Use the original mode when resuming a Team session.

CMD can capture the latest matching ID. Use `%S` in an interactive CMD terminal and `%%S` in a batch file:

```cmd
set "CLI_SESSION_ID="
for /f "delims=" %S in ('jiuwenswarm-process --latest-session --search "project name"') do @set "CLI_SESSION_ID=%S"
if defined CLI_SESSION_ID jiuwenswarm-process "Continue the previous task" --session "%CLI_SESSION_ID%" --mode agent.work.normal --timeout 120
```

An empty query skips the resumed task. For concurrent tasks, inspect the list to identify the intended session; the latest session is not necessarily the one created by a particular process.

## 4. Help and exit codes

```cmd
jiuwenswarm-process -h
jiuwenswarm-process --help
jiuwenswarm-process -help
```

All three spellings show the query options and query/resume examples, then exit. Immediately after a command has completed, check its exit code with a separate command:

```cmd
echo %ERRORLEVEL%
```

Success exits with 0. No latest matching session or a storage failure exits with 1. Invalid arguments exit with 2. Interruption exits with 130. A discovered ID identifies a session; verify the actual resumed task using its response, tool results, and exit status.

---

## Navigation

- [Documentation home](../README_EN.md)
- [Project home](../../README.md)
