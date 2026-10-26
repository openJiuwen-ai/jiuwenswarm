import type { AutocompleteItem } from "@mariozechner/pi-tui";
import type { ClientMode } from "../../modes.js";
import { isTeamMode } from "../../modes.js";
import { makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

/** TUI `/mode` 树形展示；分组行 value 与 modeAlias 默认一致，不修改 pi-tui。 */
export function buildModeAutocompleteItems(): AutocompleteItem[] {
  return [
    { value: "agent", label: "agent" },
    { value: "agent.plan", label: "    plan" },
    { value: "agent.fast", label: "    fast" },
    { value: "code", label: "code" },
    { value: "code.plan", label: "    plan" },
    { value: "code.normal", label: "    normal" },
    { value: "code.team", label: "    team" },
    { value: "team", label: "team" },
    { value: "team.plan", label: "    plan" },
    { value: "team.normal", label: "    normal" },
  ];
}

export function createModeCommand(): SlashCommand {
  const directModes = [
    "agent",
    "code",
    "agent.plan",
    "agent.fast",
    "code.plan",
    "code.normal",
    "code.team",
    "team",
    "team.plan",
    "team.normal",
  ] as const;
  /** 用户输入的简写 → 实际会话模式（/mode agent → agent.plan，/mode code → code.normal）。 */
  const modeAlias: Record<string, ClientMode> = {
    plan: "agent.plan",
    agent: "agent.plan",
    code: "code.normal",
    "agent.plan": "agent.plan",
    "agent.fast": "agent.fast",
    "code.plan": "code.plan",
    "code.normal": "code.normal",
    "code.team": "code.team",
    team: "team",
    "team.plan": "team.plan",
    "team.normal": "team",
  };

  return {
    name: "mode",
    description: "Switch chat mode",
    usage: "/mode <agent|code|code.plan|code.normal|code.team|team|team.plan>",
    example: "/mode code",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async () => [...directModes],
    action: async (ctx, args) => {
      const requestedMode = args.trim();
      // 无参数时显示当前 mode
      if (!requestedMode) {
        const currentMode = ctx.mode ?? "unknown";
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "info",
            `Current mode: ${currentMode}`,
            "m",
          ),
        );
        return;
      }
      const nextMode = modeAlias[requestedMode];
      if (!nextMode) {
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            "usage: /mode <agent|code|code.plan|code.normal|code.team|team|team.plan>",
          ),
        );
        return;
      }

      // Check if switching mode with running team tasks
      const currentMode = ctx.mode;
      if (currentMode !== nextMode && isTeamMode(currentMode) && ctx.hasRunningTeamTasks?.()) {
        const answers = await ctx.askQuestions(
          [
            {
              header: "模式切换",
              question: `当前有 team 任务正在运行，切换到 ${nextMode} 模式会中断这些任务。`,
              options: [
                { label: "中断任务并切换", description: "停止当前任务，切换到新模式" },
                { label: "取消切换", description: "继续执行当前任务" },
              ],
            },
          ],
          "mode_switch_confirm",
        );

        const selected = answers[0]?.selected_options?.[0];
        if (selected !== "中断任务并切换") {
          ctx.addItem(makeItem(ctx.sessionId, "info", "模式切换已取消", "m"));
          return;
        }
        // User confirmed interrupt, send cancel request
        ctx.sendEventOnly("chat.interrupt", { intent: "cancel", mode: currentMode });
      }

      // Optimistically update this.mode BEFORE the async mode.set round-trip.
      // Otherwise a message sent immediately after /mode (e.g. /debug) reads
      // the stale mode in sendMessage and is dispatched with the old mode.
      // mode.set is best-effort: many backends carry the mode per chat.send, so
      // a failed mode.set is ignored (matching prior behavior — no rollback).
      ctx.setMode(nextMode);
      ctx.addItem(makeItem(ctx.sessionId, "info", `Mode set to ${nextMode}`, "m"));
      try {
        await ctx.request("mode.set", { mode: nextMode });
      } catch {
        // Some backends still accept mode only on chat.send.
      }
    },
  };
}
