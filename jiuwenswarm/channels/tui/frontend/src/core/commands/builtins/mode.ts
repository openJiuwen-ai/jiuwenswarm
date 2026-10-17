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
  // 进入 plan 态时标记显式入口（与 /plan 命令一致）。这样随后 chat.send 会携带
  // plan_entry_source=slash_command，放行后端防重入闸门；否则 /mode plan 在
  // plan 已退出过的会话上会被静默拦截回 normal（plan.mode_exited / plan_slug）。
  if (nextMode.endsWith(".plan")) {
    ctx.markPlanEntryFromSlashCommand?.();
  }
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
  // Bare role aliases (no third segment) — default to *.normal variant.
  agent: "agent.work.normal",
  code: "agent.code.normal",
  team: "team.work.normal",
  "agent.work": "agent.work.normal",
  "agent.code": "agent.code.normal",
  "team.work": "team.work.normal",
  "team.code": "team.code.normal",
  "team.normal": "team.work.normal",
  // Explicit variants.
  "agent.work.normal": "agent.work.normal",
  "agent.code.normal": "agent.code.normal",
  "team.work.normal": "team.work.normal",
  "team.code.normal": "team.code.normal",
  // Plan 入口：映射到对应的 *.plan canonical（与 modes.ts LEGACY_MODE_TO_NEW
  // 一致），让旧 muscle memory 的 /mode plan / agent.plan / code.plan 真正进入
  // plan，而不是静默降级到 *.normal。进入 plan 的显式标记见 switchMode。
  plan: "agent.work.plan",
  "agent.plan": "agent.work.plan",
  "code.plan": "agent.code.plan",
  "team.plan": "team.work.plan",
  "team.plan.normal": "team.work.plan",
  "team.plan.code": "team.code.plan",
  // Legacy aliases mapping onto the new canonical 8 values.
  "agent.fast": "agent.work.normal",
  "code.normal": "agent.code.normal",
  "code.team": "team.code.normal",
};

/** Resolve a user-facing `/mode` token to the canonical runtime mode. */
export function resolveModeTarget(requestedMode: string): ClientMode | undefined {
  return MODE_ALIASES[requestedMode.trim()];
}

/**
 * TUI `/mode` 树形展示；两段分组（agent / team），每段下挂 work / code 两项。
 * `.plan` 段统一走 `/plan` 命令对称退出，不出现在此候选词中。
 * pi-tui `AutocompleteItem` 不支持 `disabled`/`header`，组头 `value` 兜底为组首子项，
 * 即"选中组头 = 切组首"。
 */
export function buildModeAutocompleteItems(): AutocompleteItem[] {
  return [
    { value: "agent.work", label: "agent" },
    { value: "agent.work", label: "  agent.work" },
    { value: "agent.code", label: "  agent.code" },
    { value: "team.work", label: "team" },
    { value: "team.work", label: "  team.work" },
    { value: "team.code", label: "  team.code" },
  ];
}

export function createModeCommand(): SlashCommand {
  const directModes = [
    "agent.work",
    "agent.code",
    "team.work",
    "team.code",
  ] as const;

  return {
    name: "mode",
    description: "Switch chat mode",
    usage: "/mode <agent.work|agent.code|team.work|team.code>",
    example: "/mode agent.code",
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
            "usage: /mode <agent.work|agent.code|team.work|team.code>",
          ),
        );
        return;
      }

      await switchMode(ctx, nextMode);
    },
  };
}
