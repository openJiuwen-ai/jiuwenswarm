import { addInfo } from "../helpers.js";
import type { ClientMode } from "../../modes.js";
import { CommandKind, type SlashCommand } from "../types.js";

const CODE_MODES = new Set(["code.normal", "code.team", "code.plan"]);

/** Resolve the plan variant while preserving the current agent/team profile. */
export function resolvePlanTarget(mode: ClientMode): ClientMode {
  if (mode === "team" || mode === "team.plan" || mode === "team.plan.normal") {
    return "team.plan.normal";
  }
  if (mode === "code.team" || mode === "team.plan.code") {
    return "team.plan.code";
  }
  return CODE_MODES.has(mode) ? "code.plan" : "agent.plan";
}

export function createPlanCommand(): SlashCommand {
  return {
    name: "plan",
    description: "Switch to plan mode, or send a planning request",
    usage: "/plan [open|<description>]",
    example: "/plan outline the migration steps",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: (ctx, args) => {
      const value = args.trim();
      const target = resolvePlanTarget(ctx.mode);
      if (ctx.mode !== target) {
        ctx.setMode(target);
      }
      ctx.markPlanEntryFromSlashCommand?.();

      if (!value) {
        ctx.addItem(addInfo(ctx.sessionId, "Plan mode enabled", "p"));
        return;
      }

      if (value === "open") {
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            "Plan mode is active. Type your planning request directly or run /plan <description>.",
            "p",
          ),
        );
        return;
      }

      const requestId = ctx.sendMessage(value, undefined, target);
      if (!requestId) {
        ctx.addItem(
          addInfo(ctx.sessionId, "offline: waiting for reconnect before sending plan request", "p"),
        );
      }
    },
  };
}
