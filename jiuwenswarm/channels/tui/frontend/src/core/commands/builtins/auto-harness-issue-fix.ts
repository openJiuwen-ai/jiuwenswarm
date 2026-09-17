import { parseArgs } from "../helpers.js";

export type AutoHarnessIssueStageProgress = {
  stage: string;
  name?: string;
  status: string;
  messages?: string[];
};

export type AutoHarnessIssueProgress = {
  summary?: string;
  stages?: AutoHarnessIssueStageProgress[];
  completed_stages?: string[];
  current_stage?: string;
  failed_stage?: string;
  last_message?: string;
  last_error?: string;
  failure_code?: string;
  pr_url?: string;
};

export type IssueFixTaskStatus = {
  error?: string;
  task_id?: string;
  status?: string;
  progress?: AutoHarnessIssueProgress;
};

export type IssueFixWatchItem = {
  issue: number;
  taskId?: string;
  status: string;
  reason?: string;
  lastMessage?: string;
  currentStage?: string;
  failedStage?: string;
  failureCode?: string;
  prUrl?: string;
};

export type IssueWatchArgs = {
  repo: string;
  labels: string[];
  max_issues: number;
  dry_run: string;
  comment_on_start: boolean;
  pipeline: string;
  issue_numbers: number[];
  max_auto_difficulty: string;
  concurrency: number;
};

export type IssueFixArgs = {
  repo: string;
  issue_numbers: number[];
  dry_run: string;
  comment_on_start: boolean;
  pipeline: string;
  max_auto_difficulty: string;
  concurrency: number;
};

export type IssueWatchResult = {
  fetched?: number;
  started?: Array<{
    number?: number;
    issue?: number;
    task_id?: string;
    status?: string;
    title?: string;
    difficulty?: { level?: string; score?: number; reasons?: string[] };
  }>;
  skipped?: Array<{
    issue?: number;
    reason?: string;
    status?: string;
    human_label?: string;
    difficulty?: { level?: string; score?: number; reasons?: string[] };
  }>;
  reconciled?: Array<{ number?: number; status?: string; pr_url?: string }>;
};

type PipelineNameResolver = (name: string) => string;

// Repo options: friendly aliases → full owner/repo values
export const REPO_OPTIONS = {
  jiuwenswarm: { full: "openJiuwen/jiuwenswarm", desc: "jiuwenswarm 代码仓" },
  agent_core: { full: "openJiuwen/agent-core", desc: "agent-core 代码仓" },
};
export const REPO_ALIASES = Object.keys(REPO_OPTIONS);
export const REPO_FULL_VALUES = Object.values(REPO_OPTIONS).map(v => v.full);

// Resolve friendly alias to full repo value
export function resolveRepoName(name: string): string {
  if (name in REPO_OPTIONS) return REPO_OPTIONS[name as keyof typeof REPO_OPTIONS].full;
  // Already a full value (passed directly) — accept it
  if (REPO_FULL_VALUES.includes(name)) return name;
  return name; // Unknown value, pass through
}

export const ISSUE_FIX_WATCH_INTERVAL_MS = 5000;
export const ISSUE_FIX_WATCH_MAX_POLLS = 720;
// Difficulty options: user-selectable levels
export const DIFFICULTY_OPTIONS = {
  low: { desc: "简单问题，单文件修改" },
  medium: { desc: "中等难度，需跨文件协调" },
  high: { desc: "复杂问题，涉及架构或多方依赖" },
};
export const DIFFICULTY_SELECTABLE_VALUES = Object.keys(DIFFICULTY_OPTIONS);
export const ISSUE_DIFFICULTY_VALUES = new Set(["low", "medium", "high", "unclear"]);

const ISSUE_FIX_TERMINAL_STATUSES = new Set([
  "success",
  "failed",
  "cancelled",
  "skipped",
  "needs_human",
]);

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function latestStageWithStatus(
  progress: AutoHarnessIssueProgress | undefined,
  status: string,
): string {
  if (!progress?.stages) return "";
  const stage = progress.stages.find((item) => item.status === status);
  return stage?.stage || "";
}

