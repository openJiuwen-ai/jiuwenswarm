import { generateSessionId } from "../../session-state.js";
import { makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export function createNewCommand(): SlashCommand {
  return {
    name: "new",
    description: "Create and switch to a session",
    usage: "/new [id]",
    example: "/new trip-plan",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      if (ctx.isProcessing) {
        ctx.addItem(makeItem(ctx.sessionId, "error", "session is busy"));
        return;
      }
      const nextId = args.trim() || generateSessionId();
      await ctx.request("session.create", { session_id: nextId });
      ctx.updateSession(nextId);
      ctx.clearEntries();
      ctx.addItem(makeItem(nextId, "info", `Switched to session ${nextId}`, "i"));
      await ctx.restoreHistory(nextId);
    },
  };
}
