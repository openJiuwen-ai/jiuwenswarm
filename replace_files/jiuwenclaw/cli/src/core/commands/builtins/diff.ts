import { addError, addDiff } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import type { TurnDiff } from "../../types.js";

interface DiffPayload {
  type: "list";
  turns: TurnDiff[];
}

export function createDiffCommand(): SlashCommand {
  return {
    name: "diff",
    description: "View uncommitted changes and per-turn diffs",
    usage: "/diff",
    example: "/diff",
    kind: CommandKind.BUILT_IN,
    action: async (ctx) => {
      try {
        const payload = await ctx.request<DiffPayload>("command.diff", {});
        const turns = payload.turns || [];
        const summary = turns.length > 0
          ? `Found ${turns.length} turn(s) with file changes`
          : "No file changes in this session";
        ctx.addItem(
          addDiff(ctx.sessionId, summary, { turns }),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `diff failed: ${message}`));
      }
    },
  };
}
