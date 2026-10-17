import { execFile } from "node:child_process";
import type { PreferredLanguage } from "../types.js";

/**
 * Code-side "is this PR done?" check for the /autofix-pr watch, ported from the
 * Python pr_status module so the whole feature stays TUI-side.
 *
 * A watch re-runs the autofix prompt every few minutes; this module answers the
 * question that lets it *stop*: has the PR been merged/closed, or are its checks
 * green? The agent reports that too, but a watch loop must not end itself on the
 * model's word alone — hence a plain CLI/HTTP check next to the prompt.
 *
 * Two forges, two signals (both verified against live repos):
 * - GitHub: `gh pr view --json state,statusCheckRollup`, the same CLI the prompt
 *   drives, so auth is whatever the user already set up.
 * - GitCode: PR state from the public v5 API; red/green from the internal v2
 *   pipeline-check endpoint on web-api.gitcode.com, which needs the PR head sha
 *   and browser-looking headers (a bare request gets 418 from the WAF). Personal
 *   repos have no pipeline: v2 returns empty, reported as `unknown`.
 *
 * Every failure mode (network down, no `gh`, unparseable payload) collapses to
 * `unknown`, which never stops a watch. Stopping on a check we could not perform
 * would silently abandon a still-red PR; the max-rounds cap bounds the loop.
 */

const GITCODE_V5 = "https://api.gitcode.com/api/v5";
const GITCODE_V2 = "https://web-api.gitcode.com/api/v2";

// The WAF in front of web-api.gitcode.com answers non-browser clients with 418.
const BROWSER_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

const HTTP_TIMEOUT_MS = 20_000;
const CMD_TIMEOUT_MS = 60_000;

export type ChecksVerdict = "success" | "failed" | "running" | "unknown";
export type PrState = "open" | "merged" | "closed" | "unknown";

export interface PrStatus {
  state: PrState;
  checks: ChecksVerdict;
  /** Only ever true on a signal actually read: merged, closed, or green checks. */
  shouldStop: boolean;
  reason: string;
}

export interface PrContext {
  repo: string; // owner/repo
  prNumber: string;
  platform: string; // "github" | "gitcode" | ""
  /** repo and prNumber both resolved. */
  isComplete: boolean;
}

/** Pick a user-visible string by UI language (watch reasons are shown verbatim). */
const msg = (lang: PreferredLanguage, zh: string, en: string): string => (lang === "zh" ? zh : en);

const stop = (state: PrState, checks: ChecksVerdict, reason: string): PrStatus => ({
  state,
  checks,
  shouldStop: true,
  reason,
});

const keepWatching = (state: PrState, checks: ChecksVerdict, reason: string): PrStatus => ({
  state,
  checks,
  shouldStop: false,
  reason,
});

// ---------------------------------------------------------------------------
// Status check: does this watch stop now?
// ---------------------------------------------------------------------------

export async function checkPrStatus(
  repo: string,
  prNumber: string,
  platform = "",
  lang: PreferredLanguage = "zh",
): Promise<PrStatus> {
  if (!repo || !repo.includes("/") || !String(prNumber).trim()) {
    return keepWatching(
      "unknown",
      "unknown",
      msg(lang, `PR 元信息不完整（repo=${repo} pr=${prNumber}）`, `Incomplete PR metadata (repo=${repo} pr=${prNumber})`),
    );
  }
  const forge = platform.trim().toLowerCase();
  if (forge === "github") return checkGithub(repo, String(prNumber), lang);
  if (forge === "gitcode") return checkGitcode(repo, String(prNumber), lang);

  // Unspecified: this project lives on GitCode, so try it first and fall back.
  const s = await checkGitcode(repo, String(prNumber), lang);
  if (s.state !== "unknown") return s;
  return checkGithub(repo, String(prNumber), lang);
}

