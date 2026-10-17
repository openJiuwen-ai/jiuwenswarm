import { generateCreateToken } from "../../session-state.js";
import { parseArgs } from "../helpers.js";
import { makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

function shouldPersistSessionFromArgs(rawArgs: string): boolean {
  const parts = parseArgs(rawArgs);
  return parts.includes("--persist") || parts.includes("--persist-session");
}

export function createNewCommand(): SlashCommand {
  return {
    name: "new",
    description: "Create and switch to a session",
    usage: "/new [--persist|--persist-session]",
    example: "/new",
    argGuide: "[--persist|--persist-session]",
    takesArgs: true,
    kind: CommandKind.BUILT_IN,
    action: async (ctx, args) => {
      if (ctx.isProcessing) {
        ctx.addItem(makeItem(ctx.sessionId, "error", "session is busy"));
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
      ctx.clearEntries();
      ctx.addItem(makeItem(nextId, "info", `Switched to session ${nextId}`, "i"));
      await ctx.restoreHistory(nextId);
    },
  };
}
