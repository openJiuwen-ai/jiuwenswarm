import { addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

const CODE_MODES = new Set(["code.normal", "code.team", "code.plan"]);

export function createPlanCommand(): SlashCommand {
  return {
    name: "plan",
    description: "Switch to plan mode, or send a planning request",
    usage: "/plan [open|<description>]",
    example: "/plan outline the migration steps",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: (ctx, args) => {
      if (ctx.mode === "team" || ctx.mode === "team.plan") {
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            "/plan does not apply in team mode; switch mode first (e.g. /mode agent).",
            "p",
          ),
        );
        return;
      }

      const value = args.trim();
      // Preserve the mode family: code.* → code.plan, agent.* → agent.plan
      const target = CODE_MODES.has(ctx.mode) ? "code.plan" : "agent.plan";
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
