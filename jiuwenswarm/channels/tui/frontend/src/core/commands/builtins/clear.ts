import { generateCreateToken } from "../../session-state.js";
import { addCommandEcho, addError, addInfo, parseArgs } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

function shouldPersistSessionFromArgs(rawArgs: string): boolean {
  const parts = parseArgs(rawArgs);
  return parts.includes("--persist") || parts.includes("--persist-session");
}

export function createClearCommand(): SlashCommand {
  return {
    name: "clear",
    altNames: ["reset"],
    description: "Clear conversation history and free up context",
    usage: "/clear [--persist|--persist-session]",
    example: "/new",
    argGuide: "[--persist|--persist-session]",
    takesArgs: true,
    kind: CommandKind.BUILT_IN,
    action: async (ctx, args) => {
      if (ctx.isProcessing) {
        ctx.addItem(
          addError(ctx.sessionId, "session is busy; stop the current run before clearing"),
        );
        return;
      }
      const persistSession = shouldPersistSessionFromArgs(args);

      const created = await ctx.request<{ session_id?: string; sessionId?: string }>(
        "session.create",
        {
          create_token: generateCreateToken(),
          previous_session_id: ctx.sessionId,
          previous_mode: ctx.mode,
          ...(persistSession ? { persist_session: true } : {}),
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