async function checkGitcode(repo: string, prNumber: string, lang: PreferredLanguage): Promise<PrStatus> {
  const [owner, name] = splitRepo(repo);
  const detail = await getJson(`${GITCODE_V5}/repos/${owner}/${name}/pulls/${prNumber}`, {
    Accept: "application/json",
    "User-Agent": "jiuwenswarm-auto-harness",
  });
  if (!detail || typeof detail !== "object") {
    return keepWatching("unknown", "unknown", msg(lang, "GitCode PR 详情读取失败", "Failed to read GitCode PR details"));
  }
  const d = detail as Record<string, unknown>;
  const state = String(d.state ?? "").toLowerCase();
  if (d.merged === true || d.merged_at) {
    return stop("merged", "unknown", msg(lang, `PR ${prNumber} 已合并`, `PR ${prNumber} merged`));
  }
  if (state && state !== "open") {
    const merged = state === "merged";
    return stop(
      merged ? "merged" : "closed",
      "unknown",
      msg(lang, `PR ${prNumber} 已${merged ? "合并" : "关闭"}`, `PR ${prNumber} ${merged ? "merged" : "closed"}`),
    );
  }
  const head = (d.head ?? {}) as Record<string, unknown>;
  const headSha = String(head.sha ?? "");
  if (!headSha) {
    return keepWatching(
      "open",
      "unknown",
      msg(lang, "读不到 PR head sha，无法查流水线", "Could not read PR head sha; cannot check pipeline"),
    );
  }
  const checks = await gitcodePipelineStatus(owner, name, prNumber, headSha);
  if (checks === "success") {
    return stop(
      "open",
      "success",
      msg(lang, `PR ${prNumber} 流水线已通过（${headSha.slice(0, 8)}）`, `PR ${prNumber} pipeline passed (${headSha.slice(0, 8)})`),
    );
  }
  if (checks === "unknown") {
    // Personal repos have no pipeline: keep watching, the prompt falls back to
    // running the project's own checks locally.
    return keepWatching(
      "open",
      "unknown",
      msg(lang, "GitCode 无可读流水线信号，继续本地校验", "No readable GitCode pipeline signal; continuing with local checks"),
    );
  }
  return keepWatching("open", checks, msg(lang, `PR ${prNumber} 流水线 ${checks}`, `PR ${prNumber} pipeline ${checks}`));
}

async function gitcodePipelineStatus(
  owner: string,
  name: string,
  prNumber: string,
  headSha: string,
): Promise<ChecksVerdict> {
  const url =
    `${GITCODE_V2}/projects/pipeline-check/${owner}%2F${name}` +
    `/merge_requests/${prNumber}/pipelines` +
    `?type=report_pipeline&commit_id=${headSha}&page=1&per_page=20`;
  const body = await getJson(url, {
    "User-Agent": BROWSER_UA,
    Referer: `https://gitcode.com/${owner}/${name}/pull/${prNumber}`,
    Origin: "https://gitcode.com",
  });
  if (!body || typeof body !== "object") return "unknown";
  const content = (body as Record<string, unknown>).content;
  if (!Array.isArray(content) || content.length === 0) return "unknown";
  const latest = content[0] as Record<string, unknown>;
  if (!latest || typeof latest !== "object") return "unknown";
  // status_with_code_quality folds code-quality into the verdict; prefer it.
  const raw = String(latest.status_with_code_quality ?? latest.status ?? "").toLowerCase();
  if (raw === "success") return "success";
  if (["running", "pending", "created", "waiting"].includes(raw)) return "running";
  if (raw) return "failed";
  return "unknown";
}

async function checkGithub(repo: string, prNumber: string, lang: PreferredLanguage): Promise<PrStatus> {
  const payload = await ghJson(["pr", "view", prNumber, "--repo", repo, "--json", "state,statusCheckRollup"]);
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return keepWatching("unknown", "unknown", msg(lang, "gh 不可用或读取 PR 失败", "gh unavailable or failed to read PR"));
  }
  const p = payload as Record<string, unknown>;
  const state = String(p.state ?? "").toLowerCase();
  if (state === "merged") return stop("merged", "unknown", msg(lang, `PR ${prNumber} 已合并`, `PR ${prNumber} merged`));
  if (state === "closed") return stop("closed", "unknown", msg(lang, `PR ${prNumber} 已关闭`, `PR ${prNumber} closed`));
  const checks = githubRollupStatus(p.statusCheckRollup);
  if (checks === "success") return stop("open", "success", msg(lang, `PR ${prNumber} 检查已全绿`, `PR ${prNumber} checks all green`));
  if (checks === "unknown") {
    return keepWatching(
      "open",
      "unknown",
      msg(lang, "GitHub 无可读检查信号，继续本地校验", "No readable GitHub check signal; continuing with local checks"),
    );
  }
  return keepWatching("open", checks, msg(lang, `PR ${prNumber} 检查 ${checks}`, `PR ${prNumber} checks ${checks}`));
}

