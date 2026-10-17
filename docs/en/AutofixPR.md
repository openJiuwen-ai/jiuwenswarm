# Auto-fix PR

`/autofix-pr` hands the "PR checks failed → find the cause → fix → push" loop to the Agent, on both GitHub and GitCode.

---

## Background

### What it solves

When a PR's checks fail, the usual routine looks like this:

1. Open the PR page and see which check went red
2. Dig through the pipeline report or CI comments to find which test failed, and where
3. Go back to the local checkout and fix it
4. Commit, push, wait for the pipeline to re-run
5. Still red → back to step 1

Steps 1, 2 and 5 involve no creative work at all — they are pure "read status → locate → re-run" — yet every round needs a person waiting on it. `/autofix-pr` gives those steps to the Agent: it reads the check status itself, gets the concrete failure evidence, finds the root cause, makes a minimal fix, then commits and pushes back to the PR branch.

**How it differs from neighbouring features:**

| Command | Direction | Changes code? |
|------|------|------|
| `/review` | Read a PR → give review comments | ❌ read-only |
| Auto Harness `issue_fix` | issue → new PR | ✅ |
| **`/autofix-pr`** | **existing PR → drive it to green** | ✅ |

**Typical uses:**

- 🔴 **CI failing on something small** — a boundary condition, a missing import, a typo: failures with one obviously correct fix
- 💬 **Acting on review comments** — turning a reviewer's concrete request into code
- 🔁 **Red after a fix** — the first attempt didn't fully solve it and another round is needed

---

## 1. Prerequisites

| Requirement | Notes |
|------|------|
| **code mode** | The command edits files and runs git, so it must run in `code` mode. Other modes are rejected with a prompt to switch |
| **An open PR for the current branch** | With no argument the PR is inferred from the current branch; you can also pass a PR number or URL |
| **`gh` for GitHub** | Installed and authenticated via `gh auth login` (same as `/review` — no new dependency) |
| **No token needed for GitCode** | Reading pipeline status and comments on public repositories requires no authentication |

> ⚠️ After installing `gh`, **restart the TUI** — otherwise the running process still has the old PATH and will report that `gh` cannot be found.

---

## 2. Quick start

Switch to code mode, then run it:

```
/mode code
/autofix-pr
```

<!-- Screenshot: running /autofix-pr in the TUI -->
![Running /autofix-pr](../zh/assets/autofix-pr-start.png)

The Agent detects the forge, identifies the PR, reads the failing checks, locates the root cause, makes a minimal fix, then commits and pushes back to the PR branch. It reports the diagnosis only after the run completes — the "Root cause / Fix" lines at the end of the screenshot below are the failure evidence it read and the fix it applied.

<!-- Screenshot: fix committed and pushed, incl. the closing Root cause / Fix summary -->
![Fixed and pushed](../zh/assets/autofix-pr-result.png)

---

## 3. Usage

```
/autofix-pr [PR number or URL]
```

| Form | Behaviour |
|------|------|
| `/autofix-pr` | Infers the open PR from the **current branch** (most common) |
| `/autofix-pr 123` | Targets a specific PR number |
| `/autofix-pr https://gitcode.com/{owner}/{repo}/pull/123` | Takes a full PR link |

---

## 4. What it does

