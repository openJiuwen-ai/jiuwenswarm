import { addCommandEcho, addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export interface SessionForkPayload {
  session_id?: string;
  source_session_id?: string;
  title?: string;
}

export function createBranchCommand(): SlashCommand {
  return {
    name: "branch",
    altNames: ["fork"],
    description: "Create a branch of the current conversation at this point",
    usage: "/branch [name]",
    example: "/branch fix-login-bug",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      if (ctx.isProcessing) {
        ctx.addItem(
          addError(ctx.sessionId, "session is busy; stop the current run before branching"),
        );
        return;
      }

      const hasMainConversation = ctx.entries.some(
        (e) => e.kind === "user" || e.kind === "assistant",
      );
      if (!hasMainConversation) {
        // Distinguish: no entries at all → "No conversation to branch"
        //               has sidechain/team activity but no main conversation → "No messages to branch"
        const message =
          ctx.teamMessageEvents.length > 0
            ? "No messages to branch"
            : "No conversation to branch";
        ctx.addItem(addError(ctx.sessionId, message));
        return;
      }

      const customTitle = args.trim();
      try {
        const payload = await ctx.request<SessionForkPayload>("session.fork", {
          source_session_id: ctx.sessionId,
          title: customTitle || undefined,
        });

        const forkSessionId = payload.session_id;
        if (!forkSessionId) throw new Error("session.fork did not return a session id");

        const originalSessionId = ctx.sessionId;
        ctx.updateSession(forkSessionId);
        ctx.clearEntries();
        ctx.addItem(addCommandEcho(forkSessionId, customTitle ? `/branch ${customTitle}` : "/branch"));
        ctx.addItem(
          addInfo(
            forkSessionId,
            "You are now in the new branch " +
              `(session ${forkSessionId}).\n` +
              `Use /resume ${originalSessionId} to return to the original.`,
            "i",
          ),
        );
        ctx.setSessionTitle(payload.title || "");
        await ctx.restoreHistory(forkSessionId);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        // Translate backend filesystem-level errors to user-friendly messages
        const userMessage =
          message.includes("source session not found")
            ? "No conversation to branch"
            : message.includes("target session already exists")
              ? "Branch session already exists"
              : `branch failed: ${message}`;
        ctx.addItem(addError(ctx.sessionId, userMessage));
      }
    },
  };
}
