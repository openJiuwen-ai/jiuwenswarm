import { existsSync } from "node:fs";
import { join } from "node:path";

import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import {
  buildInitPrompt,
  resolveLanguage,
  type ExistingFiles,
  type ScopeKey,
} from "./init.prompts.js";

// ---------------------------------------------------------------------------
// Scope options (labels are UI-only; ScopeKey is the internal identifier).
// ---------------------------------------------------------------------------

interface ScopeOption {
  key: ScopeKey;
  label: string;
  description: string;
}

function getScopeOptions(lang: "zh" | "en"): ScopeOption[] {
  if (lang === "zh") {
    return [
      {
        key: "project",
        label: "团队共享 (JIUWENSWARM.md)",
        description:
          "签入版本库，供团队共用 —— 架构说明、编码规范、常用命令、CI 约定等。",
      },
      {
        key: "personal",
        label: "个人私有 (JIUWENSWARM.local.md)",
        description:
          "只属于你自己，加入 .gitignore —— 个人偏好、沙箱地址、私有凭据、工作习惯。",
      },
      {
        key: "both",
        label: "都要 (团队 + 个人)",
        description: "同时写两份文件。",
      },
    ];
  }
  return [
    {
      key: "project",
      label: "Team-shared (JIUWENSWARM.md)",
      description:
        "Checked into source control — architecture, coding standards, common commands, CI conventions.",
    },
    {
      key: "personal",
      label: "Personal (JIUWENSWARM.local.md)",
      description:
        "Private to you, gitignored — preferences, sandbox URLs, credentials, workflow quirks.",
    },
    {
      key: "both",
      label: "Both (team + personal)",
      description: "Write both files.",
    },
  ];
}

// ---------------------------------------------------------------------------
// Command
// ---------------------------------------------------------------------------

export function createInitCommand(): SlashCommand {
  return {
    name: "init",
    description:
      "Initialize project AI collaboration config (generates JIUWENSWARM.md, optionally JIUWENSWARM.local.md)",
    usage: "/init",
    example: "/init",
    kind: CommandKind.BUILT_IN,
    action: async (ctx) => {
      const language = resolveLanguage(ctx);

      // ---- Guard 1: must be in coding mode ----
      if (!ctx.mode.startsWith("code.")) {
        ctx.addItem(
          addError(
            ctx.sessionId,
            language === "zh"
              ? "/init 需要在 coding 模式下运行。请先执行 /mode code 或 /code 切到 coding 模式再重试。"
              : "/init requires coding mode. Run /mode code or /code first, then try again.",
          ),
        );
        return;
      }

      // ---- Guard 2: /init runs in code.normal; keep UI mode and request mode aligned ----
      if (ctx.mode !== "code.normal") {
        ctx.setMode("code.normal");
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            language === "zh"
              ? "已自动切换到 code.normal 以便 /init 能写文件。"
              : "Switched to code.normal for /init (needs write permission).",
            "i",
          ),
        );
      }

      // ---- Guard 3: workspace root ----
      // ctx.getWorkspaceDir() follows the current project directory, then falls back to process.cwd().
      const rootDir =
        ctx.getWorkspaceDir() ||
        (typeof process !== "undefined" ? process.cwd() : "");
      if (!rootDir) {
        ctx.addItem(
          addError(
            ctx.sessionId,
            language === "zh"
              ? "无法识别工作目录，请先用 /workspace set <path> 指定。"
              : "Cannot resolve workspace directory. Use /workspace set <path> first.",
          ),
        );
        return;
      }

      // ---- Step 1 (local): ask scope via multi-choice ----
      const scopeOptions = getScopeOptions(language);
      // Build a label -> key map so we can reject unknown labels instead of
      // silently defaulting to "both" when the TUI returns something we don't
      // recognise (e.g. user sent a custom_input via free-text).
      const labelToKey = new Map<string, ScopeKey>(
        scopeOptions.map((o) => [o.label, o.key] as const),
      );

      let scopeKey: ScopeKey;
      try {
        const [answer] = await ctx.askQuestions(
          [
            {
              header: language === "zh" ? "范围" : "Scope",
              question:
                language === "zh"
                  ? "要设置哪些 JIUWENSWARM 文件？"
                  : "Which JIUWENSWARM files would you like to set up?",
              options: scopeOptions.map((o) => ({
                label: o.label,
                description: o.description,
              })),
            },
          ],
          "local_command_init",
        );
        const selectedLabel = answer?.selected_options?.[0];
        if (!selectedLabel || !labelToKey.has(selectedLabel)) {
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              language === "zh"
                ? "/init 已取消：未识别到范围选择。"
                : "/init cancelled: no scope selection received.",
              "i",
            ),
          );
          return;
        }
        scopeKey = labelToKey.get(selectedLabel) as ScopeKey;
      } catch (err) {
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            language === "zh"
              ? `/init 已取消：${err instanceof Error ? err.message : String(err)}`
              : `/init cancelled: ${err instanceof Error ? err.message : String(err)}`,
            "i",
          ),
        );
        return;
      }

      // ---- Pre-detect existing memory / legacy AI config files ----
      const existing: ExistingFiles = {
        jiuwenswarmMd: existsSync(join(rootDir, "JIUWENSWARM.md")),
        jiuwenswarmLocalMd: existsSync(join(rootDir, "JIUWENSWARM.local.md")),
        claudeMd: existsSync(join(rootDir, "CLAUDE.md")),
        claudeLocalMd: existsSync(join(rootDir, "CLAUDE.local.md")),
        agentsMd: existsSync(join(rootDir, "AGENTS.md")),
        openjiuwenMd: existsSync(join(rootDir, "OPENJIUWEN.md")),
        cursorRules: existsSync(join(rootDir, ".cursorrules")),
        copilotInstructions: existsSync(
          join(rootDir, ".github", "copilot-instructions.md"),
        ),
      };

      const prompt = buildInitPrompt({ rootDir, scopeKey, existing });

      // ---- Send ----
      // The earlier guard 2 already called setMode("code.normal") if needed.
      // ctx.mode is reactive state and may not reflect the change in the same
      // tick; we therefore pass the mode explicitly to sendMessage so the
      // server still receives "code.normal" even if ctx.mode is briefly stale.
      const requestId = ctx.sendMessage(prompt, undefined, "code.normal", {
        logAsUser: false,
      });
      if (!requestId) {
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            language === "zh"
              ? "当前离线，/init 请求未发送；网络恢复后请重试。"
              : "Offline; /init message not sent. Please retry after reconnecting.",
            "p",
          ),
        );
        return;
      }

      ctx.addItem(
        addInfo(
          ctx.sessionId,
          language === "zh"
            ? `正在启动项目初始化（scope=${scopeKey}）…`
            : `Starting project initialization (scope=${scopeKey})…`,
          "i",
        ),
      );
    },
  };
}
