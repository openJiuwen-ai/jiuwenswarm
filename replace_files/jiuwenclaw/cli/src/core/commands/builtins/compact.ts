import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export function createCompactCommand(): SlashCommand {
  return {
    name: "compact",
    description: "Clear conversation history but keep a summary in context",
    usage: "/compact [instructions]",
    example: "/compact focus the summary on architectural decisions",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      const instructions = args.trim();
      try {
        const payload = await ctx.request<{ instructions?: string }>(
          "command.compact",
          instructions ? { instructions } : {},
        );
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            payload?.instructions
              ? `Compact requested with custom instructions: ${payload.instructions}`
              : "Compact requested with default instructions",
            "i",
          ),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `compact failed: ${message}`));
      }
    },
  };
}
