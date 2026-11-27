import { generateSessionId } from "../../session-state.js";
import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export function createClearCommand(): SlashCommand {
  return {
    name: "clear",
    altNames: ["reset", "new"],
    description: "Clear conversation history and free up context",
    usage: "/clear",
    example: "/new",
    kind: CommandKind.BUILT_IN,
    action: async (ctx) => {
      if (ctx.isProcessing) {
        ctx.addItem(
          addError(ctx.sessionId, "session is busy; stop the current run before clearing"),
        );
        return;
      }

      const nextId = generateSessionId();
      try {
        await ctx.request("session.create", { session_id: nextId });
      } catch {
        // Some backends may create the session lazily on first message.
      }

      ctx.updateSession(nextId);
      ctx.setSessionTitle("");
      ctx.clearEntries();
      ctx.addItem(addInfo(nextId, `Started a fresh conversation in ${nextId}`, "i"));
      await ctx.restoreHistory(nextId);
    },
  };
}
