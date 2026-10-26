// jiuwenswarm/cli/src/core/commands/builtins/auto-harness.ts

import { addError, addInfo, parseArgs } from "../helpers.js";
import { CommandKind, type SlashCommand, type CommandContext } from "../types.js";
import {
  ISSUE_DIFFICULTY_VALUES,
  formatIssueWatchResult,
  issueStartIntervalSeconds,
  parseIssueFixArgs,
  REPO_OPTIONS,
  resolveRepoName,
  type IssueListRow,
  formatIssueTable,
  calculateStageProgress,
  formatIssueMatrix,
  type IssueMatrixResult,
} from "./auto-harness-issue-fix.js";

// Pipeline options: friendly display names → backend values
export const PIPELINE_DISPLAY_NAMES = {
  optimize_expert_harness: { backend: "extended_evolve_pipeline", display: "生成扩展包", desc: "生成本地 harness package" },
  optimize_meta_harness: { backend: "meta_evolve_pipeline", display: "提交优化代码", desc: "提交 PR（需配置 git）" },
};
export const PIPELINE_DISPLAY_KEYS = Object.keys(PIPELINE_DISPLAY_NAMES);

// Backend pipeline values (for validation after resolving)
export const PIPELINE_BACKEND_VALUES = Object.values(PIPELINE_DISPLAY_NAMES).map(v => v.backend);

// Resolve friendly display name to backend value
export function resolvePipelineName(name: string): string {
  if (name in PIPELINE_DISPLAY_NAMES) return PIPELINE_DISPLAY_NAMES[name as keyof typeof PIPELINE_DISPLAY_NAMES].backend;
  // Already a backend value (passed directly) — accept it
  if (PIPELINE_BACKEND_VALUES.includes(name)) return name;
  return name;
}

// Aliases for backward compat with completion/validation logic
export const PIPELINE_OPTIONS = PIPELINE_DISPLAY_NAMES;
export const PIPELINE_VALUES = PIPELINE_DISPLAY_KEYS;

// Get display-friendly label for a pipeline (accepts both friendly and backend names)
export function pipelineDisplayLabel(name: string): string {
  // Friendly name directly
  if (name in PIPELINE_DISPLAY_NAMES) {
    return `${name} (${PIPELINE_DISPLAY_NAMES[name as keyof typeof PIPELINE_DISPLAY_NAMES].display})`;
  }
  // Backend name — reverse lookup
  for (const [key, val] of Object.entries(PIPELINE_DISPLAY_NAMES)) {
    if (val.backend === name) return `${key} (${val.display})`;
  }
  return name;
}

// Interval options
export const INTERVAL_OPTIONS = {
  "1": { desc: "每 1 小时执行" },
  "2": { desc: "每 2 小时执行" },
  "4": { desc: "每 4 小时执行" },
  "8": { desc: "每 8 小时执行" },
  "12": { desc: "每 12 小时执行" },
  "24": { desc: "每 24 小时执行（每天）" },
};
export const INTERVAL_VALUES = Object.keys(INTERVAL_OPTIONS);

// Flag options with descriptions (used by app-screen.ts for autocomplete descriptions)
export const FLAG_OPTIONS = {
  "--interval": { desc: "执行间隔（小时）", alias: "-i" },
  "-i": { desc: "执行间隔（小时）", alias: "--interval" },
  "--pipeline": { desc: "Pipeline 类型", alias: "-p" },
  "-p": { desc: "Pipeline 类型", alias: "--pipeline" },
};

// Pipeline completion helper - returns completions with existing args preserved
// The completion value becomes the FULL argument string, so we must preserve existing args
function getPipelineCompletions(_partial: string, parts: string[]): string[] {
  const lastPart = parts[parts.length - 1] || "";

  // Helper to build completion preserving existing arguments
  // Remove the last incomplete part and add the completion
  const buildCompletion = (completion: string): string => {
    // Keep all parts except the last one (which is being completed)
    const existingParts = parts.slice(0, -1);
    return [...existingParts, completion].join(" ");
  };

  // If --pipeline is typed (last part is the flag), suggest flag + value combinations
  if (lastPart === "--pipeline") {
    return PIPELINE_VALUES.map(v => buildCompletion(`--pipeline ${v}`));
  }

  // If -p is typed (last part is the short flag), suggest flag + value combinations
  if (lastPart === "-p") {
    return PIPELINE_VALUES.map(v => buildCompletion(`-p ${v}`));
  }

  // If we're typing a value after --pipeline/-p (flag exists, now typing value)
  const pipelineIndex = parts.indexOf("--pipeline");
  const shortPipelineIndex = parts.indexOf("-p");
  if (pipelineIndex !== -1 || shortPipelineIndex !== -1) {
    // Check if we're at the value position (right after the flag)
    const flagPos = Math.max(pipelineIndex, shortPipelineIndex);
    // parts.length === flagPos + 2 means we're at the value slot (flag at flagPos, value at flagPos+1)
    if (parts.length === flagPos + 2 && !lastPart.startsWith("-")) {
      // Return completions with flag preserved: "--pipeline <value>"
      const flag = pipelineIndex !== -1 ? "--pipeline" : "-p";
      return PIPELINE_VALUES
        .filter(v => v.startsWith(lastPart.toLowerCase()))
        .map(v => buildCompletion(`${flag} ${v}`));
    }
  }

  // If typing a flag prefix (e.g., "--p"), suggest only flag + value combinations
  if (lastPart.startsWith("-")) {
    const hasPipeline = parts.includes("--pipeline") || parts.includes("-p");
    if (!hasPipeline) {
      const completions: string[] = [];
      // Add matching flag + value combinations (skip bare flag)
      const matchingFlags = ["--pipeline", "-p"].filter(f => f.startsWith(lastPart));
      for (const f of matchingFlags) {
        // Only add flag + value combinations, not bare flag
        for (const v of PIPELINE_VALUES) {
          completions.push(buildCompletion(`${f} ${v}`));
        }
      }
      return completions;
    }
  }

  return [];
}

// Helper functions

type AutoHarnessStageProgress = {
  stage: string;
  name?: string;
  status: string;
  messages?: string[];
};

type AutoHarnessProgress = {
  summary?: string;
  stages?: AutoHarnessStageProgress[];
  completed_stages?: string[];
  current_stage?: string;
  failed_stage?: string;
  last_message?: string;
  last_error?: string;
  failure_code?: string;
  pr_url?: string;
};

function stageProgressIcon(status?: string): string {
  switch (status) {
    case "success":
      return "✅";
    case "failed":
      return "❌";
    case "running":
      return "🔄";
    case "pending":
      return "⏳";
    default:
      return "·";
  }
}

function formatProgressLine(progress?: AutoHarnessProgress): string {
  if (!progress) return "";
  const summary = progress.summary || "";
  if (!summary) return "";
  return `   进度: ${summary}`;
}

function formatProgressBlock(progress?: AutoHarnessProgress): string[] {
  if (!progress || !progress.stages || progress.stages.length === 0) {
    return [];
  }
  const lines = ["", "阶段进度:"];
  for (const stage of progress.stages) {
    const name = stage.name || stage.stage;
    const icon = stageProgressIcon(stage.status);
    lines.push(`  ${icon} ${name}: ${stage.status}`);
  }
  if (progress.last_message) {
    lines.push(`  最近: ${progress.last_message}`);
  }
  return lines;
}

function parseScheduleStartArgs(args: string): { interval: number; pipeline: string; query: string } {
  const parts = parseArgs(args);

  let interval = -1;
  let pipeline = "";
  let queryParts: string[] = [];
  let i = 0;

  while (i < parts.length) {
    if (parts[i] === "--interval" || parts[i] === "-i") {
      i++;
      if (i < parts.length) {
        const parsed = parseInt(parts[i], 10);
        interval = isNaN(parsed) ? -1 : parsed;
        i++;
      }
    } else if (parts[i] === "--pipeline" || parts[i] === "-p") {
      i++;
      if (i < parts.length && !parts[i].startsWith("-")) {
        pipeline = parts[i];
        i++;
      }
    } else {
      queryParts.push(parts[i]);
      i++;
    }
  }

  return {
    interval,
    pipeline,
    query: queryParts.join(" "),
  };
}

function parseRunArgs(args: string): { pipeline: string; query: string } {
  const parts = parseArgs(args);

  let pipeline = "";
  let queryParts: string[] = [];
  let i = 0;

  while (i < parts.length) {
    if (parts[i] === "--pipeline" || parts[i] === "-p") {
      i++;
      if (i < parts.length && !parts[i].startsWith("-")) {
        pipeline = parts[i];
        i++;
      }
    } else {
      queryParts.push(parts[i]);
      i++;
    }
  }

  return {
    pipeline,
    query: queryParts.join(" "),
  };
}

// Schedule subcommands

const scheduleStartCommand: SlashCommand = {
  name: "start",
  description: "创建定时 auto_harness 任务",
  usage: "/auto-harness schedule start --interval <hours> [--pipeline <pipeline>] <query>",
  example: "/auto-harness schedule start --interval 4 --pipeline extended_evolve_pipeline 优化上下文压缩能力",
  kind: CommandKind.BUILT_IN,
  takesArgs: true,
  completion: (_ctx, partial) => {
    const parts = partial.trim().split(/\s+/).filter(Boolean);
    const lastPart = parts[parts.length - 1] || "";

    // Helper to build completion preserving existing arguments
    const buildCompletion = (completion: string): string => {
      const existingParts = parts.slice(0, -1);
      return [...existingParts, completion].join(" ");
    };

    // If --interval is typed, suggest --interval + value
    if (lastPart === "--interval") {
      return ["1", "2", "4", "8", "12", "24"].map(v => buildCompletion(`--interval ${v}`));
    }

    // If -i is typed, suggest -i + value
    if (lastPart === "-i") {
      return ["1", "2", "4", "8", "12", "24"].map(v => buildCompletion(`-i ${v}`));
    }

    // If we're typing a value after --interval/-i
    const intervalIndex = parts.indexOf("--interval");
    const shortIntervalIndex = parts.indexOf("-i");
    const intervalValues = ["1", "2", "4", "8", "12", "24"];
    if (intervalIndex !== -1 || shortIntervalIndex !== -1) {
      const flagPos = Math.max(intervalIndex, shortIntervalIndex);
      if (parts.length === flagPos + 1 && !lastPart.startsWith("-") && /^\d/.test(lastPart)) {
        const flag = intervalIndex !== -1 ? "--interval" : "-i";
        return intervalValues
          .filter(v => v.startsWith(lastPart))
          .map(v => buildCompletion(`${flag} ${v}`));
      }
    }

    // Check pipeline completions (handles --pipeline/-p and values)
    const pipelineCompletions = getPipelineCompletions(partial, parts);
    if (pipelineCompletions.length > 0) return pipelineCompletions;

    // Otherwise suggest flags that aren't already used
    const usedFlags: string[] = [];
    if (parts.includes("--interval") || parts.includes("-i")) usedFlags.push("--interval", "-i");
    if (parts.includes("--pipeline") || parts.includes("-p")) usedFlags.push("--pipeline", "-p");

    if (lastPart.startsWith("-")) {
      const completions: string[] = [];
      // For interval flags: show only flag + value combinations (skip bare flag)
      if (!usedFlags.includes("--interval") && "--interval".startsWith(lastPart)) {
        for (const v of intervalValues) {
          completions.push(buildCompletion(`--interval ${v}`));
        }
      }
      if (!usedFlags.includes("-i") && "-i".startsWith(lastPart)) {
        for (const v of intervalValues) {
          completions.push(buildCompletion(`-i ${v}`));
        }
      }
      // For pipeline flags: show only flag + value combinations (skip bare flag)
      if (!usedFlags.includes("--pipeline") && "--pipeline".startsWith(lastPart)) {
        for (const v of PIPELINE_VALUES) {
          completions.push(buildCompletion(`--pipeline ${v}`));
        }
      }
      if (!usedFlags.includes("-p") && "-p".startsWith(lastPart)) {
        for (const v of PIPELINE_VALUES) {
          completions.push(buildCompletion(`-p ${v}`));
        }
      }
      return completions;
    }

    return [];
  },
  action: async (ctx, args) => {
    const parsed = parseScheduleStartArgs(args);

    if (parsed.interval < 1) {
      const hint = parsed.interval === 0 ? "间隔不能为 0，请设置至少 1 小时\n示例: /auto-harness schedule start --interval 4 优化上下文压缩能力" : parsed.interval === -1 ? "请提供有效的 --interval 数值（小时）\n示例: /auto-harness schedule start --interval 4 优化上下文压缩能力" : "间隔必须大于 0 小时";
      ctx.addItem(
        addError(ctx.sessionId, `${hint}`)
      );
      return;
    }

    if (!parsed.query) {
      ctx.addItem(
        addError(ctx.sessionId, "请提供执行目标 query\n示例: /auto-harness schedule start --interval 4 优化上下文压缩能力")
      );
      return;
    }

    // Ask user to select pipeline if not specified
    let pipeline = parsed.pipeline;
    if (!pipeline) {
      try {
        const [answer] = await ctx.askQuestions([
          {
            header: "Pipeline",
            question: "请选择 Pipeline 类型:",
            options: [
              { label: "optimize_expert_harness", description: PIPELINE_DISPLAY_NAMES.optimize_expert_harness.desc },
              { label: "optimize_meta_harness", description: PIPELINE_DISPLAY_NAMES.optimize_meta_harness.desc },
            ],
          },
        ]);
        pipeline = answer.selected_options[0];
      } catch {
        // User cancelled
        ctx.addItem(addInfo(ctx.sessionId, "已取消创建任务"));
        return;
      }
    }

    // Validate pipeline value (accept both friendly and backend names)
    const resolvedPipeline = resolvePipelineName(pipeline);
    if (!PIPELINE_BACKEND_VALUES.includes(resolvedPipeline)) {
      ctx.addItem(
        addError(ctx.sessionId, `无效的 pipeline: ${pipeline}\n可选值: ${PIPELINE_DISPLAY_KEYS.join(", ")}`)
      );
      return;
    }

    // For optimize_meta_harness, check git config
    if (resolvedPipeline === "meta_evolve_pipeline") {
      const configCheck = await ctx.request<{ valid: boolean; missing_fields?: Array<{ id: string; prompt: string }> }>("schedule.check_config", {});

      const missingFields = configCheck.missing_fields as Array<{ id: string; prompt: string }> | undefined;
      if (missingFields && missingFields.length > 0) {
        const missingList = missingFields.map(f => `  - ${f.prompt}`).join("\n");
        ctx.addItem(
          addInfo(ctx.sessionId, `optimize_meta_harness 需要配置 git 信息:\n${missingList}\n\n请使用 /config edit 配置这些字段后重试`)
        );
        return;
      }
    }

    // Ask user whether to run immediately
    let run_immediately = false;
    {
      try {
        const [answer] = await ctx.askQuestions([
          {
            header: "立即执行",
            question: "是否立即执行一次任务？（如选否，则等待首个周期后再执行）",
            options: [{ label: "立即执行" }, { label: "等待周期" }],
          },
        ]);
        run_immediately = answer.selected_options[0] === "立即执行";
      } catch {
        // User cancelled or timeout, proceed without immediate execution
        run_immediately = false;
      }
    }

    // Create the scheduled task
    const result = await ctx.request<{ error?: string; task_id?: string; next_run_time?: string }>("schedule.create", {
      interval_hours: parsed.interval,
      query: parsed.query,
      pipeline: resolvedPipeline,
      run_immediately: run_immediately,
    });

    if (result.error) {
      ctx.addItem(
        addError(ctx.sessionId, `创建失败: ${result.error}`)
      );
      return;
    }

    ctx.addItem(
      addInfo(
        ctx.sessionId,
        `\n⏰ 定时任务已创建\n━━━━━━━━━━━━━━━━━━━━━━\n任务ID: ${result.task_id}\nPipeline: ${pipelineDisplayLabel(pipeline)}\n执行间隔: 每 ${parsed.interval} 小时\n下次执行: ${formatLocalTime(result.next_run_time)}${run_immediately ? "\n备注: 已立即执行一次" : ""}\n━━━━━━━━━━━━━━━━━━━━━━\n💡 使用 /auto-harness schedule list 查看所有任务\n   使用 /auto-harness schedule logs ${result.task_id} 查看执行日志\n`
      )
    );
  },
};

