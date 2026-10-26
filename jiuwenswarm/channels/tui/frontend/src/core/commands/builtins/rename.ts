import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

interface RenamePayload {
  session_id: string;
  title: string;
  previous_title?: string;
}

export function createRenameCommand(): SlashCommand {
  return {
    name: "rename",
    description: "Rename the current session title",
    usage: "/rename [new title]",
    example: "/rename My Debug Session",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      const value = args.trim();
      try {
        if (value === "") {
          const payload = await ctx.request<{ session_id: string; title: string }>(
            "session.rename",
            {},
          );
          const currentTitle = payload.title || "(untitled)";
          ctx.addItem(
            addInfo(ctx.sessionId, `Current session title: ${currentTitle}`, "r", {
              view: "kv",
              title: "Rename",
              items: [
                { label: "session", value: ctx.sessionId },
                { label: "title", value: currentTitle },
              ],
            }),
          );
          ctx.setSessionTitle(payload.title || "");
          return;
        }

        const payload = await ctx.request<RenamePayload>(
          "session.rename",
          { title: value },
        );
        ctx.addItem(
          addInfo(ctx.sessionId, `Session renamed to "${payload.title}"`, "r", {
            view: "kv",
            title: "Rename",
            items: [
              { label: "session", value: payload.session_id },
              { label: "previous", value: payload.previous_title || "(untitled)" },
              { label: "new", value: payload.title },
            ],
          }),
        );
        ctx.setSessionTitle(payload.title);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `rename failed: ${message}`));
      }
    },
  };
}