export function issueFixTaskStatusToWatchItem(
  issue: number,
  taskId: string | undefined,
  task: IssueFixTaskStatus | undefined,
  fallbackStatus = "queued",
  reason = "",
): IssueFixWatchItem {
  if (!taskId) {
    return {
      issue,
      status: fallbackStatus,
      reason,
      lastMessage: reason,
    };
  }
  if (!task || task.error) {
    return {
      issue,
      taskId,
      status: "unknown",
      reason: task?.error || reason || "任务状态暂不可用",
      lastMessage: task?.error || reason || "任务状态暂不可用",
    };
  }
  const progress = task.progress;
  const status = task.status || fallbackStatus;
  return {
    issue,
    taskId,
    status: status === "pending" ? "queued" : status,
    currentStage: progress?.current_stage || latestStageWithStatus(progress, "running"),
    failedStage: progress?.failed_stage || latestStageWithStatus(progress, "failed"),
    failureCode: progress?.failure_code || "",
    prUrl: progress?.pr_url || "",
    lastMessage: progress?.last_message || progress?.last_error || reason,
  };
}

export function formatIssueFixStatusLine(item: IssueFixWatchItem): string {
  const issue = `#${item.issue}`.padEnd(7, " ");
  const status = (item.status || "unknown").padEnd(9, " ");
  let stageText = "";
  if (item.status === "running") {
    stageText = item.currentStage ? `${item.currentStage} …` : "starting …";
  } else if (item.status === "failed") {
    stageText = item.failedStage ? `${item.failedStage} ×` : "failed";
  } else if (item.status === "success") {
    stageText = "done ✓";
  } else if (item.status === "queued") {
    stageText = "";
  } else if (item.status === "skipped") {
    stageText = "skipped";
  } else if (item.status === "needs_human") {
    stageText = "needs-human";
  } else {
    stageText = item.currentStage || item.reason || "";
  }
  const fail = item.failureCode ? `   cause: ${item.failureCode}` : "";
  const pr = item.prUrl ? `   PR: ${item.prUrl}` : "";
  const last = item.lastMessage ? `   last: ${item.lastMessage}` : "";
  return `${issue} ${status}${stageText}${fail}${pr}${last}`;
}

export function formatIssueFixStatusBlock(items: IssueFixWatchItem[]): string {
  return items
    .slice()
    .sort((a, b) => a.issue - b.issue)
    .map(formatIssueFixStatusLine)
    .join("\n");
}

// Issue fix stages with weights for progress calculation
const ISSUE_FIX_STAGES = [
  { name: "assess", weight: 0, skip: true },
  { name: "plan", weight: 0, skip: true },
  { name: "implement", weight: 40 },
  { name: "verify", weight: 30 },
  { name: "commit", weight: 15 },
  { name: "publish_pr", weight: 15 },
];

// Calculate progress percentage from stage results
export function calculateStageProgress(stages: Array<{ stage: string; status: string }>): number {
  const effectiveStages = ISSUE_FIX_STAGES.filter(s => !s.skip);
  const totalWeight = effectiveStages.reduce((sum, s) => sum + s.weight, 0);

  let progressPercent = 0;
  for (const stage of stages) {
    const stageDef = ISSUE_FIX_STAGES.find(s => s.name === stage.stage);
    if (!stageDef || stageDef.skip) continue;

    if (stage.status === "success") {
      progressPercent += stageDef.weight;
    } else if (stage.status === "running") {
      progressPercent += stageDef.weight * 0.5;
    }
  }

  return Math.round(progressPercent / totalWeight * 100);
}

// Format progress bar (40 blocks for 40-char column)
function formatProgressBar(percent: number): string {
  const filled = Math.round(percent * 29 / 100);
  const empty = 29 - filled;
  return "█".repeat(filled) + "░".repeat(empty);
}

// Table format for issue list display
export interface IssueListRow {
  issue: number;
  status: string;
  stage?: string;
  progress?: number;
  details: string;
  prUrl?: string;
}

// Issue matrix row type
export interface IssueMatrixRow {
  number: number;
  title: string;
  body?: string;
  labels?: string[];
  difficulty: string;
  updated_at?: string;
}

// Issue matrix result type
export type IssueMatrixResult = {
  error?: string;
  owner?: string;
  repo?: string;
  total?: number;
  added?: number;
  removed?: number;
  updated?: number;
  difficulty_counts?: Record<string, number>;
  matrix?: IssueMatrixRow[];
  has_more?: boolean;
  page?: number;
  per_page?: number;
  cached?: boolean;
  labels_filter?: string[];
};