function githubRollupStatus(rollup: unknown): ChecksVerdict {
  if (!Array.isArray(rollup) || rollup.length === 0) return "unknown";
  let seenRunning = false;
  for (const check of rollup) {
    if (!check || typeof check !== "object") continue;
    const c = check as Record<string, unknown>;
    const status = String(c.status ?? "").toUpperCase();
    if (status && status !== "COMPLETED") {
      seenRunning = true;
      continue;
    }
    const conclusion = String(c.conclusion ?? c.state ?? "").toUpperCase();
    if (["SUCCESS", "NEUTRAL", "SKIPPED"].includes(conclusion)) continue;
    if (["", "PENDING", "EXPECTED"].includes(conclusion)) {
      seenRunning = true;
      continue;
    }
    return "failed";
  }
  return seenRunning ? "running" : "success";
}

// ---------------------------------------------------------------------------
// PR context: which PR is this watch about?
// ---------------------------------------------------------------------------

/**
 * Whether the checkout has no uncommitted changes to *tracked* files. A watch
 * auto-commits and pushes unattended, so it must refuse to start when tracked
 * files are modified/staged — otherwise those changes could be swept into a
 * commit or acted on. Untracked files are deliberately ignored: build output,
 * agent history, __pycache__, .gitignore etc. are present in almost every real
 * checkout and the autofix agent only ever stages the files it changed (never
 * `git add .`), so they are not a contamination risk — counting them would make
 * a watch refuse to start almost everywhere.
 *
 * Returns true only when `git status --porcelain --untracked-files=no` is empty;
 * any git failure returns false (refuse), since runCmd yields "" on failure too.
 */
export async function isWorkingTreeClean(projectDir: string): Promise<boolean> {
  // Confirm a real git repo first: runCmd returns "" on failure too, so without
  // this a git error would masquerade as an empty (clean) status.
  const inRepo = await runCmd("git", ["-C", projectDir, "rev-parse", "--is-inside-work-tree"]);
  if (inRepo !== "true") return false;
  const out = await runCmd("git", ["-C", projectDir, "status", "--porcelain", "--untracked-files=no"]);
  return out === "";
}

export async function resolvePrContext(projectDir: string, prArg = ""): Promise<PrContext> {
  const fromUrl = parsePrUrl(prArg);
  if (fromUrl.isComplete) return fromUrl;

  const candidates = await remoteRepos(projectDir);
  if (candidates.length === 0) return emptyContext();

  const prNumber = prNumberFromArg(prArg);
  if (prNumber) {
    for (const [platform, repo] of candidates) {
      if (await prExists(repo, prNumber, platform)) {
        return { repo, prNumber, platform, isComplete: true };
      }
    }
    // Keep the number even when no remote owns it; a wrong repo yields "unknown",
    // which keeps the watch running rather than ending it on a bad reading.
    const [platform, repo] = candidates[0];
    return { repo, prNumber, platform, isComplete: Boolean(repo && prNumber) };
  }

  const branch = await git(projectDir, "rev-parse", "--abbrev-ref", "HEAD");
  if (branch && branch !== "HEAD") {
    for (const [platform, repo] of candidates) {
      const found = await findOpenPrForBranch(repo, branch, platform);
      if (found) return { repo, prNumber: found, platform, isComplete: true };
    }
  }
  const [platform, repo] = candidates[0];
  return { repo, prNumber: "", platform, isComplete: false };
}

async function remoteRepos(projectDir: string): Promise<Array<[string, string]>> {
  // Upstream first: in a fork workflow the PR is opened against upstream.
  const names = (await git(projectDir, "remote")).split(/\s+/).filter(Boolean);
  const ordered = [
    ...["upstream", "origin"].filter((n) => names.includes(n)),
    ...names.filter((n) => n !== "upstream" && n !== "origin"),
  ];
  const out: Array<[string, string]> = [];
  for (const nm of ordered) {
    const [platform, repo] = parseRemote(await git(projectDir, "remote", "get-url", nm));
    if (repo && !out.some(([, r]) => r === repo)) out.push([platform, repo]);
  }
  return out;
}

function parseRemote(remoteUrl: string): [string, string] {
  const url = remoteUrl.trim();
  if (!url) return ["", ""];
  let platform = "";
  if (url.includes("gitcode.com")) platform = "gitcode";
  else if (url.includes("github.com")) platform = "github";
  // Both scp-style (git@host:owner/repo.git) and https end in owner/repo[.git].
  let path = url.startsWith("git@")
    ? (url.split(":").pop() ?? "")
    : (url.split("://")[1] ?? "").split("/").slice(1).join("/");
  path = path.replace(/^\/+|\/+$/g, "");
  if (path.endsWith(".git")) path = path.slice(0, -4);
  const parts = path.split("/").filter(Boolean);
  const repo = parts.length >= 2 ? parts.slice(-2).join("/") : "";
  return [platform, repo];
}

