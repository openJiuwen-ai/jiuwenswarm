import type { AutocompleteItem } from "@mariozechner/pi-tui";
import type { ClientMode } from "../../modes.js";
import { formatModeForDisplay, isTeamMode } from "../../modes.js";
import { makeItem } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";

/** Switch mode while preserving the team-task interruption confirmation. */
export async function switchMode(
  ctx: CommandContext,
  nextMode: ClientMode,
  options: { announce?: boolean } = {},
): Promise<boolean> {
  const currentMode = ctx.mode;
  if (currentMode !== nextMode && isTeamMode(currentMode) && ctx.hasRunningTeamTasks?.()) {
    const displayNextMode = formatModeForDisplay(nextMode);
    const answers = await ctx.askQuestions(
      [
        {
          header: "模式切换",
          question: `当前有 team 任务正在运行，切换到 ${displayNextMode} 模式会中断这些任务。`,
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
      return false;
    }
    ctx.sendEventOnly("chat.interrupt", { intent: "cancel", mode: currentMode });
  }

  // Update locally before the async round-trip so an immediately following
  // message is dispatched with the new mode. The backend call remains
  // best-effort because chat.send also carries the active mode.
  ctx.setMode(nextMode);
  if (options.announce !== false) {
    ctx.addItem(
      makeItem(ctx.sessionId, "info", `Mode set to ${formatModeForDisplay(nextMode)}`, "m"),
    );
  }
  try {
    await ctx.request("mode.set", { mode: nextMode });
  } catch {
    // Some backends still accept mode only on chat.send.
  }
  return true;
}

const MODE_ALIASES: Record<string, ClientMode> = {
  plan: "agent.plan",
  agent: "agent.plan",
  code: "code.normal",
  "agent.plan": "agent.plan",
  "agent.fast": "agent.fast",
  "code.plan": "code.plan",
  "code.normal": "code.normal",
  "code.team": "code.team",
  "team.work": "team",
  "team.code": "code.team",
  team: "team",
  "team.plan": "team.plan.normal",
  "team.plan.normal": "team.plan.normal",
  "team.plan.code": "team.plan.code",
  "team.normal": "team",
};

/** Resolve a user-facing `/mode` token to the canonical runtime mode. */
export function resolveModeTarget(requestedMode: string): ClientMode | undefined {
  return MODE_ALIASES[requestedMode.trim()];
}

/** TUI `/mode` 树形展示；分组行 value 与 modeAlias 默认一致，不修改 pi-tui。 */
export function buildModeAutocompleteItems(): AutocompleteItem[] {
  return [
    { value: "agent", label: "agent" },
    { value: "agent.plan", label: "    plan" },
    { value: "code", label: "code" },
    { value: "code.plan", label: "    plan" },
    { value: "code.normal", label: "    normal" },
    { value: "team.work", label: "team" },
    { value: "team.work", label: "    work" },
    { value: "team.code", label: "    code" },
  ];
}

export function createModeCommand(): SlashCommand {
  const directModes = [
    "agent",
    "code",
    "agent.plan",
    "code.plan",
    "code.normal",
    "team.work",
    "team.code",
  ] as const;

  return {
    name: "mode",
    description: "Switch chat mode",
    usage: "/mode <agent|code|code.plan|code.normal|team.work|team.code>",
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
            `Current mode: ${formatModeForDisplay(currentMode)}`,
            "m",
          ),
        );
        return;
      }
      const nextMode = resolveModeTarget(requestedMode);
      if (!nextMode) {
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            "usage: /mode <agent|code|code.plan|code.normal|team.work|team.code>",
          ),
        );
        return;
      }

      await switchMode(ctx, nextMode);
    },
  };
}