// Helper: calculate visual width (CJK chars = 2, ASCII = 1)
function matrixVisualWidth(str: string): number {
  let width = 0;
  for (let i = 0; i < str.length; i++) {
    const code = str.codePointAt(i) || 0;
    // Box drawing characters (U+2500-U+257F) are width 1
    if (code >= 0x2500 && code <= 0x257F) {
      width += 1;
    } else if (
      (code >= 0x4E00 && code <= 0x9FFF) || // CJK Unified Ideographs
      (code >= 0x3000 && code <= 0x303F) || // CJK Symbols
      (code >= 0xFF00 && code <= 0xFFEF) || // Halfwidth/Fullwidth
      code >= 0x1F000 // Emoji
    ) {
      width += 2;
    } else {
      width += 1;
    }
    if (code > 0xFFFF) i++; // Skip low surrogate
  }
  return width;
}

// Helper: pad string to visual width
function matrixPadRight(str: string, targetWidth: number): string {
  const currentWidth = matrixVisualWidth(str);
  if (currentWidth >= targetWidth) return str;
  return str + " ".repeat(targetWidth - currentWidth);
}

// Helper: truncate string to visual width
function matrixTruncateToWidth(str: string, maxWidth: number): string {
  let result = "";
  let width = 0;
  for (let i = 0; i < str.length; i++) {
    const char = str[i];
    const code = str.codePointAt(i) || 0;
    let charWidth = 1;
    if (
      (code >= 0x4E00 && code <= 0x9FFF) ||
      (code >= 0x3000 && code <= 0x303F) ||
      (code >= 0xFF00 && code <= 0xFFEF) ||
      code >= 0x1F000
    ) {
      charWidth = 2;
    }
    if (code > 0xFFFF) i++; // Skip low surrogate

    if (width + charWidth <= maxWidth) {
      result += char;
      width += charWidth;
    } else {
      break;
    }
  }
  return result;
}

// Helper: wrap text to fixed visual width, returns array of lines
// Handles embedded newlines in the input string
function matrixWrapText(str: string, maxWidth: number): string[] {
  const result: string[] = [];
  // First split by existing newlines in the string
  const paragraphs = str.split(/\r?\n/);
  for (const paragraph of paragraphs) {
    if (paragraph.length === 0) {
      result.push("");
      continue;
    }
    let remaining = paragraph;
    while (matrixVisualWidth(remaining) > maxWidth) {
      const line = matrixTruncateToWidth(remaining, maxWidth);
      result.push(line);
      remaining = remaining.slice(line.length);
    }
    if (remaining.length > 0) {
      result.push(remaining);
    }
  }
  return result.length > 0 ? result : [""];
}

