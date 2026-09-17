import { makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

const FOLD_MODES = ["none", "tools", "thinking", "all"] as const;

export function createFoldCommand(): SlashCommand {
  return {
    name: "fold",
    description: "Fold transcript sections",
    usage: "/fold <none|tools|thinking|all>",
    example: "/fold tools",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async () => [...FOLD_MODES],
    action: async (ctx, args) => {
      const mode = args.trim();
      if (!FOLD_MODES.includes(mode as (typeof FOLD_MODES)[number])) {
        ctx.addItem(makeItem(ctx.sessionId, "error", "usage: /fold <none|tools|thinking|all>"));
        return;
      }
      ctx.setTranscriptFoldMode(mode as (typeof FOLD_MODES)[number]);
      ctx.addItem(makeItem(ctx.sessionId, "info", `Transcript fold set to ${mode}`, "f"));
    },
  };
}
