import { CommandKind, type SlashCommand } from "../types.js";

export function createHistoryCommand(): SlashCommand {
  return {
    name: "history",
    description: "Reload current session history",
    usage: "/history",
    example: "/history",
    kind: CommandKind.BUILT_IN,
    action: async (ctx) => {
      ctx.clearEntries();
      await ctx.restoreHistory(ctx.sessionId);
    },
  };
}
