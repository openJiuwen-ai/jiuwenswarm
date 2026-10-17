import { generateCreateToken } from "../../session-state.js";
import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

function normalizePersistContent(rawArgs: string): string {
  return rawArgs.trim();
}

export function createPersistCommand(): SlashCommand {
  return {
    name: "persist",
    description: "Start a Persist Session and send its first task",
    usage: "/persist <task>",
    example: "/persist continue building the login flow",
    argGuide: "<task>",
    takesArgs: true,
    kind: CommandKind.BUILT_IN,
    action: async (ctx, args) => {
      const content = normalizePersistContent(args);
      if (!content) {
        ctx.addItem(addError(ctx.sessionId, "Usage: /persist <task>"));
        return;
      }
      if (ctx.isProcessing) {
        ctx.addItem(addError(ctx.sessionId, "session is busy"));
        return;
      }

      const created = await ctx.request<{ session_id?: string; sessionId?: string }>(
        "session.create",
        {
          create_token: generateCreateToken(),
          previous_session_id: ctx.sessionId,
          previous_mode: ctx.mode,
          persist_session: true,
          mode: ctx.mode,
        },
      );
      const nextId = created.session_id ?? created.sessionId;
      if (!nextId) throw new Error("session.create did not return a session id");

      ctx.updateSession(nextId);
      ctx.clearEntries();
      ctx.addItem(addInfo(nextId, `Started Persist Session ${nextId}`, "i"));
      await ctx.restoreHistory(nextId);
      if (!ctx.sendMessage(content, undefined, ctx.mode)) {
        ctx.addItem(addError(nextId, "failed to send the first Persist Session task"));
      }
    },
  };
}