const scheduleListCommand: SlashCommand = {
  name: "list",
  description: "列出所有任务",
  kind: CommandKind.BUILT_IN,
  takesArgs: false,
  action: async (ctx, _args) => {
    ctx.addItem(addInfo(ctx.sessionId, "\n🔍 正在查询任务...\n", "i"));

    const result = await ctx.request<{ tasks?: Array<{ task_id: string; query: string; status: string; interval_hours: number; next_run_time: string; created_at: string; is_one_time?: boolean; pipeline?: string; progress?: AutoHarnessProgress }> }>("schedule.list", {});

    const tasks = result.tasks as Array<{ task_id: string; query: string; status: string; interval_hours: number; next_run_time: string; created_at: string; is_one_time?: boolean; pipeline?: string; progress?: AutoHarnessProgress }> | undefined;
    if (!tasks || tasks.length === 0) {
      ctx.addItem(addInfo(ctx.sessionId, "\n📭 暂无任务\n💡 使用 /auto-harness schedule start 创建定时任务\n   使用 /auto-harness run 创建一次性任务\n", "i"));
      return;
    }

    const lines = ["\n📋 任务列表\n━━━━━━━━━━━━━━━━━━━━━━"];
    for (const task of tasks) {
      const statusIcon = task.status === "running" ? "🔄" :
                         task.status === "pending" ? "⏳" :
                         task.status === "cancelled" ? "🛑" :
                         task.status === "failed" ? "❌" :
                         task.status === "success" ? "✅" : "📦";
      const isOneTime = task.is_one_time ? " [一次性]" : " [定时]";
      const queryPreview = task.query.length > 50 ? task.query.substring(0, 50) + "..." : task.query;
      const pipelineInfo = task.pipeline ? `Pipeline: ${pipelineDisplayLabel(task.pipeline)}` : "";
      lines.push(
        `${statusIcon}${isOneTime} ${task.task_id}`
      );
      lines.push(`   目标: ${queryPreview}`);
      // Show interval only for recurring tasks
      if (task.is_one_time) {
        lines.push(`   状态: ${task.status}${pipelineInfo ? ` | ${pipelineInfo}` : ""}`);
      } else {
        lines.push(`   状态: ${task.status} | 间隔: ${task.interval_hours}h | 下次执行: ${formatLocalTime(task.next_run_time)}${pipelineInfo ? ` | ${pipelineInfo}` : ""}`);
      }
      const progressLine = formatProgressLine(task.progress);
      if (progressLine) lines.push(progressLine);
      lines.push(`   创建时间: ${formatLocalTime(task.created_at)}`);
      lines.push("");
    }

    lines.push("💡 使用 /auto-harness schedule logs <task_id> 查看执行日志");
    lines.push("");
    ctx.addItem(addInfo(ctx.sessionId, lines.join("\n")));
  },
};

const scheduleStatusCommand: SlashCommand = {
  name: "status",
  description: "查看任务详情",
  usage: "/auto-harness schedule status <task_id>",
  kind: CommandKind.BUILT_IN,
  takesArgs: true,
  completion: async (ctx, partial) => {
    // Fetch task list for task_id completion
    try {
      const result = await ctx.request<{ tasks?: Array<{ task_id: string }> }>("schedule.list", {}, 5000);
      const tasks = result.tasks || [];
      const prefix = partial.trim().toLowerCase();
      if (!prefix) return tasks.map((t) => t.task_id);
      return tasks.filter((t) => t.task_id.toLowerCase().startsWith(prefix)).map((t) => t.task_id);
    } catch {
      return [];
    }
  },
  action: async (ctx, args) => {
    const task_id = args.trim();

    if (!task_id) {
      ctx.addItem(
        addError(ctx.sessionId, "用法: /auto-harness schedule status <task_id>\n示例: /auto-harness schedule status sch_abc123")
      );
      return;
    }

    const result = await ctx.request<{ error?: string; task_id?: string; query?: string; status?: string; interval_hours?: number; created_at?: string; next_run_time?: string; current_execution_id?: string; execution_history?: Array<{ execution_id: string; status: string; completed_at?: string }>; is_one_time?: boolean; pipeline?: string; progress?: AutoHarnessProgress }>("schedule.status", { task_id });

    if (result.error) {
      ctx.addItem(
        addError(ctx.sessionId, result.error)
      );
      return;
    }

    const isOneTime = result.is_one_time;
    const statusIcon = result.status === "running" ? "🔄" :
                       result.status === "pending" ? "⏳" :
                       result.status === "cancelled" ? "🛑" :
                       result.status === "failed" ? "❌" :
                       result.status === "success" ? "✅" : "📦";
    const lines = [`\n📄 任务详情\n━━━━━━━━━━━━━━━━━━━━━━`];
    lines.push(`任务ID: ${result.task_id}`);
    lines.push(`目标: ${result.query}`);
    lines.push(`状态: ${statusIcon} ${result.status}`);
    if (result.pipeline) {
      lines.push(`Pipeline: ${pipelineDisplayLabel(result.pipeline)}`);
    }
    if (isOneTime) {
      lines.push(`类型: 🎯 一次性任务`);
    } else {
      lines.push(`类型: ⏰ 定时任务 | 间隔: 每 ${result.interval_hours} 小时`);
      lines.push(`下次执行: ${formatLocalTime(result.next_run_time) || "已取消"}`);
    }
    lines.push(`创建时间: ${formatLocalTime(result.created_at)}`);

    if (result.current_execution_id) {
      lines.push(`\n🔄 当前执行: ${result.current_execution_id}`);
      lines.push(`💡 使用 /auto-harness schedule logs ${result.task_id} 查看实时日志`);
    }
    lines.push(...formatProgressBlock(result.progress));

    const history = result.execution_history as Array<{ execution_id: string; status: string; completed_at?: string }> | undefined;
    if (history && history.length > 0) {
      lines.push(`\n📜 执行历史 (${history.length} 次)`);
      const recentHistory = history.slice(-5);
      for (const record of recentHistory) {
        const histIcon = record.status === "success" ? "✅" :
                           record.status === "failed" ? "❌" :
                           record.status === "cancelled" ? "🛑" : "⚠️";
        lines.push(`  ${histIcon} ${record.execution_id} - ${formatLocalTime(record.completed_at) || "进行中"}`);
      }
    }

    lines.push("\n━━━━━━━━━━━━━━━━━━━━━━");
    lines.push("");
    ctx.addItem(
      addInfo(ctx.sessionId, lines.join("\n"))
    );
  },
};

const scheduleLogsCommand: SlashCommand = {
  name: "logs",
  description: "查看任务日志（自动适配：运行中则实时跟踪，已完成则显示历史）",
  usage: "/auto-harness schedule logs <task_id> [--history <n>]",
  example: "/auto-harness schedule logs sch_abc123 --history 0",
  kind: CommandKind.BUILT_IN,
  takesArgs: true,
  completion: async (ctx, partial) => {
    // Check for trailing space before trimming (to detect completed arguments)
    const hasTrailingSpace = partial.endsWith(" ");
    const parts = partial.trim().split(/\s+/).filter(Boolean);
    const lastPart = parts[parts.length - 1] || "";

    // Extract history index value if present (to exclude from task_id detection)
    const historyMatch = partial.match(/--history\s+(\d+)/);
    const historyIndexValue = historyMatch ? historyMatch[1] : null;

    // Find existing task_id: first non-flag, non-history-index argument
    const existingTaskId = parts.find((p) => {
      return !p.startsWith("-") && p !== historyIndexValue && !/^\d+$/.test(p);
    }) || "";

    // Helper: build completion string preserving existing arguments
    // Returns the full argument string including existing args and the completion
    const buildCompletion = (completion: string, replaceLast: boolean = true): string => {
      const prefixParts = replaceLast ? parts.slice(0, -1) : parts;
      return [...prefixParts, completion].join(" ");
    };

    // Step 1: Check if we need a history index value
    // --history is present and we're at the value position
    if (parts.includes("--history")) {
      const historyIdx = parts.indexOf("--history");
      // If we're exactly at the position after --history (typing the value)
      if (parts.length === historyIdx + 1) {
        const values = ["0", "1", "2", "3", "4"];
        if (lastPart && /^\d/.test(lastPart)) {
          // Preserve existing task_id before --history when completing index value
          const taskIdBeforeHistory = parts.slice(0, historyIdx).find((p) => !p.startsWith("-"));
          const filtered = values.filter((v) => v.startsWith(lastPart));
          if (taskIdBeforeHistory) {
            return filtered.map((v) => `${taskIdBeforeHistory} --history ${v}`);
          }
          return filtered.map((v) => buildCompletion(v));
        }
        // No number typed yet, suggest values with existing args preserved
        const taskIdBeforeHistory = parts.slice(0, historyIdx).find((p) => !p.startsWith("-"));
        if (taskIdBeforeHistory) {
          return values.map((v) => `${taskIdBeforeHistory} --history ${v}`);
        }
        return values.map((v) => buildCompletion(v));
      }
      // If history index is complete with trailing space, suggest task_ids
      // Check: history value exists AND there's a trailing space
      if (parts.length >= historyIdx + 2 && /^\d+$/.test(parts[historyIdx + 1]) && hasTrailingSpace) {
        try {
          const result = await ctx.request<{ tasks?: Array<{ task_id: string }> }>("schedule.list", {}, 5000);
          const tasks = result.tasks || [];
          // Return full string with existing args + task_id
          const existingArgs = parts.slice(0, -1).join(" ");
          return tasks.map((t) => `${existingArgs} ${t.task_id}`);
        } catch {
          return [];
        }
      }
      // If history index is complete but no trailing space, don't suggest task_ids yet
      if (parts.length === historyIdx + 2 && /^\d+$/.test(parts[historyIdx + 1])) {
        return [];
      }
    }

    // Step 2: Check if lastPart is exactly --history - suggest values immediately
    if (lastPart === "--history") {
      // Preserve existing task_id when suggesting history index values
      if (existingTaskId) {
        return ["0", "1", "2", "3", "4"].map((v) => `${existingTaskId} --history ${v}`);
      }
      return ["0", "1", "2", "3", "4"].map((v) => buildCompletion(v));
    }

    // Step 3: If typing a flag, suggest --history with existing args preserved
    if (lastPart.startsWith("-") && lastPart !== "--history") {
      if (parts.includes("--history")) return [];
      // Preserve existing task_id when completing --history flag
      if (existingTaskId) {
        return ["--history"].filter((f) => f.startsWith(lastPart)).map((f) => `${existingTaskId} ${f}`);
      }
      return ["--history"].filter((f) => f.startsWith(lastPart)).map((f) => buildCompletion(f));
    }

    // Step 4: If task_id is complete, suggest --history flag
    if (existingTaskId && hasTrailingSpace && !parts.includes("--history")) {
      return [`${existingTaskId} --history`];
    }

    // Step 5: Otherwise, suggest task_ids
    try {
      const result = await ctx.request<{ tasks?: Array<{ task_id: string }> }>("schedule.list", {}, 5000);
      const tasks = result.tasks || [];
      if (!lastPart) return tasks.map((t) => t.task_id);
      return tasks.filter((t) => t.task_id.toLowerCase().startsWith(lastPart.toLowerCase())).map((t) => t.task_id);
    } catch {
      return [];
    }
  },
  action: async (ctx, args) => {
    const parsed = parseLogArgs(args);

    if (!parsed.task_id) {
      ctx.addItem(
        addError(ctx.sessionId, "用法: /auto-harness schedule logs <task_id> [--history <n>]\n功能:\n  不带 --history: 自动适配 - 运行中则实时跟踪，已完成则显示最近历史日志\n  --history <n>: 查看指定历史执行日志（0=最近一次，1=上一次...）\n示例:\n  /auto-harness schedule logs sch_abc123              # 自动适配模式\n  /auto-harness schedule logs sch_abc123 --history 0  # 查看最近一次历史日志")
      );
      return;
    }

    if (parsed.log_type === "current") {
      // Try current logs first, auto-fallback to history if task is not running
      // First, check if task has a current execution (running)
      const statusResult = await ctx.request<{ error?: string; status?: string; current_execution_id?: string; execution_history?: Array<{ execution_id: string }> }>("schedule.status", { task_id: parsed.task_id });

      if (statusResult.error) {
        ctx.addItem(addError(ctx.sessionId, statusResult.error));
        return;
      }

      // If task is running and has current_execution_id, stream current logs
      if (statusResult.status === "running" && statusResult.current_execution_id) {
        await streamCurrentLogs(ctx, parsed.task_id);
      } else {
        // Task is not running, auto-switch to history mode (most recent)
        const history = statusResult.execution_history as Array<{ execution_id: string }> | undefined;
        if (history && history.length > 0) {
          ctx.addItem(addInfo(ctx.sessionId, `\n💡 任务已完成，自动显示最近一次历史日志\n任务状态: ${statusResult.status}\n`, "i"));
          await readFullHistoryLogs(ctx, parsed.task_id, 0);  // Show most recent history
        } else {
          ctx.addItem(addInfo(ctx.sessionId, `\n📭 任务无执行历史\n任务状态: ${statusResult.status}\n`));
        }
      }
    } else {
      // History mode: read full log in batches
      await readFullHistoryLogs(ctx, parsed.task_id, parsed.history_index);
    }
  },
};

