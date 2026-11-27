import { makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export function createViewCommand(): SlashCommand {
  return {
    name: "view",
    description: "Switch transcript density",
    usage: "/view <compact|detailed>",
    example: "/view compact",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async () => ["compact", "detailed"],
    action: async (ctx, args) => {
      const nextMode = args.trim();
      if (nextMode !== "compact" && nextMode !== "detailed") {
        ctx.addItem(makeItem(ctx.sessionId, "error", "usage: /view <compact|detailed>"));
        return;
      }
      ctx.setTranscriptMode(nextMode);
      ctx.addItem(makeItem(ctx.sessionId, "info", `Transcript view set to ${nextMode}`, "v"));
    },
  };
}
