import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export function createChromeCommand(): SlashCommand {
  return {
    name: "chrome",
    description: "Browser integration settings",
    usage: "/chrome",
    example: "/chrome",
    kind: CommandKind.BUILT_IN,
    action: async (ctx) => {
      try {
        await ctx.request("command.chrome", {});
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            "Chrome integration command was dispatched to the agent backend",
            "i",
          ),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `chrome failed: ${message}`));
      }
    },
  };
}