| Phase | Action |
|------|------|
| **Phase 0** | Reads `git remote` to detect the forge (GitHub / GitCode) — you never specify it |
| **Phase 1** | Identifies the open PR for the current branch and runs safety checks (see [§7](#7-safety-constraints)) |
| **Phase 2** | Reads failing checks and review comments to get **concrete** failure evidence — which test, which error — not just "it's red" |
| **Phase 3** | Finds the root cause and makes a minimal fix |
| **Phase 4** | Runs the project's own checks locally to confirm the fix |
| **Phase 5** | Commits (with traceability trailers) and pushes back to the PR branch |

---

## 5. Forge differences

The command detects the forge automatically; this section documents its internal behaviour and is not something you need to know to use it.

### GitHub

- Reads red/green from `gh pr view --json statusCheckRollup`
- Pulls failure logs directly with `gh run view --log-failed`

### GitCode

GitCode has no `gh`, so REST endpoints are used instead, with a few non-obvious details:

- **Pipeline status** comes from the `web-api` `pipeline-check` endpoint (which needs the PR head's full 40-character sha)
- **That endpoint returns status only, no logs.** The failure details live in the CI bot's PR comment — a table of checks where each failure links to its report. The Agent follows those links to reach the failing tests and tracebacks
- **Personal repositories have no pipeline by default**, so the endpoint returns empty. **Empty is never read as "green"** — the Agent falls back to running the project's own checks locally

---

## 6. Watching a PR (`--watch`)

A single run fixes one round: after the push, if checks are still running or go red again, you have to run it again by hand. `--watch` automates that too — it re-checks the PR every few minutes and, as long as it hasn't passed, sends another `/autofix-pr` round, until the PR is **green / merged / closed**.

```
/autofix-pr --watch                    # watch the current branch's PR
/autofix-pr 123 --watch                # watch a specific PR
/autofix-pr --watch --interval 3       # re-check every 3 minutes
/autofix-pr --stop                     # stop a running watch
```

### How it runs

- **Runs one round immediately**, then re-checks every `--interval`. Each round is a full `/autofix-pr` shown in the session, so you see what it read and what it changed.
- **No overlap**: if the previous round (or any tool) is still running, that re-check is skipped and resumes once idle.
- **Waits out disconnects**: it never fires while the WebSocket is down — it resumes on the next re-check after reconnecting, and the missed tick doesn't count against the round budget.

### Flags

| Flag | Notes |
|------|------|
| `--watch` | Turns on continuous watch mode |
| `--interval <minutes>` | Re-check cadence, default **10 minutes**; accepts fractions (`0.5` = 30 s), floored at **10 s** |
| `--stop` | Stops a running watch (works in any mode — no need to be in code mode) |

### When it stops

| Stop reason | How it's decided |
|------|------|
| **Green / merged / closed** | The command re-checks it itself via CLI/API (`gh` on GitHub, REST on GitCode). It does **not** take the Agent's word for it — the Agent saying "it's fixed" isn't enough; a signal must actually be read |
| **12 rounds reached** (fuse) | Stops after 12 rounds still not passing, so it can't spin unbounded |
| **Manual `--stop`** | You can cancel at any time |

> "Can't read the status" is **never** treated as a stop signal (network down, no `gh`, empty response all count as "unknown"). Stopping a red PR whose status we couldn't read would silently abandon it — so unknown keeps watching, with the 12-round fuse as the backstop.

### Guardrails before a watch starts

- **code mode required**, same as a single round.
- **Clean working tree required**: a watch commits and pushes unattended, so it refuses to start if **tracked** files have uncommitted changes — commit or stash first. Untracked files (build output, `__pycache__`, etc.) don't count and won't block it.
- **A PR must be resolvable**: it won't start if no open PR is found for the current branch — pass `/autofix-pr <number> --watch` to specify one.

> ⚠️ **GitCode personal repositories usually have no pipeline**, so the endpoint returns empty → "unknown" → the "green" signal never arrives, and only the 12-round fuse can stop it. Each round the Agent still falls back to running the project's checks locally, but the watch loop itself won't end on its own — watch the round cap on such repos, or `--stop` manually.

### Auto-approve for the run (optional)

Starting a watch (and a single round too) first asks once: **auto-approve all commands for this run?**

- Choose "Auto-approve (this run)": no per-command permission prompts during the run — suited to unattended watching.
- Choose "Ask each time": every command is approved manually as usual.

The grant is **scoped to this run only**: it clears the moment the single round ends, the watch stops, or you Ctrl+C — the next run asks again. It never becomes a standing bypass.

---

## 7. Safety constraints

Because this command changes code and pushes automatically, several rules are built in and cannot be bypassed:

| Rule | Notes |
|------|------|
| **Only the PR for the current branch** | Given someone else's fork PR number, the Agent reports read-only and hands back — it will **not** add a remote, fetch, or patch that fork into your repository |
| **Never fake green** | Making checks pass by deleting or weakening tests, editing CI config, or force-pushing is forbidden |
| **Fix the implementation** | Fixes belong in implementation code; tests and CI configuration are left alone |

> 💡 The first rule matters most: even with uncommitted work in your tree, handing it someone else's fork PR number will not contaminate your repository.

### Traceability

Automated commits carry two trailers so machine fixes stay distinguishable from human ones:

```
Auto-Fixed-By: jiuwenswarm /autofix-pr
Co-authored-by: jiuwenswarm-autofix <noreply@openjiuwen.com>
```

The commit **author remains you** (`--author` is never rewritten), so CLA signing and contribution stats are unaffected.

<!-- Screenshot: trailers in git log / co-author shown on the forge -->
![Traceability markers in the commit](../zh/assets/autofix-pr-trailer.png)

> GitHub renders `jiuwenswarm-autofix` as a co-author; GitCode does not render the co-author avatar, but the trailers are in the commit message itself and remain visible in `git log` and the commit detail page.

---

## 8. FAQ

**Q: It says code mode is required.**
Run `/mode code` first, then retry.

**Q: It cannot find `gh`.**
Install `gh`, run `gh auth login`, then **restart the TUI** — the running process still holds the old PATH.

**Q: My repository is on GitCode. Do I need a token?**
Not for public repositories. Reading pipeline status and PR comments needs no authentication.

**Q: Could it delete tests just to turn CI green?**
No — that is one of the hard rules. If the problem cannot be solved by changing the implementation, it reports that honestly instead of faking green.

**Q: Does one run always fix it?**
It fixes one round per run — if checks are still failing after the push, run it again. To have it keep going until the PR passes, use `--watch` (see [§6](#6-watching-a-pr---watch)).

**Q: Does `--watch` run forever?**
No. It stops on green / merged / closed, or after at most 12 rounds (the fuse). You can also `/autofix-pr --stop` at any time.

**Q: The watch says the working tree has uncommitted changes.**
A watch changes code and pushes unattended, so it requires a clean tree. Commit or stash your tracked-file changes first, then start it.

---

## 9. Known limitations

- **`--watch` stopping depends on being able to read the PR status.** GitCode personal repositories have no pipeline and the endpoint returns empty, so status reads as "unknown", the "green" signal never arrives, and only the 12-round fuse can stop it (see [§6](#6-watching-a-pr---watch)).
- **GitCode failure details depend on the CI bot's comment format**, which is an openJiuwen convention and may not apply to arbitrary GitCode repositories. Without that comment, the Agent falls back to running checks locally, as designed.

---

## Related

- [Slash Commands Reference](SlashCommands.md)
- [Tool Permissions & Security](ToolPermissionsSecurity.md)
- [Modes](Modes.md)