// Format issue matrix for display (table format with wrapping)
export function formatIssueMatrix(result: IssueMatrixResult): string {
  if (result.error) {
    return `\n矩阵刷新失败: ${result.error}\n`;
  }

  // Fixed column widths (visual width)
  const COL_ISSUE = 8;      // "#1270  "
  const COL_TITLE = 60;     // Title column
  const COL_DIFF = 8;       // "low    "
  const COL_DATE = 12;      // "2026-06-11 "

  const lines: string[] = [];
  const page = result.page || 1;
  const cached = result.cached ? "(缓存)" : "";
  const labelsInfo = result.labels_filter && result.labels_filter.length > 0 ? ` [${result.labels_filter.join(",")}]` : "";
  const pageInfo = result.total && result.per_page ? ` 第${page}/${Math.ceil(result.total / result.per_page)}页` : "";
  lines.push(`\nIssues 信息如下：`);

  // Table header
  const headerIssue = matrixPadRight("Issue", COL_ISSUE);
  const headerTitle = matrixPadRight("名称", COL_TITLE);
  const headerDiff = matrixPadRight("难度", COL_DIFF);
  const headerDate = matrixPadRight("更新时间", COL_DATE);
  lines.push(`┌${"─".repeat(COL_ISSUE)}┬${"─".repeat(COL_TITLE)}┬${"─".repeat(COL_DIFF)}┬${"─".repeat(COL_DATE)}┐`);
  lines.push(`│${headerIssue}│${headerTitle}│${headerDiff}│${headerDate}│`);
  lines.push(`├${"─".repeat(COL_ISSUE)}┼${"─".repeat(COL_TITLE)}┼${"─".repeat(COL_DIFF)}┼${"─".repeat(COL_DATE)}┤`);

  // Issue rows with wrapping (each issue separated by horizontal line)
  const matrix = result.matrix || [];
  const separator = `├${"─".repeat(COL_ISSUE)}┼${"─".repeat(COL_TITLE)}┼${"─".repeat(COL_DIFF)}┼${"─".repeat(COL_DATE)}┤`;

  for (let idx = 0; idx < matrix.length; idx++) {
    const issue = matrix[idx];
    const issueNum = matrixPadRight(`${issue.number}`, COL_ISSUE);
    const titleLines = matrixWrapText(issue.title || "", COL_TITLE);
    const difficulty = matrixPadRight(issue.difficulty || "unclear", COL_DIFF);
    const date = matrixPadRight(issue.updated_at?.substring(0, 10) || "-", COL_DATE);

    // First line: all columns
    const firstTitle = matrixPadRight(titleLines[0], COL_TITLE);
    lines.push(`│${issueNum}│${firstTitle}│${difficulty}│${date}│`);

    // Subsequent lines: only title column (if wrapped)
    for (let i = 1; i < titleLines.length; i++) {
      const wrapTitle = matrixPadRight(titleLines[i], COL_TITLE);
      lines.push(`│${matrixPadRight("", COL_ISSUE)}│${wrapTitle}│${matrixPadRight("", COL_DIFF)}│${matrixPadRight("", COL_DATE)}│`);
    }

    // Add separator between issues (not after the last one)
    if (idx < matrix.length - 1) {
      lines.push(separator);
    }
  }

  // Table footer
  lines.push(`└${"─".repeat(COL_ISSUE)}┴${"─".repeat(COL_TITLE)}┴${"─".repeat(COL_DIFF)}┴${"─".repeat(COL_DATE)}┘`);

  // Statistics (after table)
  const counts = result.difficulty_counts || {};
  const diffStats = Object.entries(counts)
    .map(([k, v]) => `${k}(${v})`)
    .join(" ");
  lines.push(`\n数据汇总: ${result.owner}/${result.repo}${labelsInfo} (${result.total} issues)${pageInfo}${cached}`);
  lines.push(`难度分布: ${diffStats}`);
  if (!result.cached && (result.added || result.removed || result.updated)) {
    lines.push(`本次更新: 新增 ${result.added || 0} | 更新 ${result.updated || 0} | 移除 ${result.removed || 0}`);
  }

  // Tips
  if (result.has_more) {
    lines.push("");
    const nextPage = (result.page || 1) + 1;
    lines.push(`💡显示 50 条issue，使用 --page ${nextPage} 查看更多`);
  }
  if (result.cached) {
    lines.push("💡 使用 --force-refresh 强制更新");
  }
  lines.push("");
  lines.push("💡 /auto-harness issue fix 编号 执行修复");

  return lines.join("\n");
}

// Map status to shorter display name
function displayStatus(status: string): string {
  const statusMap: Record<string, string> = {
    "task_created": "created",
    "queued": "queued",
    "pending": "pending",
    "running": "running",
    "success": "success",
    "failed": "failed",
    "cancelled": "cancelled",
    "skipped": "skipped",
    "needs_human": "needs_human",
    "pr_created": "pr_ok",
  };
  return statusMap[status] || status.substring(0, 8);
}