interface LogEntry {
  event_type?: string;
  content?: string;
  stage?: string;
  status?: string;
  timestamp?: string;
  message?: string;
  is_processing?: boolean;
  source_chunk_type?: string;
  tool_name?: string;
  is_error?: boolean;
  // Error message (from chat.error event)
  error?: string;
  // Pipeline and stages info (from harness.message)
  pipeline?: string;
  stages?: Array<{ slot: string; display_name: string }>;
  // Session finished info
  is_terminal?: boolean;
  results_count?: number;
  // Extension-level fields (from harness.stage_result with scope='extension')
  scope?: string;              // 'extension' indicates extension-level event
  extension_name?: string;     // Extension name (e.g., context_fencing, merged_extensions)
  extension_stage?: string;    // 'implement_ext' | 'verify_ext' | 'activate_ext' | 'merge_ext'
  parent_stage?: string;       // Parent stage (e.g., 'build_verify', 'activate')
  task_id?: string;            // Task ID for extension
  // Extension ready fields (harness.extension_ready)
  runtime_path?: string;       // Runtime extension directory path
  extension_runtime_path?: string;
  config_path?: string;
  runtime_extensions?: Array<{ extension_name: string; runtime_path: string; config_path: string }>;
  components_summary?: { rails?: number; tools?: number; skills?: number };
  // Activate interaction fields (harness.activate_interaction)
  interaction_type?: string;
  interaction_id?: string;
  options?: string[];
  // Stage result messages
  messages?: string[];
  metrics?: Record<string, unknown>;
  // Nested tool payload (as in history-parser.ts resolveToolPayload)
  tool_call?: {
    name?: string;
    id?: string;
    tool_call_id?: string;
    arguments?: string | Record<string, unknown>;
    description?: string;
  };
  tool_result?: {
    name?: string;
    tool_name?: string;
    result?: string;
    status?: string;
    success?: boolean;
    summary?: string;
  };
  // Direct fields fallback
  name?: string;
  id?: string;
  tool_call_id?: string;
}

// Drain remaining logs from completed execution history
async function drainHistoryLogs(
  ctx: CommandContext,
  task_id: string,
  offset: number,
  parseState: ParseState | undefined,
  setParseState: (state: ParseState | undefined) => void
): Promise<void> {
  let historyOffset = offset;
  let hasMoreHistory = true;
  while (hasMoreHistory) {
    const historyResult = await ctx.request<{
      error?: string;
      logs?: Array<LogEntry>;
      has_more?: boolean;
    }>("schedule.logs", {
      task_id,
      log_type: "history",
      history_index: 0,
      offset: historyOffset,
      limit: 3000,
    }, 120000);

    if (historyResult.error || !historyResult.logs || historyResult.logs.length === 0) break;

    const result = parseAndAggregateLogs(historyResult.logs, parseState);
    setParseState(result.state);
    for (const section of result.sections) {
      const formattedLine = formatLogSection(section);
      if (formattedLine) {
        ctx.addItem(addInfo(ctx.sessionId, formattedLine, "i"));
      }
    }
    historyOffset += historyResult.logs.length;
    hasMoreHistory = historyResult.has_more ?? false;
  }
}

