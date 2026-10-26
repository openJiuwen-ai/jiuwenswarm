import { generateCreateToken } from "../../session-state.js";
import { addCommandEcho, addError, addInfo } from "../helpers.js";
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

      const created = await ctx.request<{ session_id?: string; sessionId?: string }>(
        "session.create",
        {
          create_token: generateCreateToken(),
          previous_session_id: ctx.sessionId,
          previous_mode: ctx.mode,
          mode: ctx.mode,
        },
      );
      const nextId = created.session_id ?? created.sessionId;
      if (!nextId) throw new Error("session.create did not return a session id");

      ctx.updateSession(nextId);
      ctx.setSessionTitle("");
      ctx.clearEntries();
      ctx.addItem(addCommandEcho(nextId, "/clear"));
      ctx.addItem(addInfo(nextId, `Started a fresh conversation in ${nextId}`, "i"));
      await ctx.restoreHistory(nextId);
    },
  };
}
