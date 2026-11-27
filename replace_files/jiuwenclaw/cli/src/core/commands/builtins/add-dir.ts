import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export function createAddDirCommand(): SlashCommand {
  return {
    name: "add-dir",
    description: "Add a new working directory",
    usage: "/add-dir <path>",
    example: "/add-dir ../another-repo",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      const directoryPath = args.trim();
      try {
        const payload = await ctx.request<{ path?: string; remember?: boolean }>(
          "command.add_dir",
          {
            ...(directoryPath ? { path: directoryPath } : {}),
            remember: false,
          },
        );
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            payload?.path
              ? `Requested working directory: ${payload.path}`
              : "Requested add-dir flow without explicit path",
            "i",
            {
              view: "kv",
              title: "Add Directory",
              items: [
                {
                  label: "path",
                  value: typeof payload?.path === "string" ? payload.path : "(interactive)",
                },
                { label: "remember", value: String(Boolean(payload?.remember)) },
              ],
            },
          ),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `add-dir failed: ${message}`));
      }
    },
  };
}