// Stream logs for currently running task (tail -f style)
async function streamCurrentLogs(
  ctx: CommandContext,
  task_id: string
): Promise<void> {
  let offset = 0;
  let isRunning = true;
  let executionId = "";
  let pollInterval = 2000; // 2 seconds
  let maxPolls = 300; // Max 300 polls (~10 minutes) to prevent infinite loop
  let pollCount = 0;
  let consecutiveEmptyPolls = 0;
  const maxEmptyPolls = 3; // Stop after 3 consecutive empty polls when not running

  // Parse state for maintaining pipeline info across batches
  let parseState: ParseState | undefined;

  // Clear any previous interrupt flag before starting new stream
  ctx.clearInterruptRequested();

  ctx.addItem(addInfo(ctx.sessionId, `\n📋 实时日志跟踪: ${task_id}\n正在连接...\n💡 按 Ctrl+C 可中断日志查看，任务将继续后台运行\n`));

  // Helper: check interrupt and exit if requested
  const checkInterrupt = (): boolean => {
    if (ctx.isInterruptRequested()) {
      ctx.clearInterruptRequested();
      ctx.addItem(addInfo(ctx.sessionId, `\n⏸️ 日志跟踪已中断\n💡 任务仍在后台运行，可使用 /auto-harness schedule logs ${task_id} 继续查看\n`));
      return true;
    }
    return false;
  };

  // Helper: interruptible request - checks interrupt flag while waiting
  const interruptibleRequest = async <T>(
    method: string,
    params: Record<string, unknown>,
    timeoutMs: number,
    checkIntervalMs: number = 200
  ): Promise<T | null> => {
    const requestPromise = ctx.request<T>(method, params, timeoutMs);
    // Poll interrupt flag periodically while waiting for request
    const interruptChecker = new Promise<null>((resolve) => {
      const interval = setInterval(() => {
        if (ctx.isInterruptRequested()) {
          clearInterval(interval);
          resolve(null);
        }
      }, checkIntervalMs);
      // Clean up interval when request completes
      requestPromise.then(() => clearInterval(interval)).catch(() => clearInterval(interval));
    });
    // Race between request and interrupt
    return Promise.race([requestPromise, interruptChecker]);
  };

  while (isRunning && pollCount < maxPolls) {
    // Check interrupt at start of each loop
    if (checkInterrupt()) return;

    pollCount++;
    try {
      // Use interruptible request for immediate Ctrl+C response
      const result = await interruptibleRequest<{
        error?: string;
        logs?: Array<LogEntry>;
        execution_id?: string;
        total_lines?: number;
        is_running?: boolean;
        has_more?: boolean;
      }>("schedule.logs", {
        task_id,
        log_type: "current",
        offset,
        limit: 3000,
      }, 1200000);  // 60s timeout

      // Request was interrupted - clear flag and show message via helper
      if (result === null) {
        checkInterrupt(); // clears flag and shows message (returns true when flag was set)
        return;
      }

      // Check interrupt after request completes
      if (checkInterrupt()) return;

      // Check for error - likely means execution finished
      if (result.error) {
        if (result.error.includes("当前无正在执行的日志")) {
          // Task completed — pull remaining logs from execution history
          await drainHistoryLogs(ctx, task_id, offset, parseState,
            (updatedState) => { parseState = updatedState; });
          ctx.addItem(addInfo(ctx.sessionId, `\n✅ 任务执行完成\n`));
          return;
        }
        // Other errors (e.g. task doesn't exist) — show message directly
        if (result.error.includes("不存在")) {
          ctx.addItem(addInfo(ctx.sessionId, `\n✅ 任务执行完成\n`));
          return;
        }
        ctx.addItem(addError(ctx.sessionId, result.error));
        return;
      }

      executionId = result.execution_id || "";
      isRunning = result.is_running ?? false;

      // Display new logs - aggregate streaming chunks for better display
      const logs = result.logs || [];
      if (logs.length > 0) {
        consecutiveEmptyPolls = 0;

        const parseResult = parseAndAggregateLogs(logs, parseState);
        parseState = parseResult.state;
        for (const section of parseResult.sections) {
          // Check interrupt during log display
          if (checkInterrupt()) return;
          const formattedLine = formatLogSection(section);
          if (formattedLine) {
            ctx.addItem(addInfo(ctx.sessionId, formattedLine, "i"));
          }
        }
        offset = offset + logs.length;
      } else {
        consecutiveEmptyPolls++;
      }

      // Stop if not running and no new logs for a few polls
      if (!isRunning && consecutiveEmptyPolls >= maxEmptyPolls) {
        break;
      }

      // Check for local interrupt request (Ctrl+C)
      if (checkInterrupt()) return;

      // Continue polling if still running - use shorter intervals for faster interrupt response
      if (isRunning) {
        // Use shorter 500ms intervals and check interrupt status each time
        for (let i = 0; i < pollInterval / 500; i++) {
          await new Promise((resolve) => setTimeout(resolve, 500));
          // Check for local interrupt request (Ctrl+C)
          if (checkInterrupt()) return;
        }
      } else {
        // Wait a bit more to get final logs
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    } catch (e) {
      ctx.addItem(addError(ctx.sessionId, `⚠️ 日志流错误: ${e}\n💡 请稍后重试，或使用 /auto-harness schedule logs ${task_id} 查看日志`));
      return;
    }
  }

  if (pollCount >= maxPolls) {
    ctx.addItem(addInfo(ctx.sessionId, `\n⏱️ 日志跟踪已超时退出 \n💡 任务仍在后台运行，可使用 /auto-harness schedule logs ${task_id} 继续查看\n`));
  } else {
    // Try to read remaining logs from completed execution history
    await drainHistoryLogs(ctx, task_id, offset, parseState,
      (updatedState) => { parseState = updatedState; });
    ctx.addItem(addInfo(ctx.sessionId, `\n✅ 任务执行完成\n执行ID: ${executionId}\n`));
  }
}

// Read full history logs in batches
async function readFullHistoryLogs(
  ctx: CommandContext,
  task_id: string,
  history_index: number
): Promise<void> {
  ctx.addItem(addInfo(ctx.sessionId, `\n📜 正在读取历史日志...\n`, "i"));

  let allLogs: Array<LogEntry> = [];
  let offset = 0;
  const batchSize = 5000;
  let executionId = "";
  let completedAt = "";
  let status = "";
  let hasMore = true;

  while (hasMore) {
    const result = await ctx.request<{
      error?: string;
      logs?: Array<LogEntry>;
      execution_id?: string;
      completed_at?: string;
      status?: string;
      total_lines?: number;
      has_more?: boolean;
    }>("schedule.logs", {
      task_id,
      log_type: "history",
      history_index,
      offset,
      limit: batchSize,
    }, 60000);

    if (result.error) {
      ctx.addItem(addError(ctx.sessionId, result.error));
      return;
    }

    // Capture metadata from first batch
    if (offset === 0) {
      executionId = result.execution_id || "";
      completedAt = result.completed_at || "";
      status = result.status || "";
    }

    const logs = result.logs || [];
    if (logs.length === 0) {
      hasMore = false;
      break;
    }

    allLogs.push(...logs);
    offset = offset + logs.length;

    // Check if there's more to read
    hasMore = result.has_more ?? (logs.length >= batchSize);
  }

  // Display full aggregated logs
  if (allLogs.length === 0) {
    ctx.addItem(addInfo(ctx.sessionId, "\n📭 日志为空\n"));
    return;
  }

  const result = parseAndAggregateLogs(allLogs);
  const lines: string[] = [`\n📜 执行日志\n━━━━━━━━━━━━━━━━━━━━━━\n执行ID: ${executionId}`];
  if (completedAt) {
    const statusIcon = status === "success" ? "✅" :
                       status === "failed" ? "❌" :
                       status === "cancelled" ? "🛑" : "⚠️";
    lines.push(`完成时间: ${formatLocalTime(completedAt)} | 状态: ${statusIcon} ${status}`);
  }
  lines.push("━━━━━━━━━━━━━━━━━━━━━━\n");
  lines.push("");
  lines.push("=" .repeat(80));

  for (const section of result.sections) {
    const formattedLine = formatLogSection(section, true);
    if (formattedLine) {
      lines.push(formattedLine);
    }
  }

  lines.push("=" .repeat(80));
  lines.push("");

  const formattedContent = lines.join("\n");

  // Use FileViewer mode if available (TUI environment)
  if (ctx.enterFileViewer) {
    ctx.enterFileViewer(
      formattedContent,
      `执行日志: ${executionId}`,
      `task_id: ${task_id}, history_index: ${history_index}`,
    );
  } else {
    // Fallback: display directly (non-TUI environment)
    ctx.addItem(addInfo(ctx.sessionId, formattedContent));
  }
}

/**
 * Parse and aggregate logs similar to handleIncomingFrame.
 * Merges streaming chunks (chat.delta, chat.reasoning) into complete messages.
 */
interface ParsedLogSection {
  type: "assistant" | "stage" | "status" | "error" | "info" | "pipeline" | "session_finished" | "extension_ready" | "activate_interaction";
  content: string;
  stage?: string;
  status?: string;
  timestamp?: string;
  tool_name?: string;
  tool_success?: boolean;
  // Pipeline info
  pipeline?: string;
  stages?: Array<{ slot: string; display_name: string }>;
  completed_stages?: string[];
  // Stage result tracking (for warning icon display)
  stages_with_success_result?: string[];
  stages_with_result?: string[];
  // Extension info (for extended_evolve_pipeline)
  extension_order?: string[];
  extensions_by_name?: Record<string, ExtensionProgressInfo>;
  // Gap count for inline progress bar display
  gap_count?: number;
  // Skipped stages (issue-fix skips assess/plan)
  skipped_stages?: string[];
  // Stage messages (for meta_evolve_pipeline CI fix tracking)
  stage_messages?: string[];
  ci_fix_count?: number;
  // Extension ready info (harness.extension_ready)
  extension_name?: string;
  runtime_path?: string;
  components_summary?: { rails?: number; tools?: number; skills?: number };
  // Activate interaction info (harness.activate_interaction)
  interaction_id?: string;
}

// ANSI color codes for log display differentiation
const ANSI = {
  cyan: "\x1b[36m",     // 工具调用
  green: "\x1b[32m",    // 成功
  red: "\x1b[31m",      // 错误/失败
  yellow: "\x1b[33m",   // 阶段
  blue: "\x1b[34m",     // 状态
  magenta: "\x1b[35m",  // 压缩信息
  brightWhite: "\x1b[97m",  // 辅助输出 (柔和)
  gray: "\x1b[90m",     // 普通 gray (bright black)
  dimGray: "\x1b[38;5;240m", // 更暗的灰色 (256-color mode)
  lightBlue: "\x1b[94m",    // AI 消息 - 浅蓝色 (bright blue)
  bold: "\x1b[1m",
  underline: "\x1b[4m",
  reset: "\x1b[0m",
};

// Helper: get extension status icon
function getExtensionStatusIcon(status: ExtensionProgressStatus): string {
  switch (status) {
    case 'success': return '✓';
    case 'failed': return '✗';
    case 'running': return '⏳';
    case 'timeout': return '⏱';
    case 'waiting': return '?';
    case 'skipped': return '○';
    case 'rejected': return '✗';
    default: return '○'; // pending
  }
}

// Helper: get extension status color
function getExtensionStatusColor(status: ExtensionProgressStatus): string {
  switch (status) {
    case 'success': return ANSI.green;
    case 'failed': return ANSI.red;
    case 'running': return ANSI.yellow;
    case 'timeout': return ANSI.red;
    default: return ANSI.gray;
  }
}

// Helper: format extension status matrix for display
function formatExtensionStatusMatrix(
  extensionOrder: string[],
  extensionsByName: Record<string, ExtensionProgressInfo>
): string {
  const lines: string[] = [];
  // Header with box border (yellow/gold color for extensions) - matching design list width
  lines.push(`${ANSI.yellow}${ANSI.bold}┌${'─'.repeat(26)} 🔧 扩展状态 (${extensionOrder.length} 个) ${'─'.repeat(26)}┐${ANSI.reset}`);

  for (let i = 0; i < extensionOrder.length; i++) {
    const extName = extensionOrder[i];
    const ext = extensionsByName[extName];
    if (!ext) continue;

    const implIcon = getExtensionStatusIcon(ext.implementStatus);
    const implColor = getExtensionStatusColor(ext.implementStatus);
    const verifyIcon = getExtensionStatusIcon(ext.verifyStatus);
    const verifyColor = getExtensionStatusColor(ext.verifyStatus);

    const num = `${ANSI.yellow}${String(i + 1).padStart(2, ' ')}.${ANSI.reset}`;
    lines.push(`${ANSI.yellow}│${ANSI.reset} ${num} ${extName}: ${implColor}实现 ${implIcon}${ANSI.reset} → ${verifyColor}验证 ${verifyIcon}${ANSI.reset}`);
  }

  // Footer border - matching design list width (72 chars)
  lines.push(`${ANSI.yellow}${ANSI.bold}└${'─'.repeat(72)}┘${ANSI.reset}`);

  return lines.join('\n');
}

// Helper: parse named list from messages (same logic as Web's parseNamedList)
// Gaps: separated by ';', Designs: separated by ','
function parseNamedList(messages: string[], prefix: string): string[] {
  const values: string[] = [];
  for (const message of messages) {
    const normalized = message.trim();
    if (!normalized.startsWith(prefix)) continue;
    const raw = normalized.slice(prefix.length).trim();
    // Gaps use ';' separator, Designs use ',' separator (same as Web)
    const parts = prefix === 'Designs:' ? raw.split(',') : raw.split(';');
    for (const part of parts) {
      const value = part.trim();
      // Deduplicate
      if (value && !values.includes(value)) {
        values.push(value);
      }
    }
  }
  return values;
}

// Helper: format stage messages for display (extract key info)
function formatStageMessages(stage: string, messages: string[]): string {
  const lines: string[] = [];

  for (const msg of messages) {
    // Filter out structural messages (same logic as Web)
    const normalized = msg.trim();
    if (normalized.startsWith('Gaps:') || normalized.startsWith('Designs:') ||
        normalized.startsWith('Gap analysis complete:') || normalized.startsWith('Extension design complete:')) {
      continue; // These are shown separately
    }

    // Extract CI results for verify stage
    if (stage === 'verify' && normalized.includes('CI 结果:')) {
      const ciMatch = normalized.match(/CI 结果:\s*(.+)/);
      if (ciMatch) {
        const ciResults = ciMatch[1];
        // Format lint and type-check results
        const lintMatch = ciResults.match(/lint=(\w+)/);
        const typeMatch = ciResults.match(/type-check=(\w+)/);
        if (lintMatch && typeMatch) {
          const lintIcon = lintMatch[1] === 'PASS' ? '✓' : '✗';
          const lintColor = lintMatch[1] === 'PASS' ? ANSI.green : ANSI.red;
          const typeIcon = typeMatch[1] === 'PASS' ? '✓' : '✗';
          const typeColor = typeMatch[1] === 'PASS' ? ANSI.green : ANSI.red;
          lines.push(`  🔍 lint ${lintColor}${lintIcon}${ANSI.reset} type-check ${typeColor}${typeIcon}${ANSI.reset}`);
        }
      }
      continue;
    }

    // Show other messages with indent
    lines.push(`  ${normalized}`);
  }

  return lines.join('\n');
}

// Helper: format gap list for assess stage completion
function formatGapList(messages: string[]): string {
  const gaps = parseNamedList(messages, 'Gaps:');
  if (gaps.length === 0) return '';

  const lines: string[] = [];
  // Header with box border
  lines.push(`${ANSI.cyan}${ANSI.bold}┌${'─'.repeat(25)} 📋 发现 ${gaps.length} 个关键缺口 ${'─'.repeat(25)}┐${ANSI.reset}`);
  // Gap items with numbering
  for (let i = 0; i < gaps.length; i++) {
    const gap = gaps[i];
    const num = `${ANSI.cyan}${String(i + 1).padStart(2, ' ')}.${ANSI.reset}`;
    lines.push(`${ANSI.cyan}│${ANSI.reset} ${num} ${gap}`);
  }
  // Footer border
  lines.push(`${ANSI.cyan}${ANSI.bold}└${'─'.repeat(72)}┘${ANSI.reset}`);
  return lines.join('\n');
}

// Helper: format design list for plan stage completion
function formatDesignList(messages: string[]): string {
  const designs = parseNamedList(messages, 'Designs:');
  if (designs.length === 0) return '';

  const lines: string[] = [];
  // Header with box border (magenta color for designs)
  lines.push(`${ANSI.magenta}${ANSI.bold}┌${'─'.repeat(25)} 📝 生成 ${designs.length} 个设计方案 ${'─'.repeat(25)}┐${ANSI.reset}`);
  // Design items with numbering
  for (let i = 0; i < designs.length; i++) {
    const design = designs[i];
    const num = `${ANSI.magenta}${String(i + 1).padStart(2, ' ')}.${ANSI.reset}`;
    lines.push(`${ANSI.magenta}│${ANSI.reset} ${num} ${design}`);
  }
  // Footer border
  lines.push(`${ANSI.magenta}${ANSI.bold}└${'─'.repeat(72)}┘${ANSI.reset}`);
  return lines.join('\n');
}

// Helper function to calculate visual width (Chinese/CJK chars = 2, others = 1)
function visualWidth(str: string): number {
  let width = 0;
  // Use codePointAt to correctly handle emoji (surrogate pairs)
  for (let i = 0; i < str.length; i++) {
    const code = str.codePointAt(i) || 0;

    // Box drawing characters (U+2500-U+257F) are width 1
    if (code >= 0x2500 && code <= 0x257F) {
      width += 1; // Box drawing
    } else if (
      (code >= 0x4E00 && code <= 0x9FFF) || // CJK Unified Ideographs
      (code >= 0x3000 && code <= 0x303F) || // CJK Symbols
      (code >= 0xFF00 && code <= 0xFFEF) || // Halfwidth/Fullwidth
      code >= 0x1F000 // Emoji and other high ranges
    ) {
      width += 2; // Wide characters (Chinese, emoji)
    } else {
      width += 1; // ASCII and others
    }

    // Skip low surrogate if we processed a surrogate pair (emoji takes 2 UTF-16 units)
    if (code > 0xFFFF) {
      i++; // Skip the low surrogate
    }
  }
  return width;
}

// Helper function to wrap text to a maximum visual width
function wrapText(text: string, maxWidth?: number): string {
  // Auto-detect terminal width if not provided
  // "💬 " prefix takes 4 visual chars (emoji=2, space=1), leave 2 margin on right
  const defaultWidth = (process.stdout.columns || 100) - 6;
  const wrapWidth = maxWidth ?? defaultWidth;

  const lines: string[] = [];
  const paragraphs = text.split('\n');

  for (const paragraph of paragraphs) {
    if (paragraph.trim() === '') {
      lines.push('');
      continue;
    }

    let currentLine = '';
    let currentWidth = 0;

    for (const char of paragraph) {
      const charWidth = visualWidth(char);

      if (currentWidth + charWidth > wrapWidth && currentLine.length > 0) {
        lines.push(currentLine);
        currentLine = char;
        currentWidth = charWidth;
      } else {
        currentLine += char;
        currentWidth += charWidth;
      }
    }

    if (currentLine.length > 0) {
      lines.push(currentLine);
    }
  }

  return lines.join('\n');
}

// Helper function to create a proper box with title embedded in top border
function createBox(title: string, content: string, color: string): string {
  const leftDashes = 32;
  const rightDashes = 37;
  const titleVisualWidth = visualWidth(title);
  const topVisualWidth = 72 + titleVisualWidth;
  const contentPadding = topVisualWidth - 3 - visualWidth(content);

  const topBorder = `╔${"═".repeat(leftDashes)} ${title} ${"═".repeat(rightDashes)}╗`;
  const paddedContent = contentPadding < 0
    ? `║ ${content.substring(0, topVisualWidth - 7)}... ║`
    : `║ ${content}${" ".repeat(Math.max(0, contentPadding))} ║`;
  const bottomBorder = `╚${"═".repeat(topVisualWidth - 1)}╝`;

  return `${ANSI.bold}${color}${topBorder}${ANSI.reset}\n${color}${paddedContent}${ANSI.reset}\n${color}${ANSI.bold}${bottomBorder}${ANSI.reset}`;
}

// Helper: format assistant content with dialog bubble style (light blue)
function formatAssistantContent(content: string): string {
  const wrapped = wrapText(content);
  const lines = wrapped.split('\n');
  // First line has 💬 prefix in light blue, subsequent lines have indentation
  const formattedLines = lines.map((line, index) => {
    if (index === 0) {
      return `${ANSI.lightBlue}💬 ${line}${ANSI.reset}`;
    }
    return `${ANSI.lightBlue}   ${line}${ANSI.reset}`;  // 3 spaces indent to align with 💬
  });
  return formattedLines.join('\n');
}

// Helper: extract PR URL from stage messages (publish stage)
function extractPrUrl(messages: string[]): string | undefined {
  const urlRegex = /https:\/\/gitcode\.com\/[^\s)>\"]+/;
  for (const msg of messages) {
    const match = msg.match(urlRegex);
    if (match && /(?:pulls|pull_requests|merge_requests)\/\d+/.test(match[0])) {
      return match[0];
    }
  }
  return undefined;
}

// Format log section for display (compact for streaming, detailed for history)
function formatLogSection(section: ParsedLogSection, detailed: boolean = false): string | null {
  switch (section.type) {
    case "assistant":
      return formatAssistantContent(section.content);

    case "pipeline":
      const stagesDisplay = section.stages?.map((s) => s.display_name).join(" → ") || "";
      return `\n${createBox(`Pipeline: ${pipelineDisplayLabel(section.pipeline || "unknown")}`, `流程: ${stagesDisplay}`, ANSI.cyan)}\n`;

    case "stage":
      const stageDisplayName = section.stages?.find((s) => s.slot === section.stage)?.display_name || section.stage || "?";

      // 扩展级事件（含"扩展"但非"阶段完成"）不更新阶段完成状态
      const isExtensionLevel = section.content.includes("扩展") && !section.content.includes("阶段完成");

      if (section.status) {
        // 扩展级事件始终显示▶；阶段级事件仅在非终态时显示
        let activeStage: string | undefined;
        if (isExtensionLevel) {
          activeStage = section.stage;
        } else {
          activeStage = section.status !== 'success' && section.status !== 'failed' ? section.stage : undefined;
        }

        const effectiveCompleted = [...(section.completed_stages || [])];
        const effectiveSuccessSet = new Set(section.stages_with_success_result || []);
        const effectiveResultSet = new Set(section.stages_with_result || []);

        // 仅阶段级事件更新完成状态
        if (!isExtensionLevel && section.stage && !effectiveCompleted.includes(section.stage)) {
          if (section.status === 'success' || section.status === 'failed') {
            effectiveCompleted.push(section.stage);
            if (section.status === 'failed') {
              effectiveResultSet.add(section.stage);
            } else if (section.status === 'success') {
              effectiveSuccessSet.add(section.stage);
            }
          }
        }

        const progressBar = formatStageProgress(section.stages, effectiveCompleted, activeStage, section.gap_count, section.extension_order?.length, effectiveSuccessSet, effectiveResultSet, section.skipped_stages);
        const icon = section.status === "success" ? "✅" : section.status === "failed" ? "❌" : "⏸️";
        const color = section.status === "success" ? ANSI.green : section.status === "failed" ? ANSI.red : ANSI.yellow;
        const statusText = section.status === "success" ? "完成" : section.status === "failed" ? "失败" : section.status;

        let detailLines: string[] = [];
        if (section.stage === 'assess' && section.stage_messages) {
          const gapList = formatGapList(section.stage_messages);
          if (gapList) detailLines.push(gapList);
          const otherMsgs = formatStageMessages(section.stage, section.stage_messages);
          if (otherMsgs) detailLines.push(otherMsgs);
        }
        if (section.stage === 'plan' && section.stage_messages) {
          const designList = formatDesignList(section.stage_messages);
          if (designList) detailLines.push(designList);
          const otherMsgsPlan = formatStageMessages(section.stage, section.stage_messages);
          if (otherMsgsPlan) detailLines.push(otherMsgsPlan);
        }
        if (section.stage === 'verify' && section.stage_messages) {
          const formattedMsgs = formatStageMessages(section.stage, section.stage_messages);
          if (formattedMsgs) detailLines.push(formattedMsgs);
          if (section.ci_fix_count && section.ci_fix_count > 0) {
            detailLines.push(`  🔄 修复循环: ${section.ci_fix_count} 次`);
          }
        }
        // publish 阶段：显示 PR 链接和任务总结
        if (section.stage === 'publish' && section.stage_messages) {
          const prUrl = extractPrUrl(section.stage_messages);
          if (prUrl) {
            detailLines.push(`  🔗 PR: ${prUrl}`);
          }
          const summaryMsg = section.stage_messages.find(m => m.includes('任务总结'));
          if (summaryMsg) {
            const trimmed = summaryMsg.length > 120 ? summaryMsg.substring(0, 120) + '…' : summaryMsg;
            detailLines.push(`  📋 ${trimmed}`);
          }
        }
        // Extension status matrix shown AFTER stage-specific content, only during build_verify
        // (activate stage shows merge/activation info lines instead, not the matrix)
        if (section.stage !== 'activate' && section.extension_order && section.extensions_by_name && section.extension_order.length > 0) {
          const hasProgress = Object.values(section.extensions_by_name).some(
            ext => ext.implementStatus !== 'pending' || ext.verifyStatus !== 'pending'
          );
          if (hasProgress) {
            detailLines.push(formatExtensionStatusMatrix(section.extension_order, section.extensions_by_name));
          }
        }

        const detailsBlock = detailLines.length > 0 ? '\n' + detailLines.join('\n') + '\n' : '';
        return `\n${progressBar}\n${color}${ANSI.bold}${icon} ${stageDisplayName} ${statusText}${ANSI.reset}${detailsBlock}\n`;
      }

      const startProgressBar = formatStageProgress(
        section.stages,
        section.completed_stages,
        section.stage,
        section.gap_count,
        section.extension_order?.length,
        new Set(section.stages_with_success_result || []),
        new Set(section.stages_with_result || []),
        section.skipped_stages
      );
      const showContent = section.content && section.content !== stageDisplayName;
      const normalizedContent = (section.content || "").trim();

      if (normalizedContent.includes('Gap analysis complete') || normalizedContent.startsWith('Gaps:')) {
        if (detailed) {
          return `\n${startProgressBar}\n${ANSI.yellow}${ANSI.bold}▶ 📊 ${stageDisplayName}${ANSI.reset}\n`;
        }
        return `\n${startProgressBar}\n`;
      }

      if (showContent) {
        const wrappedContent = wrapText(section.content, 100);
        const indentedContent = wrappedContent.split("\n").map(line => "  " + line).join("\n");
        if (detailed) {
          return `\n${startProgressBar}\n${ANSI.yellow}${ANSI.bold}▶ 📊 ${stageDisplayName}${ANSI.reset}\n${ANSI.yellow}${indentedContent}${ANSI.reset}\n`;
        }
        return `\n${startProgressBar}\n${ANSI.gray}${indentedContent}${ANSI.reset}\n`;
      }

      if (detailed) {
        return `\n${startProgressBar}\n${ANSI.yellow}${ANSI.bold}▶ 📊 ${stageDisplayName}${ANSI.reset}\n`;
      }
      return `\n${startProgressBar}\n`;

    case "session_finished":
      const finishedIcon = section.status === "success" ? "🎉" : "⚠️";
      const finishedColor = section.status === "success" ? ANSI.green : ANSI.yellow;
      return `\n${createBox(`${finishedIcon} ${section.content}`, `Pipeline: ${pipelineDisplayLabel(section.pipeline || "unknown")}`, finishedColor)}\n`;

    case "status":
      return `${ANSI.blue}▶ ${section.content}${ANSI.reset}`;

    case "error":
      const wrappedError = wrapText(section.content, 100);
      const errorLines = wrappedError.split("\n");
      const formattedErrorLines = errorLines.map((line, index) => {
        if (index === 0) {
          return `${ANSI.red}${ANSI.bold}🔥 错误: ${line}${ANSI.reset}`;
        }
        return `${ANSI.red}${ANSI.bold}        ${line}${ANSI.reset}`; // 8 spaces indent to align with "🔥 错误:"
      });
      return formattedErrorLines.join("\n");

    case "info":
      return `${ANSI.gray}  · ${section.content}${ANSI.reset}`;

    case "extension_ready":
      const extReadyLines: string[] = [];
      extReadyLines.push(`${ANSI.green}${ANSI.bold}📦 ${section.content}${ANSI.reset}`);
      if (section.runtime_path) {
        extReadyLines.push(`  目录: ${ANSI.cyan}${ANSI.underline}${section.runtime_path}${ANSI.reset}`);
      }
      if (section.components_summary) {
        const cs = section.components_summary;
        const parts: string[] = [];
        if (cs.rails && cs.rails > 0) parts.push(`${ANSI.cyan}${cs.rails} rails${ANSI.reset}`);
        if (cs.tools && cs.tools > 0) parts.push(`${ANSI.yellow}${cs.tools} tools${ANSI.reset}`);
        if (cs.skills && cs.skills > 0) parts.push(`${ANSI.magenta}${cs.skills} skills${ANSI.reset}`);
        if (parts.length > 0) {
          extReadyLines.push(`  组件: ${parts.join(' ')}`);
        }
      }
      return extReadyLines.join('\n');

    case "activate_interaction":
      return `${ANSI.yellow}${ANSI.bold}⏳ ${section.content}${ANSI.reset}`;

    default:
      return null;
  }
}

// Helper: format stage progress bar with visual progress indicator
function formatStageProgress(
  stages?: Array<{ slot: string; display_name: string }>,
  completedStages?: string[],
  currentStage?: string,
  // Inline count display for progress bar
  gapCount?: number,
  extensionCount?: number,
  // Track which stages succeeded vs failed (for warning icon display)
  stagesWithSuccessResult?: Set<string>,
  stagesWithResult?: Set<string>,
  // Skipped stages (issue-fix: assess/plan are skipped, shown differently)
  skippedStages?: string[]
): string {
  if (!stages || stages.length === 0) return "";

  // Calculate progress
  const completedCount = completedStages?.length || 0;
  const total = stages.length;
  const percent = Math.min(100, Math.round((completedCount / total) * 100));

  // Create progress bar (fixed 80 chars for consistent look)
  const barLength = 80;
  const filledLength = Math.min(barLength, Math.round((completedCount / total) * barLength));
  const bar = `${ANSI.green}${"█".repeat(filledLength)}${ANSI.reset}${ANSI.gray}${"░".repeat(barLength - filledLength)}${ANSI.reset}`;

  // Create stage status line with icons and names
  const skippedSet = new Set(skippedStages || []);
  const parts = stages.map((s) => {
    const isSkipped = skippedSet.has(s.slot);
    const isCompleted = !isSkipped && completedStages?.includes(s.slot);
    const isCurrent = currentStage === s.slot;
    const hasSuccess = stagesWithSuccessResult?.has(s.slot);
    const hasResult = stagesWithResult?.has(s.slot);

    // Add inline count for specific stages
    let inlineCount = '';
    if (s.slot === 'assess' && gapCount && gapCount > 0 && (isCompleted || isCurrent)) {
      inlineCount = ` (${gapCount})`;
    }
    if (s.slot === 'plan' && extensionCount && extensionCount > 0 && (isCompleted || isCurrent)) {
      inlineCount = ` (${extensionCount})`;
    }

    if (isSkipped) {
      return `${ANSI.dimGray}⊘ ${s.display_name}${ANSI.reset}`;
    } else if (isCompleted) {
      // 成功→✓(绿)；失败(hasResult无success)→⚠(黄)
      if (hasSuccess) {
        return `${ANSI.green}✓ ${s.display_name}${inlineCount}${ANSI.reset}`;
      } else if (hasResult) {
        return `${ANSI.yellow}⚠  ${s.display_name}${inlineCount}${ANSI.reset}`;
      } else {
        return `${ANSI.green}✓ ${s.display_name}${inlineCount}${ANSI.reset}`;
      }
    } else if (isCurrent) {
      return `${ANSI.yellow}▶ ${s.display_name}${inlineCount}${ANSI.reset}`;
    } else {
      return `${ANSI.gray}○ ${s.display_name}${ANSI.reset}`;
    }
  });

  // Combine: progress bar with percent, then stage names on separate line
  return `${ANSI.bold}进度${ANSI.reset} ${bar} ${percent}%\n${parts.join(" → ")}`;
}

// Extension progress status types (matching Web's harnessStore.ts)
type ExtensionProgressStatus = 'pending' | 'running' | 'success' | 'failed' | 'timeout' | 'waiting' | 'skipped' | 'rejected';

// Extension progress info for tracking each extension's status
interface ExtensionProgressInfo {
  extensionName: string;
  implementStatus: ExtensionProgressStatus;
  verifyStatus: ExtensionProgressStatus;
  activateStatus: ExtensionProgressStatus;
}

// State for incremental log parsing (to maintain pipeline info across batches)
interface ParseState {
  pipelineInfo: { pipeline: string; stages: Array<{ slot: string; display_name: string }> } | null;
  completedStages: string[];
  currentStage: string | null;
  extensionOrder: string[];
  extensionsByName: Record<string, ExtensionProgressInfo>;
  gapCount: number;
  ciFixCount: number;
  hasFailure: boolean;  // Track if any stage or extension failed during execution
  // scope=""且status="success"的阶段
  stagesWithSuccessResult: Set<string>;
  // scope=""且status为终态(success/failed)的阶段
  stagesWithResult: Set<string>;
  // 已出现过的阶段(任意status)
  stagesAppeared: Set<string>;
  // 已跳过的阶段(issue-fix 模式下 assess/plan)
  skippedStages: string[];
}

function parseAndAggregateLogs(
  logs: Array<LogEntry>,
  initialState?: ParseState
): { sections: ParsedLogSection[]; state: ParseState } {
  const sections: ParsedLogSection[] = [];

  // Track pipeline progress - use initial state if provided (for incremental parsing)
  let pipelineInfo = initialState?.pipelineInfo ?? null;
  const completedStages: string[] = initialState?.completedStages ?? [];
  let currentStage = initialState?.currentStage ?? null;
  const extensionOrder: string[] = initialState?.extensionOrder ?? [];
  const extensionsByName: Record<string, ExtensionProgressInfo> = initialState?.extensionsByName ?? {};
  let gapCount = initialState?.gapCount ?? 0;
  let ciFixCount = initialState?.ciFixCount ?? 0;
  let hasFailure = initialState?.hasFailure ?? false;
  const stagesWithSuccessResult: Set<string> = initialState?.stagesWithSuccessResult ?? new Set<string>();
  const stagesWithResult: Set<string> = initialState?.stagesWithResult ?? new Set<string>();
  const stagesAppeared: Set<string> = initialState?.stagesAppeared ?? new Set<string>();
  const skippedStages: string[] = initialState?.skippedStages ?? [];

  // Note: pipeline type is determined dynamically in the loop when pipelineInfo is set

  for (const log of logs) {
    const eventType = log.event_type || "";
    const content = log.content || log.message || "";

    // Pipeline-specific filtering: only show chat.final and chat.error
    // Skip all other chat events (reasoning, delta, tool_call, tool_result, processing_status)
    if (eventType.startsWith("chat.")) {
      if (eventType !== "chat.final" && eventType !== "chat.error") {
        continue;
      }
    }

    switch (eventType) {
      case "chat.final":
        if (content) {
          sections.push({ type: "assistant", content: content });
        }
        break;

      case "chat.error":
        const errorMsg = log.error || content || "未知错误";
        // chat.error 不表示任务失败（如 learnings 阶段的 chat.error 不影响 publish 成功），
        // 仅由 stage_result 的状态来判定任务成败。
        sections.push({ type: "error", content: errorMsg });
        break;

      case "harness.message":
        // Check if this is pipeline info with stages
        if (log.stages && log.pipeline) {
          // Pipeline header - show workflow structure
          pipelineInfo = { pipeline: log.pipeline, stages: log.stages };
          sections.push({
            type: "pipeline",
            content: log.content || "",
            pipeline: log.pipeline,
            stages: log.stages,
          });
          break;
        }

        // Detect issue-fix skip message and mark assess/plan as skipped
        if (content.includes("显式 GitCode issue 修复任务，跳过 assess/plan")) {
          for (const skipped of ["assess", "plan"]) {
            if (!completedStages.includes(skipped)) {
              completedStages.push(skipped);
              skippedStages.push(skipped);
              stagesWithResult.add(skipped);
            }
          }
        }

        // Regular stage message
        const stage = log.stage || "";
        if (currentStage !== stage) {
          currentStage = stage;
        }

        sections.push({
          type: "stage",
          content: content,
          stage: stage,
          stages: pipelineInfo?.stages,
          pipeline: pipelineInfo?.pipeline,
          completed_stages: [...completedStages],
          skipped_stages: [...skippedStages],
        });
        break;

      case "harness.stage_result":
        // Check if this is an extension-level event (scope === 'extension')
        const scope = log.scope || '';
        const extName = log.extension_name;
        const extStage = log.extension_stage || '';

        // Handle merge_ext: show concise merge info highlighting N→1, no extension matrix
        if (extStage === 'merge_ext') {
          const mergeStatus = log.status || 'pending';
          const extCount = extensionOrder.length;
          const mergeLabel = extCount > 0 ? `${extCount} 个扩展 → 1 个运行时扩展` : '合并扩展';
          const mergeText = mergeStatus === 'success' ? `✅ ${extName}: ${mergeLabel}完成` : mergeStatus === 'failed' ? `❌ ${extName}: ${mergeLabel}失败` : `⏳ ${extName}: ${mergeLabel}`;
          if (mergeStatus === 'failed') hasFailure = true;
          sections.push({
            type: "info",
            content: mergeText,
          });
          break;
        }

        if (scope === 'extension' && extName) {
          // Extension-level progress update
          const extStatus = (log.status || 'pending') as ExtensionProgressStatus;

          // Track extension failures
          if (extStatus === 'failed' || extStatus === 'timeout' || extStatus === 'rejected') {
            hasFailure = true;
          }

          // Skip merged_extensions from extension matrix (it's a merge container)
          // Show activation status as info line, not full stage render
          if (extName === 'merged_extensions') {
            if (extStage === 'activate_ext') {
              const actText = extStatus === 'success' ? '✅ 激活合并扩展完成' : extStatus === 'failed' ? '❌ 激活合并扩展失败' : '⏳ 激活合并扩展进行中';
              sections.push({
                type: "info",
                content: actText,
              });
            }
            break;
          }

          // Activate-stage extension events should not be added to the design extension
          // matrix — they are runtime activations shown as separate info lines
          if (extStage === 'activate_ext' || log.parent_stage === 'activate') {
            const actText = extStatus === 'success' ? `✅ 激活 ${extName} 完成` : extStatus === 'failed' ? `❌ 激活 ${extName} 失败` : `⏳ 激活 ${extName}`;
            sections.push({
              type: "info",
              content: actText,
            });
            // Still update activateStatus for extensions that were in the matrix from build_verify
            const existing = extensionsByName[extName];
            if (existing) {
              existing.activateStatus = extStatus;
            }
            break;
          }

          // Add to extension order if new (only design extensions, not merged)
          if (!extensionOrder.includes(extName)) {
            extensionOrder.push(extName);
          }

          // Get or create extension info
          const existing = extensionsByName[extName] || {
            extensionName: extName,
            implementStatus: 'pending',
            verifyStatus: 'pending',
            activateStatus: 'pending',
          };

          // Update specific extension stage status
          if (extStage === 'implement_ext') {
            existing.implementStatus = extStatus;
          } else if (extStage === 'verify_ext') {
            existing.verifyStatus = extStatus;
          } else if (extStage === 'activate_ext' || log.parent_stage === 'activate') {
            existing.activateStatus = extStatus;
          }

          extensionsByName[extName] = existing;

          // Output section showing updated extension status matrix
          // Deep-copy extensionsByName so each section captures the state at that point,
          // not the final state (all sections share the same mutable dict otherwise)
          const extensionsSnapshot: Record<string, ExtensionProgressInfo> = {};
          for (const [key, val] of Object.entries(extensionsByName)) {
            extensionsSnapshot[key] = { ...val };
          }
          sections.push({
            type: "stage",
            content: `扩展 ${extName} ${extStage} ${extStatus}`,
            stage: log.stage || log.parent_stage || currentStage || "",
            status: extStatus,
            stages: pipelineInfo?.stages,
            pipeline: pipelineInfo?.pipeline,
            completed_stages: [...completedStages],
            stages_with_success_result: [...stagesWithSuccessResult],
            stages_with_result: [...stagesWithResult],
            extension_order: [...extensionOrder],
            extensions_by_name: extensionsSnapshot,
            gap_count: gapCount,
            skipped_stages: [...skippedStages],
          });
          break;
        }

        if (log.stage && !scope && (log.status === 'success' || log.status === 'failed') && !completedStages.includes(log.stage)) {
          // extended_evolve_pipeline: build_verify 不在此处计入完成，等 activate 出现后再计入
          if (pipelineInfo?.pipeline === 'extended_evolve_pipeline' && log.stage === 'build_verify') {
            stagesWithResult.add(log.stage);
          } else {
            completedStages.push(log.stage);
            stagesWithResult.add(log.stage);
          }
          if (log.status === 'failed') hasFailure = true;
          if (log.status === 'success') {
            stagesWithSuccessResult.add(log.stage);
          }
        }
        if (log.stage && !scope) {
          stagesAppeared.add(log.stage);
        }
        if (log.stage && !scope && log.stage !== currentStage) {
          currentStage = log.stage;
        }

        // extended_evolve_pipeline: activate出现时将build_verify计入完成
        if (pipelineInfo?.pipeline === 'extended_evolve_pipeline' && log.stage === 'activate' && !scope) {
          if (!completedStages.includes('build_verify') && stagesWithResult.has('build_verify')) {
            completedStages.push('build_verify');
          }
        }

        // activate阶段跳过running状态，仅显示最终success
        if (log.stage === 'activate' && log.status === 'running' && !scope) {
          break;
        }

        // meta_evolve_pipeline: 统计CI修复次数
        const stageMessages = log.messages || [];
        if (pipelineInfo?.pipeline === "meta_evolve_pipeline" && log.stage === "verify") {
          for (const msg of stageMessages) {
            if (msg.includes('修复循环') || msg.includes('[修复循环]')) {
              ciFixCount++;
            }
          }
        }

        // Extract gap count from assess stage messages (Gaps: ...)
        if (log.stage === 'assess' && stageMessages.length > 0) {
          const gaps = parseNamedList(stageMessages, 'Gaps:');
          gapCount = gaps.length;
        }

        // Extract extension names from plan stage messages (Designs: ...)
        if (log.stage === 'plan' && stageMessages.length > 0) {
          for (const msg of stageMessages) {
            if (msg.startsWith('Designs:')) {
              const designs = msg.slice('Designs:'.length).trim().split(',');
              for (const design of designs) {
                const name = design.trim();
                if (name && !extensionOrder.includes(name)) {
                  extensionOrder.push(name);
                  extensionsByName[name] = {
                    extensionName: name,
                    implementStatus: 'pending',
                    verifyStatus: 'pending',
                    activateStatus: 'pending',
                  };
                }
              }
            }
          }
        }

        // Deep-copy extension info so each section captures state at that point
        const stageExtSnapshot: Record<string, ExtensionProgressInfo> = {};
        for (const [key, val] of Object.entries(extensionsByName)) {
          stageExtSnapshot[key] = { ...val };
        }
        sections.push({
          type: "stage",
          content: `阶段完成: ${log.stage || "unknown"}`,
          stage: log.stage,
          status: log.status,
          stages: pipelineInfo?.stages,
          pipeline: pipelineInfo?.pipeline,
          completed_stages: [...completedStages],
          stages_with_success_result: [...stagesWithSuccessResult],
          stages_with_result: [...stagesWithResult],
          // Include extension info for display (snapshot at this point)
          extension_order: [...extensionOrder],
          extensions_by_name: stageExtSnapshot,
          // Include gap count for inline progress bar
          gap_count: gapCount,
          stage_messages: stageMessages.length > 0 ? stageMessages : undefined,
          ci_fix_count: ciFixCount,
          skipped_stages: [...skippedStages],
        });
        break;

      case "harness.session_finished":
        // Mark the final stage as completed when session ends
        if (currentStage && !completedStages.includes(currentStage)) {
          completedStages.push(currentStage);
        }

        let finalStatus: string;
        const pipelineType = pipelineInfo?.pipeline || log.pipeline || "";

        if (pipelineType === "extended_evolve_pipeline") {
          // Rule: build_verify just needs to appear (presence check), check if activate has success result
          // activate stage must have success status for the pipeline to be successful
          const hasBuildVerifyAppeared = stagesAppeared.has('build_verify');
          const hasActivateSuccess = stagesWithSuccessResult.has('activate');
          finalStatus = hasBuildVerifyAppeared && hasActivateSuccess ? "success" : "failed";
        } else if (pipelineType === "meta_evolve_pipeline") {
          // Rule: every stage must have harness.stage_result with success status
          // (issue-fix 模式下跳过的阶段视为成功)
          const expectedStages = pipelineInfo?.stages?.map(s => s.slot) || [];
          const skippedSet = new Set(skippedStages);
          const allStagesSuccessful = expectedStages.length > 0 &&
            expectedStages.every(stage =>
              stagesWithSuccessResult.has(stage) || skippedSet.has(stage)
            );
          finalStatus = allStagesSuccessful ? "success" : "failed";
        } else {
          // 仅根据 stage_result 判断任务成败，chat.error 不影响最终状态
          finalStatus = hasFailure ? "failed" : "success";
        }

        sections.push({
          type: "session_finished",
          content: finalStatus === "success" ? "任务执行成功" : `任务执行${finalStatus}`,
          status: finalStatus,
          pipeline: log.pipeline,
        });
        break;

      case "harness.extension_ready":
        // Extension ready: show directory structure and components summary
        const extReadyName = log.extension_name || "unknown";
        const compSummary = log.components_summary;
        const compParts: string[] = [];
        if (compSummary) {
          if (compSummary.rails && compSummary.rails > 0) compParts.push(`${compSummary.rails} rails`);
          if (compSummary.tools && compSummary.tools > 0) compParts.push(`${compSummary.tools} tools`);
          if (compSummary.skills && compSummary.skills > 0) compParts.push(`${compSummary.skills} skills`);
        }
        const compDisplay = compParts.length > 0 ? compParts.join(', ') : '无组件';
        sections.push({
          type: "extension_ready",
          content: `扩展 ${extReadyName} 已就绪`,
          stage: currentStage || "activate",
          extension_name: extReadyName,
          runtime_path: log.extension_runtime_path || log.runtime_path,
          components_summary: compSummary,
        });
        // Also show components count as info line
        if (compParts.length > 0) {
          sections.push({
            type: "info",
            content: `📦 ${extReadyName}: ${compDisplay}`,
          });
        }
        break;

      case "harness.activate_interaction":
        // Activation interaction prompt — show what's being activated
        const actExtName = log.extension_name || "unknown";
        sections.push({
          type: "activate_interaction",
          content: `等待激活确认: ${actExtName}`,
          stage: currentStage || "activate",
          interaction_id: log.interaction_id,
          extension_name: actExtName,
        });
        break;

      default:
        if (content) {
          sections.push({ type: "info", content: `[${eventType}] ${content.substring(0, 100)}` });
        }
        break;
    }
  }

  return { sections, state: { pipelineInfo, completedStages, currentStage, extensionOrder, extensionsByName, gapCount, ciFixCount, hasFailure, stagesWithSuccessResult, stagesWithResult, stagesAppeared, skippedStages } };
}

// Format log section for history display (detailed with colors)
const scheduleCancelCommand: SlashCommand = {
  name: "cancel",
  description: "取消任务",
  usage: "/auto-harness schedule cancel <task_id>",
  kind: CommandKind.BUILT_IN,
  takesArgs: true,
  completion: async (ctx, partial) => {
    // Fetch task list for task_id completion
    try {
      const result = await ctx.request<{ tasks?: Array<{ task_id: string; status?: string }> }>("schedule.list", {}, 5000);
      const tasks = result.tasks || [];
      // Only show non-cancelled tasks
      const activeTasks = tasks.filter((t) => t.status !== "cancelled");
      const prefix = partial.trim().toLowerCase();
      if (!prefix) return activeTasks.map((t) => t.task_id);
      return activeTasks.filter((t) => t.task_id.toLowerCase().startsWith(prefix)).map((t) => t.task_id);
    } catch {
      return [];
    }
  },
  action: async (ctx, args) => {
    const task_id = args.trim();

    if (!task_id) {
      ctx.addItem(
        addError(ctx.sessionId, "用法: /auto-harness schedule cancel <task_id>\n示例: /auto-harness schedule cancel sch_abc123")
      );
      return;
    }

    const result = await ctx.request<{ error?: string; task_id?: string }>("schedule.cancel", { task_id });

    if (result.error) {
      ctx.addItem(
        addError(ctx.sessionId, `取消失败: ${result.error}`)
      );
      return;
    }

    ctx.addItem(
      addInfo(ctx.sessionId, `\n🛑 任务已取消: ${result.task_id}\n💡 使用 /auto-harness schedule list 查看所有任务\n`)
    );
  },
};

const scheduleDeleteCommand: SlashCommand = {
  name: "delete",
  description: "删除任务（取消并移除）",
  usage: "/auto-harness schedule delete <task_id>",
  kind: CommandKind.BUILT_IN,
  takesArgs: true,
  completion: async (ctx, partial) => {
    // Fetch task list for task_id completion
    try {
      const result = await ctx.request<{ tasks?: Array<{ task_id: string }> }>("schedule.list", {}, 5000);
      const tasks = result.tasks || [];
      const prefix = partial.trim().toLowerCase();
      if (!prefix) return tasks.map((t) => t.task_id);
      return tasks.filter((t) => t.task_id.toLowerCase().startsWith(prefix)).map((t) => t.task_id);
    } catch {
      return [];
    }
  },
  action: async (ctx, args) => {
    const task_id = args.trim();

    if (!task_id) {
      ctx.addItem(
        addError(ctx.sessionId, "用法: /auto-harness schedule delete <task_id>\n示例: /auto-harness schedule delete sch_abc123")
      );
      return;
    }

    const result = await ctx.request<{ error?: string; task_id?: string }>("schedule.delete", { task_id });

    if (result.error) {
      ctx.addItem(
        addError(ctx.sessionId, `删除失败: ${result.error}`)
      );
      return;
    }

    ctx.addItem(
      addInfo(ctx.sessionId, `\n🗑️ 任务已删除: ${result.task_id}\n`)
    );
  },
};

// Schedule parent command

const scheduleCommand: SlashCommand = {
  name: "schedule",
  description: "任务管理",
  usage: "/auto-harness schedule <start|list|status|logs|cancel|delete>",
  kind: CommandKind.BUILT_IN,
  takesArgs: true,
  subCommands: [scheduleStartCommand, scheduleListCommand, scheduleStatusCommand, scheduleLogsCommand, scheduleCancelCommand, scheduleDeleteCommand],
  completion: (_ctx, partial) => {
    const subNames = ["start", "list", "status", "logs", "cancel", "delete"];
    const prefix = partial.trim().toLowerCase();
    if (!prefix) return subNames;
    return subNames.filter((n) => n.startsWith(prefix));
  },
  action: (ctx, args) => {
    const subcommand = args.trim().split(/\s+/)[0];
    if (!subcommand) {
      ctx.addItem(
        addError(ctx.sessionId, "用法: /auto-harness schedule <子命令> [参数]\n子命令:\n  start   创建定时任务\n  list    列出所有任务\n  status  查看任务详情\n  logs    查看执行日志（实时跟踪或历史）\n  cancel  取消任务\n  delete  删除任务\n示例:\n  /auto-harness schedule list\n  /auto-harness schedule logs sch_abc123")
      );
      return;
    }
    const validSubs = ["start", "list", "status", "logs", "cancel", "delete"];
    if (!validSubs.includes(subcommand)) {
      ctx.addItem(
        addError(ctx.sessionId, `未知子命令 "${subcommand}"\n用法: /auto-harness schedule <start|list|status|logs|cancel|delete>`)
      );
    }
  },
};

// Run command - one-time task execution

const runCommand: SlashCommand = {
  name: "run",
  description: "执行一次性 auto_harness 任务",
  usage: "/auto-harness run [--pipeline <pipeline>] <query>",
  example: "/auto-harness run --pipeline extended_evolve_pipeline 优化数据库查询性能",
  kind: CommandKind.BUILT_IN,
  takesArgs: true,
  completion: (_ctx, partial) => {
    const parts = partial.trim().split(/\s+/).filter(Boolean);
    // Check pipeline completions (handles --pipeline/-p and values with preserved args)
    return getPipelineCompletions(partial, parts);
  },
  action: async (ctx, args) => {
    const parsed = parseRunArgs(args);

    if (!parsed.query) {
      ctx.addItem(
        addError(ctx.sessionId, "用法: /auto-harness run [--pipeline <类型>] <目标>\nPipeline类型:\n  optimize_expert_harness  - 生成扩展包（本地harness package）\n  optimize_meta_harness    - 提交PR（需配置git）\n示例:\n  /auto-harness run 优化数据库查询性能\n  /auto-harness run --pipeline optimize_expert_harness 优化上下文压缩能力")
      );
      return;
    }

    // Ask user to select pipeline if not specified
    let pipeline = parsed.pipeline;
    if (!pipeline) {
      try {
        const [answer] = await ctx.askQuestions([
          {
            header: "Pipeline",
            question: "请选择 Pipeline 类型:",
            options: [
              { label: "optimize_expert_harness", description: PIPELINE_DISPLAY_NAMES.optimize_expert_harness.desc },
              { label: "optimize_meta_harness", description: PIPELINE_DISPLAY_NAMES.optimize_meta_harness.desc },
            ],
          },
        ]);
        pipeline = answer.selected_options[0];
      } catch {
        // User cancelled
        ctx.addItem(addInfo(ctx.sessionId, "已取消创建任务"));
        return;
      }
    }

    // Validate pipeline value (accept both friendly and backend names)
    const resolvedPipeline = resolvePipelineName(pipeline);
    if (!PIPELINE_BACKEND_VALUES.includes(resolvedPipeline)) {
      ctx.addItem(
        addError(ctx.sessionId, `无效的 pipeline: ${pipeline}\n可选值: ${PIPELINE_DISPLAY_KEYS.join(", ")}`)
      );
      return;
    }

    // For optimize_meta_harness, check git config
    if (resolvedPipeline === "meta_evolve_pipeline") {
      const configCheck = await ctx.request<{ valid: boolean; missing_fields?: Array<{ id: string; prompt: string }> }>("schedule.check_config", {});

      const missingFields = configCheck.missing_fields as Array<{ id: string; prompt: string }> | undefined;
      if (missingFields && missingFields.length > 0) {
        const missingList = missingFields.map(f => `  - ${f.prompt}`).join("\n");
        ctx.addItem(
          addInfo(ctx.sessionId, `optimize_meta_harness 需要配置 git 信息:\n${missingList}\n\n请使用 /config edit 配置这些字段后重试`)
        );
        return;
      }
    }

    ctx.addItem(addInfo(ctx.sessionId, `\n🚀 正在创建一次性任务...\nPipeline: ${pipelineDisplayLabel(pipeline)}\n`, "i"));

    // Create and execute one-time task
    const result = await ctx.request<{ error?: string; task_id?: string; status?: string; message?: string }>("schedule.run", {
      query: parsed.query,
      pipeline: resolvedPipeline,
    });

    if (result.error) {
      ctx.addItem(
        addError(ctx.sessionId, `创建失败: ${result.error}`)
      );
      return;
    }

    ctx.addItem(
      addInfo(ctx.sessionId, `\n🚀 任务已创建并开始执行\n━━━━━━━━━━━━━━━━━━━━━━\n任务ID: ${result.task_id}\nPipeline: ${pipelineDisplayLabel(pipeline)}\n状态: ${result.status}\n━━━━━━━━━━━━━━━━━━━━━━\n💡 正在进入实时日志跟踪模式...\n   按 Ctrl+C 可中断日志查看，任务将继续后台运行\n`)
    );

    // Start streaming logs
    if (result.task_id) {
      await streamCurrentLogs(ctx, result.task_id);
    }
  },
};

const issueScanCommand: SlashCommand = {
  name: "scan",
  description: "扫描仓库所有 GitCode issue 并生成分析矩阵",
  usage: "/auto-harness issue scan [--page <页码>] [--labels <标签>]",
  example: "/auto-harness issue scan --repo jiuwenswarm",
  kind: CommandKind.BUILT_IN,
  takesArgs: true,
  action: async (ctx, args) => {
    const parts = parseArgs(args);
    let repo = "";
    let forceRefresh = "";  // 空值触发交互
    let page = 1;
    let labels = "";  // 默认只显示 bug 类型

    for (let i = 0; i < parts.length; i++) {
      const part = parts[i];
      if (part === "--repo" && i + 1 < parts.length) {
        repo = parts[i + 1];
        i += 1;
      } else if (part === "--force-refresh") {
        forceRefresh = "yes";
      } else if (part === "--page" && i + 1 < parts.length) {
        const parsed = parseInt(parts[i + 1], 10);
        if (Number.isFinite(parsed) && parsed > 0) {
          page = parsed;
        }
        i += 1;
      } else if (part === "--labels" && i + 1 < parts.length) {
        labels = parts[i + 1];
        i += 1;
      } else if (part === "--all") {
        // --all 表示显示所有类型，等同于 --labels ""
        labels = "";
      }
    }

    // Repo 二选一交互：未指定则弹出选择框
    if (!repo) {
      const [answer] = await ctx.askQuestions([
        {
          header: "仓库",
          question: "请选择目标仓库:",
          options: [
            { label: "jiuwenswarm", description: REPO_OPTIONS.jiuwenswarm.desc },
            { label: "agent_core", description: REPO_OPTIONS.agent_core.desc },
          ],
        },
      ]);
      if (!answer.selected_options[0]) {
        ctx.addItem(addInfo(ctx.sessionId, "已取消"));
        return;
      }
      repo = resolveRepoName(answer.selected_options[0]);
    } else {
      repo = resolveRepoName(repo);
    }

    // 强制刷新二选一交互：未指定则弹出选择框
    if (!forceRefresh) {
      const [answer] = await ctx.askQuestions([
        {
          header: "刷新",
          question: "是否强制刷新（重新调用 GitCode API）？",
          options: [
            { label: "否（推荐）", description: "使用缓存数据，快速返回" },
            { label: "是", description: "强制调用 API，更新最新数据" },
          ],
        },
      ]);
      if (!answer.selected_options[0]) {
        ctx.addItem(addInfo(ctx.sessionId, "已取消"));
        return;
      }
      forceRefresh = answer.selected_options[0] === "是" ? "yes" : "no";
    }

    const actualForceRefresh = forceRefresh === "yes";

    ctx.addItem(addInfo(ctx.sessionId, `\n正在扫描 GitCode issue 矩阵...\n仓库: ${repo}\n${labels ? `标签过滤: ${labels}\n` : "显示所有类型\n"}${actualForceRefresh ? "强制刷新所有\n" : ""}${page > 1 ? `页码: ${page}\n` : ""}`, "i"));

    const result = await ctx.request<IssueMatrixResult>("issue.matrix", {
      repo,
      force_refresh: actualForceRefresh,
      page,
      labels,
    });

    ctx.addItem(addInfo(ctx.sessionId, formatIssueMatrix(result)));
  },
};

const issueFixCommand: SlashCommand = {
  name: "fix",
  description: "指定单个或多个 GitCode issue 创建独立修复任务",
  usage: "/auto-harness issue fix <issue_number(s)>",
  example: "/auto-harness issue fix 1272,1271,1270",
  kind: CommandKind.BUILT_IN,
  takesArgs: true,
  action: async (ctx, args) => {
    const parsed = parseIssueFixArgs(args, resolvePipelineName);
    if (parsed.issue_numbers.length === 0) {
      ctx.addItem(addError(ctx.sessionId, "请提供 issue 编号\n示例: /auto-harness issue fix 1272,1271,1270"));
      return;
    }

    // Repo 二选一交互：未指定则弹出选择框
    if (!parsed.repo) {
      const [answer] = await ctx.askQuestions([
        {
          header: "仓库",
          question: "请选择目标仓库:",
          options: [
            { label: "jiuwenswarm", description: REPO_OPTIONS.jiuwenswarm.desc },
            { label: "agent_core", description: REPO_OPTIONS.agent_core.desc },
          ],
        },
      ]);
      if (!answer.selected_options[0]) {
        ctx.addItem(addInfo(ctx.sessionId, "已取消"));
        return;
      }
      parsed.repo = resolveRepoName(answer.selected_options[0]);
    } else {
      // 用户指定了 --repo，解析别名或完整值
      parsed.repo = resolveRepoName(parsed.repo);
    }

    if (!PIPELINE_BACKEND_VALUES.includes(parsed.pipeline)) {
      ctx.addItem(addError(ctx.sessionId, `无效的 pipeline: ${parsed.pipeline}`));
      return;
    }

    // 难度已在 scan 矩阵中展示，默认 medium，不再交互
    if (!ISSUE_DIFFICULTY_VALUES.has(parsed.max_auto_difficulty)) {
      ctx.addItem(addError(ctx.sessionId, `无效的难度上限: ${parsed.max_auto_difficulty}\n可选值: low, medium, high, unclear`));
      return;
    }

    const configCheck = await ctx.request<{ valid: boolean; missing_fields?: Array<{ id: string; prompt: string }> }>("schedule.check_config", {});
    const missingFields = configCheck.missing_fields as Array<{ id: string; prompt: string }> | undefined;
    if (missingFields && missingFields.length > 0) {
      const missingList = missingFields.map(f => `  - ${f.prompt}`).join("\n");
      ctx.addItem(addInfo(ctx.sessionId, `GitCode issue 自动处理需要配置:\n${missingList}\n\n请使用 /config edit 配置这些字段后重试`));
      return;
    }

    const isDryRun = parsed.dry_run === "yes";
    ctx.addItem(addInfo(ctx.sessionId, `\n正在为指定 GitCode issue 创建修复任务...\n仓库: ${parsed.repo}\nIssue: ${parsed.issue_numbers.map(n => `#${n}`).join(", ")}\n难度上限: ${parsed.max_auto_difficulty}\n执行模式: ${isDryRun ? "预演" : "正式执行"}\n并发启动: ${parsed.concurrency}\n`, "i"));
    const result = await ctx.request<{
      error?: string;
      fetched?: number;
      started?: Array<{ number?: number; issue?: number; task_id?: string; status?: string; title?: string }>;
      skipped?: Array<{ issue?: number; reason?: string; status?: string }>;
      reconciled?: Array<{ number?: number; status?: string; pr_url?: string }>;
    }>("issue.watch_once", {
      repo: parsed.repo,
      issue_numbers: parsed.issue_numbers,
      max_issues: parsed.issue_numbers.length,
      dry_run: isDryRun,
      comment_on_start: parsed.comment_on_start,
      pipeline: parsed.pipeline,
      max_auto_difficulty: parsed.max_auto_difficulty,
      start_interval_seconds: issueStartIntervalSeconds(parsed.concurrency),
    });

    if (result.error) {
      ctx.addItem(addError(ctx.sessionId, `创建失败: ${result.error}`));
      return;
    }

    ctx.addItem(addInfo(ctx.sessionId, formatIssueWatchResult(result)));
    if (isDryRun) {
      return;
    }

    // Collect created task IDs for guidance display
    const taskEntries = (result.started || [])
      .map(s => ({ issue: s.number ?? s.issue ?? 0, taskId: s.task_id || "" }))
      .filter(e => e.taskId);

    if (taskEntries.length > 0) {
      const lines: string[] = ["\n后台任务已提交执行", "━━━━━━━━━━━━━━━━━━━━━━"];
      for (const { issue, taskId } of taskEntries) {
        lines.push(`  #${issue}   task: ${taskId}`);
      }
      lines.push("━━━━━━━━━━━━━━━━━━━━━━");
      lines.push("💡 查看进度:");
      lines.push("   /auto-harness issue status             查看 issue 总体进度");
      if (taskEntries.length === 1) {
        lines.push(`   /auto-harness schedule status ${taskEntries[0].taskId}   查看该任务详细日志`);
      } else {
        lines.push("   /auto-harness schedule status <task_id>   查看各任务详细日志");
      }
      ctx.addItem(addInfo(ctx.sessionId, lines.join("\n")));
    }
  },
};

const issueDeleteCommand: SlashCommand = {
  name: "delete",
  description: "删除 issue 处理记录和运行日志",
  usage: "/auto-harness issue delete <issue_numbers> [--completed] [--failed]",
  example: "/auto-harness issue delete 123 456",
  kind: CommandKind.BUILT_IN,
  takesArgs: true,
  action: async (ctx, args) => {
    const parts = parseArgs(args);
    const issueNumbers: number[] = [];
    let deleteCompleted = false;
    let deleteFailed = false;

    for (const part of parts) {
      if (part === "--completed") {
        deleteCompleted = true;
      } else if (part === "--failed") {
        deleteFailed = true;
      } else {
        const num = parseInt(part, 10);
        if (Number.isFinite(num) && num > 0) {
          issueNumbers.push(num);
        }
      }
    }

    if (issueNumbers.length === 0 && !deleteCompleted && !deleteFailed) {
      ctx.addItem(addError(ctx.sessionId, "用法: /auto-harness issue delete <issue_numbers> [--completed] [--failed]\n示例:\n  /auto-harness issue delete 123\n  /auto-harness issue delete 123 456\n  /auto-harness issue delete --completed\n  /auto-harness issue delete --failed"));
      return;
    }

    // 调用后端 delete RPC
    const result = await ctx.request<{
      error?: string;
      deleted?: Array<{ issue: number; task_id?: string; log_size?: string }>;
      rejected?: Array<{ issue: number; reason: string }>;
    }>("issue.delete", {
      issue_numbers: issueNumbers,
      delete_completed: deleteCompleted,
      delete_failed: deleteFailed,
    });

    if (result.error) {
      ctx.addItem(addError(ctx.sessionId, `删除失败: ${result.error}`));
      return;
    }

    const lines = ["\n删除结果", "━━━━━━━━━━━━━━━━━━━━━━"];
    if (result.deleted && result.deleted.length > 0) {
      lines.push(`已删除: ${result.deleted.length} 条记录`);
      for (const item of result.deleted) {
        lines.push(`  #${item.issue}${item.task_id ? ` task: ${item.task_id}` : ""}`);
      }
    }
    if (result.rejected && result.rejected.length > 0) {
      lines.push(`拒绝删除: ${result.rejected.length} 条`);
      for (const item of result.rejected) {
        lines.push(`  #${item.issue}: ${item.reason}`);
      }
    }
    lines.push("━━━━━━━━━━━━━━━━━━━━━━");
    ctx.addItem(addInfo(ctx.sessionId, lines.join("\n")));
  },
};

const issueStatusCommand: SlashCommand = {
  name: "status",
  description: "查看 GitCode issue 处理状态",
  usage: "/auto-harness issue status",
  kind: CommandKind.BUILT_IN,
  takesArgs: false,
  action: async (ctx, _args) => {
    ctx.addItem(addInfo(ctx.sessionId, "\n正在查询 GitCode issue 处理记录...\n", "i"));
    const result = await ctx.request<{ issues?: Array<{ key?: string; number?: number; status?: string; task_id?: string; task_status?: string; title?: string; pr_url?: string; reason?: string; progress?: AutoHarnessProgress }> }>("issue.state.list", {});
    const issues = result.issues || [];

    // 转换为表格行格式
    const rows: IssueListRow[] = issues.map((issue) => {
      const number = issue.number || parseInt(String(issue.key || "").split("#").pop() || "0", 10);
      const reason = issue.reason || "";
      const stage = issue.progress?.current_stage || issue.progress?.stages?.find(s => s.status === "running")?.stage || "";
      const progressPercent = issue.progress?.stages ? calculateStageProgress(issue.progress.stages) : 0;

      const isTaskDeleted = issue.task_status === "task_deleted";
      const inFlightStatuses = new Set(["task_created", "running"]);
      const effectiveStatus = isTaskDeleted
        ? "task_deleted"
        : (issue.task_id && issue.task_status && inFlightStatuses.has(issue.status || "")
            ? issue.task_status
            : (issue.status || "unknown"));

      // 终态任务进度显示100%或0%
      const terminalStatuses = new Set(["success", "failed", "pr_created", "completed", "completed_without_pr", "complete", "skipped", "needs_human"]);
      const finalProgress = terminalStatuses.has(effectiveStatus) ? (effectiveStatus === "success" || effectiveStatus === "pr_created" || effectiveStatus === "completed" || effectiveStatus === "completed_without_pr" || effectiveStatus === "complete" ? 100 : 0) : progressPercent;

      let details = "";
      if (issue.pr_url) {
        details = `PR: ${issue.pr_url}`;
      } else if (issue.task_id) {
        details = `task: ${issue.task_id}`;
      } else if (reason) {
        // Strip URLs from reason for compact display
        details = reason.replace(/https?:\/\/\S+/g, "").replace(/;+\s*;+/g, ";").replace(/:\s*$/, "").trim();
      } else if (issue.title) {
        details = issue.title.substring(0, 30);
      }

      return {
        issue: number,
        status: effectiveStatus,
        stage,
        progress: finalProgress,
        details,
        prUrl: issue.pr_url,
      };
    });

    ctx.addItem(addInfo(ctx.sessionId, formatIssueTable(rows)));
  },
};

const issueCommand: SlashCommand = {
  name: "issue",
  description: "GitCode issue 自动处理",
  usage: "/auto-harness issue <scan|fix|status|delete>",
  kind: CommandKind.BUILT_IN,
  takesArgs: true,
  subCommands: [issueFixCommand, issueScanCommand, issueStatusCommand, issueDeleteCommand],
  completion: (_ctx, partial) => {
    const subNames = ["scan", "fix", "status", "delete"];
    const prefix = partial.trim().toLowerCase();
    if (!prefix) return subNames;
    return subNames.filter((n) => n.startsWith(prefix));
  },
  action: (ctx, args) => {
    const text = args.trim();
    if (!text) {
      ctx.addItem(addError(ctx.sessionId, "用法: /auto-harness issue <fix|scan|status|delete>\n示例:\n  /auto-harness issue fix 1272,1271\n  /auto-harness issue scan --repo jiuwenswarm\n  /auto-harness issue status\n  /auto-harness issue delete 123"));
      return;
    }
    const subcommand = text.split(/\s+/)[0];
    const validSubs = ["scan", "fix", "status", "delete"];
    if (!validSubs.includes(subcommand)) {
      ctx.addItem(
        addError(ctx.sessionId, `未知子命令 "${subcommand}"\n用法: /auto-harness issue <scan|fix|status|delete>`)
      );
    }
  },
};

// Main auto-harness command

export function createAutoHarnessCommand(): SlashCommand {
  return {
    name: "auto-harness",
    description: "Auto-Harness 任务管理",
    hidden: false, // Temporarily hidden from TUI, core functionality preserved for future re-enable
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    subCommands: [runCommand, scheduleCommand, issueCommand],
    completion: (_ctx, partial) => {
      const subNames = ["run", "schedule"];
      const prefix = partial.trim().toLowerCase();
      if (!prefix) return subNames;
      return subNames.filter((n) => n.startsWith(prefix));
    },
    action: (ctx, args) => {
      const text = args.trim();
      if (!text) {
        ctx.addItem(
          addError(ctx.sessionId, "用法: /auto-harness <run|schedule|issue> [参数]\n子命令:\n  run       创建并执行一次性任务\n  schedule  管理定时任务\n  issue     处理 GitCode issue\n示例:\n  /auto-harness run 优化上下文压缩能力\n  /auto-harness schedule list\n  /auto-harness issue fix 1272,1271")
        );
        return;
      }
      const subcommand = text.split(/\s+/)[0];
      const validSubs = ["run", "schedule", "issue"];
      if (!validSubs.includes(subcommand)) {
        ctx.addItem(
          addError(ctx.sessionId, `未知子命令 "${subcommand}"\n用法: /auto-harness <run|schedule|issue>\n示例:\n  /auto-harness run 优化上下文压缩能力\n  /auto-harness schedule list`)
        );
      }
    },
  };
}

function formatLocalTime(isoTime?: string): string {
  if (!isoTime) return "未知";
  try {
    const date = new Date(isoTime);
    // Format as local time: YYYY-MM-DD HH:mm
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    const hours = String(date.getHours()).padStart(2, "0");
    const minutes = String(date.getMinutes()).padStart(2, "0");
    return `${year}-${month}-${day} ${hours}:${minutes}`;
  } catch {
    return isoTime;
  }
}

function parseLogArgs(args: string): { task_id: string; log_type: string; history_index: number } {
  const parts = parseArgs(args);

  let log_type = "current";
  let history_index = -1;

  // First, extract --history index if present
  const historyMatch = args.match(/--history\s+(\d+)/);
  const historyIndexValue = historyMatch ? historyMatch[1] : null;

  if (args.includes("--current")) {
    log_type = "current";
    history_index = -1;
  } else if (historyMatch) {
    log_type = "history";
    history_index = parseInt(historyMatch[1], 10);
  }

  // Find task_id: first non-flag argument, excluding history index value
  const task_id = parts.find((p) => {
    return !p.startsWith("-") && p !== historyIndexValue;
  }) || "";

  return { task_id, log_type, history_index };
}