export function formatIssueTable(rows: IssueListRow[]): string {
  if (rows.length === 0) {
    return "\n暂无 GitCode issue 处理记录\n";
  }

  const lines: string[] = [];
  // Fixed column widths
  const COL_ISSUE = 8;      // "#1416  "
  const COL_STATUS = 10;    // "created  "
  const COL_STAGE = 14;     // "implement    "
  const COL_PROGRESS = 30;  // "██░░░░░░░░  "
  const COL_DETAILS = 50;   // Details column

  // Header
  lines.push(`┌${"─".repeat(COL_ISSUE)}┬${"─".repeat(COL_STATUS)}┬${"─".repeat(COL_STAGE)}┬${"─".repeat(COL_PROGRESS)}┬${"─".repeat(COL_DETAILS)}┐`);
  lines.push(`│${matrixPadRight("Issue", COL_ISSUE)}│${matrixPadRight("Status", COL_STATUS)}│${matrixPadRight("Stage", COL_STAGE)}│${matrixPadRight("Progress", COL_PROGRESS)}│${matrixPadRight("Details", COL_DETAILS)}│`);
  lines.push(`├${"─".repeat(COL_ISSUE)}┼${"─".repeat(COL_STATUS)}┼${"─".repeat(COL_STAGE)}┼${"─".repeat(COL_PROGRESS)}┼${"─".repeat(COL_DETAILS)}┤`);

  // Rows with separators between each row
  const separator = `├${"─".repeat(COL_ISSUE)}┼${"─".repeat(COL_STATUS)}┼${"─".repeat(COL_STAGE)}┼${"─".repeat(COL_PROGRESS)}┼${"─".repeat(COL_DETAILS)}┤`;
  const displayRows = rows.slice(-20);
  for (let idx = 0; idx < displayRows.length; idx++) {
    const row = displayRows[idx];
    const issueNum = matrixPadRight(`#${row.issue}`, COL_ISSUE);
    const status = matrixPadRight(displayStatus(row.status || "unknown"), COL_STATUS);
    const stage = matrixPadRight(row.stage || "-", COL_STAGE);
    const progress = matrixPadRight(formatProgressBar(row.progress || 0), COL_PROGRESS);
    let details = row.details || "";
    if (row.prUrl) {
      details = `PR: ${row.prUrl}`;
    }
    // Wrap details to fit column width (support multiline)
    const detailLines = matrixWrapText(details, COL_DETAILS);
    const firstDetail = matrixPadRight(detailLines[0], COL_DETAILS);
    lines.push(`│${issueNum}│${status}│${stage}│${progress}│${firstDetail}│`);
    for (let i = 1; i < detailLines.length; i++) {
      const wrapDetail = matrixPadRight(detailLines[i], COL_DETAILS);
      lines.push(`│${matrixPadRight("", COL_ISSUE)}│${matrixPadRight("", COL_STATUS)}│${matrixPadRight("", COL_STAGE)}│${matrixPadRight("", COL_PROGRESS)}│${wrapDetail}│`);
    }
    // Add separator between rows (not after the last one)
    if (idx < displayRows.length - 1) {
      lines.push(separator);
    }
  }

  // Footer
  lines.push(`└${"─".repeat(COL_ISSUE)}┴${"─".repeat(COL_STATUS)}┴${"─".repeat(COL_STAGE)}┴${"─".repeat(COL_PROGRESS)}┴${"─".repeat(COL_DETAILS)}┘`);
  lines.push("");
  lines.push("💡使用 /auto-harness schedule logs <task id> 查看详细日志信息");
  return "\n" + lines.join("\n");
}

export function isIssueFixWatchDone(items: IssueFixWatchItem[]): boolean {
  return items.every((item) => ISSUE_FIX_TERMINAL_STATUSES.has(item.status));
}

export function issueStartIntervalSeconds(concurrency: number): number {
  if (concurrency <= 1) return 8;
  if (concurrency === 2) return 2;
  return 1.2;
}

export function parseIssueNumbers(value: string): number[] {
  return value
    .split(/[,\s，]+/)
    .map((v) => parseInt(v.trim(), 10))
    .filter((v, index, arr) => Number.isFinite(v) && v > 0 && arr.indexOf(v) === index);
}

