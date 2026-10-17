/**
 * Prompt for the /autofix-pr command, built entirely in the TUI (like /init).
 *
 * A single prompt covers both forges; the agent detects which from the git
 * remote (Phase 0). GitHub reads remote CI and its logs through `gh`. GitCode's
 * pipeline status is read from the web-api v2 pipeline-check endpoint (WAF needs
 * browser headers; public repos need no token), but its logs are unreachable, so
 * GitCode diagnosis runs the project's checks locally.
 */

export type AutofixPrPlatform = "github" | "gitcode";

export interface BuildAutofixPrPromptArgs {
  /** PR number, URL or free text; empty means "infer from the current branch". */
  prArg?: string;
  /** Forge hint; when github/gitcode the agent may skip Phase 0 detection. Any
   *  other value (including empty) leaves detection to the agent. */
  platform?: string;
}

const AUTOFIX_PR_PROMPT_TEMPLATE = `# Autofix PR: Drive the Pull Request to Green

Fix the open pull request for the current branch until its checks pass and reviewer
requests are addressed. This works on both GitHub and GitCode remotes.

## Phase 0: Detect the Platform

Run \`git remote get-url origin\` and note the host:
- \`github.com\` → use the \`gh\` CLI for the PR steps below.
- \`gitcode.com\` → there is no \`gh\` CLI; use the REST calls shown in the GitCode branches.
  Derive \`<owner>/<repo>\` from that same remote URL.

Everything from Phase 3 onward is identical for both.

## Phase 1: Identify the PR

You fix only the PR for the branch you are ALREADY checked out on. A PR number below merely
disambiguates when the branch has more than one open PR; the PR it names must still be THIS
branch's PR (its \`head.ref\` equals \`git branch --show-current\`). If the number names a
different branch, or a fork you are not on, do NOT add a remote, fetch another fork, or patch
files in this repo — just read its status, report it, and hand back. Never pull someone
else's PR into the current repository.

If a PR number is provided below, use it. Otherwise find the open PR whose head branch is
the current branch (\`git branch --show-current\`):
- GitHub: \`gh pr view --json number,headRefName,state\` (or \`gh pr view <number>\`).
- GitCode (public repos need no token): find the PR AND its head commit sha — you will
  need that exact sha for the status check in Phase 2.
  - If a PR number is given, read the PR and take its head sha:

        curl -s "https://api.gitcode.com/api/v5/repos/<owner>/<repo>/pulls/<number>"
        # → use the JSON field  head.sha  as HEAD_SHA

  - Otherwise list open PRs and match the one whose \`head.ref\` is the current branch:

        curl -s "https://api.gitcode.com/api/v5/repos/<owner>/<repo>/pulls?state=open"

    Take that PR's \`head.sha\` as HEAD_SHA. When you are checked out on the PR branch this
    equals \`git rev-parse HEAD\`.

If there is no open PR for this branch, stop and say so — do not open one.

## Phase 2: Gather the Failure Signal

- GitHub (remote CI, logs reachable):
  1. \`gh pr checks <number>\` — list the checks and find the failing ones.
  2. For each failing check, read its log, e.g. \`gh run view <run-id> --log-failed\`.
     The log is the primary evidence — do not guess the cause from the test name alone.
  3. \`gh pr view <number> --comments\` — read review comments that request changes.
- GitCode (remote CI status readable, but its logs are NOT):
  1. Read the pipeline pass/fail for HEAD_SHA (the PR head sha from Phase 1). The status
     lives on a WAF-guarded host, so a bare request gets HTTP 418 — you MUST send
     browser-like headers. No token is needed for a public repo:

         curl -s "https://web-api.gitcode.com/api/v2/projects/pipeline-check/<owner>%2F<repo>/merge_requests/<number>/pipelines?type=report_pipeline&commit_id=<HEAD_SHA>&page=1&per_page=5" \\
           -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36" \\
           -H "Referer: https://gitcode.com/<owner>/<repo>/pull/<number>/check" \\
           -H "Origin: https://gitcode.com"

     Pass the exact HEAD_SHA as \`commit_id\`: omitting it returns the PR's WHOLE run history
     (old commits, mixed statuses) and a wrong sha returns nothing. Read \`content[].status\`
     (\`success\` / \`failed\` / \`running\`); if several rows come back, the most recent is the
     current one. \`running\` means the result is not in yet — rely on your local run below.
     An EMPTY result (this repo has no remote pipeline at all) is NOT a pass — it means there
     is no remote signal, so your local run below is the ONLY signal; run it and treat its
     failures as the checks to fix.
  2. Read the PR comments — they carry BOTH the reviewer feedback AND, on a repo with a CI
     bot, a results table that names each failing check with a link to its report:

         curl -s "https://api.gitcode.com/api/v5/repos/<owner>/<repo>/pulls/<number>/comments" -o pr_comments.json

     Parse that file with \`encoding='utf-8'\` — GitCode returns UTF-8 and a bare read on
     Windows dies with a cp1252 UnicodeDecodeError. In the comments look for a checks table
     (rows like \`UT测试 / 静态检查 … ✅ / ❌\`) with report links. For each FAILED check, fetch
     its linked report — these are usually public (e.g. a pytest-html report on OBS) — and
     read the actual failing tests and tracebacks from it. That report IS the "why", the
     equivalent of GitHub's \`--log-failed\`. Do not guess the cause from the check name alone.
  3. If no such report is reachable (the pipeline API itself gives only pass/fail, no log),
     fall back to running the project's own checks locally (\`pytest\`, \`npm test\`, the linter)
     and read the failure there.

If the checks pass (and, on GitCode, your local run is clean) and no comment requests a
change, report that the PR is already green and stop without committing.

## Phase 3: Diagnose and Fix

Find the root cause in the source and fix it there. Keep the change minimal and scoped to
the failure; do not refactor unrelated code. Removing a comment that described the very
bug you just fixed is fine.

## Phase 4: Verify Locally

Run the failing check's fast local equivalent (the failing test, the linter) and confirm
it now passes before pushing. On GitCode this local run IS your only reliable check — the
remote log is unreachable, so never push a "fix" you have not reproduced-then-fixed locally.

## Phase 5: Commit and Push

Run these steps in order. Step 3 is not optional — these two trailers are the only thing
that tells this commit apart from a hand-written one, and they are the easiest step to
forget. The commit keeps the human's own author identity (do NOT change the author); the
\`Co-authored-by\` trailer is what marks the machine's involvement, mirroring how Claude Code
attributes its commits.

1. Stage only the files you changed to fix the failure.
2. Commit with a subject that names the actual defect, plus BOTH trailers:

       git commit -m "fix: total() should sum all items, not skip the first" \\
                  --trailer "Auto-Fixed-By: jiuwenswarm /autofix-pr" \\
                  --trailer "Co-authored-by: jiuwenswarm-autofix <noreply@openjiuwen.com>"

3. Verify both trailers landed: run \`git log -1 --format=%B\` and read the output. If either
   \`Auto-Fixed-By:\` or \`Co-authored-by:\` is missing, repair it before pushing (re-pass only
   the missing one; --amend is idempotent for a trailer already present):

       git commit --amend --trailer "Auto-Fixed-By: jiuwenswarm /autofix-pr" \\
                          --trailer "Co-authored-by: jiuwenswarm-autofix <noreply@openjiuwen.com>"

   Amending here is safe and expected — nothing has been pushed yet.
4. Push to the existing PR branch.

Then report what you changed and the resulting check status.

## Hard Rules

- Operate only on the PR for the branch you are checked out on. NEVER add a remote for, or
  fetch, someone else's fork to "acquire" a PR's code, and never patch a PR you are not on.
  If you cannot reach the code by already being on its branch, report the status and hand
  back — do not modify the repository you happen to be sitting in.
- Fix the root cause in the source. NEVER make CI pass by deleting or weakening tests,
  loosening assertions, marking tests skipped, or editing CI/workflow configuration.
- Every commit you make must carry BOTH the \`Auto-Fixed-By: jiuwenswarm /autofix-pr\` and the
  \`Co-authored-by: jiuwenswarm-autofix <noreply@openjiuwen.com>\` trailers. Keep the human's
  own author identity — do not \`--author\` the commit; the co-author trailer, not the author
  line, is what marks the machine. A correct code change that cannot be told apart from a
  hand-written commit is still a failed run — verify both trailers before you push (Phase 5
  step 3).
- Never force-push and never rewrite already-published history. Amending your own commit
  before it is pushed is not rewriting published history and is allowed.
- Stop and hand the PR back to the human — push nothing — when the correct fix is
  ambiguous, requires a product decision, or the failure looks environmental/flaky
  rather than a real code defect. Explain what you found and what you need.
- Do not post comments on the PR unless you are blocked and are handing it back.
`;

/** Build the /autofix-pr prompt, appending optional platform / PR hints. */
export function buildAutofixPrPrompt(args: BuildAutofixPrPromptArgs = {}): string {
  const tail: string[] = [];
  const hint = (args.platform ?? "").trim().toLowerCase();
  if (hint === "github" || hint === "gitcode") {
    tail.push(`Detected platform: ${hint} (you may skip Phase 0 detection).`);
  }
  const target = (args.prArg ?? "").trim();
  if (target) {
    tail.push(`PR number: ${target}`);
  }
  let prompt = AUTOFIX_PR_PROMPT_TEMPLATE;
  if (tail.length > 0) {
    prompt += "\n\n" + tail.join("\n") + "\n";
  }
  return prompt;
}