function parsePrUrl(prArg: string): PrContext {
  const arg = (prArg || "").trim().replace(/\/+$/, "");
  if (!arg.includes("://")) return emptyContext();
  const [platform] = parseRemote(arg);
  // Path shape: <owner>/<repo>/<pull|pulls|merge_requests>/<number>
  const segments = (arg.split("://")[1] ?? "").split("/").filter(Boolean).slice(1);
  if (segments.length < 4 || !/^\d+$/.test(segments[segments.length - 1])) return emptyContext();
  const kind = segments[segments.length - 2].toLowerCase();
  if (!["pull", "pulls", "merge_requests", "merge_request"].includes(kind)) return emptyContext();
  const repo = segments.slice(-4, -2).join("/");
  return { repo, prNumber: segments[segments.length - 1], platform, isComplete: Boolean(repo) };
}

function prNumberFromArg(prArg: string): string {
  const arg = (prArg || "").trim().replace(/\/+$/, "");
  if (!arg) return "";
  if (/^\d+$/.test(arg)) return arg;
  const tail = arg.split("/").pop() ?? "";
  return /^\d+$/.test(tail) ? tail : "";
}

async function prExists(repo: string, prNumber: string, platform: string): Promise<boolean> {
  if (platform === "github") {
    const payload = await ghJson(["pr", "view", prNumber, "--repo", repo, "--json", "number"]);
    return Boolean(payload && typeof payload === "object" && !Array.isArray(payload));
  }
  const [owner, name] = splitRepo(repo);
  const detail = await getJson(`${GITCODE_V5}/repos/${owner}/${name}/pulls/${prNumber}`, {
    Accept: "application/json",
    "User-Agent": "jiuwenswarm-auto-harness",
  });
  if (!detail || typeof detail !== "object") return false;
  const d = detail as Record<string, unknown>;
  return Boolean(d.number || d.id);
}

async function findOpenPrForBranch(repo: string, branch: string, platform: string): Promise<string> {
  if (platform === "github") {
    const payload = await ghJson(["pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number"]);
    if (Array.isArray(payload) && payload.length > 0) {
      return String((payload[0] as Record<string, unknown>).number ?? "");
    }
    return "";
  }
  const [owner, name] = splitRepo(repo);
  const pulls = await getJson(`${GITCODE_V5}/repos/${owner}/${name}/pulls?state=open&per_page=100`, {
    Accept: "application/json",
    "User-Agent": "jiuwenswarm-auto-harness",
  });
  if (!Array.isArray(pulls)) return "";
  for (const pull of pulls) {
    if (pull && typeof pull === "object") {
      const head = ((pull as Record<string, unknown>).head ?? {}) as Record<string, unknown>;
      if (String(head.ref ?? "") === branch) return String((pull as Record<string, unknown>).number ?? "");
    }
  }
  return "";
}

// ---------------------------------------------------------------------------
// Low-level helpers — every failure collapses to a benign empty result.
// ---------------------------------------------------------------------------

function splitRepo(repo: string): [string, string] {
  const i = repo.indexOf("/");
  return i < 0 ? [repo, ""] : [repo.slice(0, i), repo.slice(i + 1)];
}

function emptyContext(): PrContext {
  return { repo: "", prNumber: "", platform: "", isComplete: false };
}

async function getJson(url: string, headers: Record<string, string>): Promise<unknown> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), HTTP_TIMEOUT_MS);
    try {
      const res = await fetch(url, { headers, signal: controller.signal });
      if (res.status !== 200) return null;
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  } catch {
    // network, DNS, malformed JSON, WAF — all just "unknown".
    return null;
  }
}

/** Run `git -C <dir> …`, returning trimmed stdout or "" on any failure. */
function git(projectDir: string, ...args: string[]): Promise<string> {
  return runCmd("git", ["-C", projectDir, ...args]);
}

/** Run `gh …` and parse its JSON output, or null when that is not possible. */
async function ghJson(args: string[]): Promise<unknown> {
  const out = await runCmd("gh", args);
  if (!out) return null;
  try {
    return JSON.parse(out);
  } catch {
    return null;
  }
}

function runCmd(cmd: string, args: string[]): Promise<string> {
  return new Promise((resolve) => {
    execFile(cmd, args, { timeout: CMD_TIMEOUT_MS, maxBuffer: 1_048_576 }, (err, stdout) => {
      if (err) return resolve("");
      resolve(String(stdout).trim());
    });
  });
}