export function parseIssueWatchArgs(
  args: string,
  resolvePipelineName: PipelineNameResolver,
): IssueWatchArgs {
  const parts = parseArgs(args);
  let repo = "";
  let labels = ["auto-harness"];
  let max_issues = 1;
  let dry_run = "";  // 空值触发交互
  let comment_on_start = false;
  let pipeline = "meta_evolve_pipeline";
  let issue_numbers: number[] = [];
  let max_auto_difficulty = "";
  let concurrency = 1;

  let i = 0;
  while (i < parts.length) {
    const part = parts[i];
    if (part === "--repo" && i + 1 < parts.length) {
      repo = parts[i + 1];
      i += 2;
    } else if ((part === "--label" || part === "--labels") && i + 1 < parts.length) {
      labels = parts[i + 1]
        .split(",")
        .map((v) => v.trim())
        .filter(Boolean);
      i += 2;
    } else if ((part === "--max" || part === "--max-issues") && i + 1 < parts.length) {
      const parsed = parseInt(parts[i + 1], 10);
      max_issues = Number.isFinite(parsed) ? parsed : 1;
      i += 2;
    } else if ((part === "--issue" || part === "--issues") && i + 1 < parts.length) {
      issue_numbers = parseIssueNumbers(parts[i + 1]);
      i += 2;
    } else if (part === "--pipeline" && i + 1 < parts.length) {
      pipeline = resolvePipelineName(parts[i + 1]);
      i += 2;
    } else if ((part === "--max-difficulty" || part === "--difficulty") && i + 1 < parts.length) {
      max_auto_difficulty = parts[i + 1].toLowerCase();
      i += 2;
    } else if (part === "--concurrency" && i + 1 < parts.length) {
      const parsed = parseInt(parts[i + 1], 10);
      concurrency = Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
      i += 2;
    } else if (part === "--dry-run") {
      dry_run = "yes";
      i += 1;
    } else if (part === "--comment") {
      comment_on_start = true;
      i += 1;
    } else {
      i += 1;
    }
  }

  if (issue_numbers.length > 0) {
    max_issues = issue_numbers.length;
  }

  return {
    repo,
    labels,
    max_issues,
    dry_run,
    comment_on_start,
    pipeline,
    issue_numbers,
    max_auto_difficulty,
    concurrency,
  };
}

export function parseIssueFixArgs(
  args: string,
  resolvePipelineName: PipelineNameResolver,
): IssueFixArgs {
  const parts = parseArgs(args);
  let repo = "";
  let dry_run = "no";
  let comment_on_start = false;
  let pipeline = "meta_evolve_pipeline";
  let max_auto_difficulty = "medium";
  let concurrency = 1;
  const numberParts: string[] = [];

  let i = 0;
  while (i < parts.length) {
    const part = parts[i];
    if (part === "--repo" && i + 1 < parts.length) {
      repo = parts[i + 1];
      i += 2;
    } else if (part === "--pipeline" && i + 1 < parts.length) {
      pipeline = resolvePipelineName(parts[i + 1]);
      i += 2;
    } else if ((part === "--max-difficulty" || part === "--difficulty") && i + 1 < parts.length) {
      max_auto_difficulty = parts[i + 1].toLowerCase();
      i += 2;
    } else if (part === "--concurrency" && i + 1 < parts.length) {
      const parsed = parseInt(parts[i + 1], 10);
      concurrency = Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
      i += 2;
    } else if (part === "--dry-run") {
      dry_run = "yes";
      i += 1;
    } else if (part === "--comment") {
      comment_on_start = true;
      i += 1;
    } else {
      numberParts.push(part);
      i += 1;
    }
  }

  return {
    repo,
    issue_numbers: parseIssueNumbers(numberParts.join(",")),
    dry_run,
    comment_on_start,
    pipeline,
    max_auto_difficulty,
    concurrency,
  };
}

export function formatIssueWatchResult(result: IssueWatchResult): string {
  const lines = ["\nGitCode issue 处理完成", "━━━━━━━━━━━━━━━━━━━━━━"];
  lines.push(`拉取数量: ${result.fetched ?? 0}`);
  lines.push(`新建任务: ${result.started?.length ?? 0}`);
  for (const item of result.started || []) {
    const difficulty = item.difficulty?.level ? ` difficulty=${item.difficulty.level}` : "";
    lines.push(
      `  #${item.number ?? item.issue}: ${item.task_id || item.status || "dry-run"}${difficulty}`,
    );
  }
  if (result.reconciled && result.reconciled.length > 0) {
    lines.push(`状态更新: ${result.reconciled.length}`);
    for (const item of result.reconciled) {
      lines.push(`  #${item.number}: ${item.status}${item.pr_url ? ` ${item.pr_url}` : ""}`);
    }
  }
  if (result.skipped && result.skipped.length > 0) {
    lines.push(`跳过: ${result.skipped.length}`);
    for (const item of result.skipped.slice(0, 10)) {
      const difficulty = item.difficulty?.level ? ` difficulty=${item.difficulty.level}` : "";
      const human = item.human_label ? ` label=${item.human_label}` : "";
      lines.push(
        `  #${item.issue}: ${item.reason || item.status || "skipped"}${difficulty}${human}`,
      );
    }
  }
  lines.push("━━━━━━━━━━━━━━━━━━━━━━");
  return lines.join("\n");
}
