import { addError, addDiff } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import type { TurnDiff, GitDiffData } from "../../types.js";

interface DiffPayload {
  type: "list";
  turns: TurnDiff[];
  gitDiff?: GitDiffData | null;
}

export function createDiffCommand(): SlashCommand {
  return {
    name: "diff",
    description: "View uncommitted changes and per-turn diffs",
    usage: "/diff",
    example: "/diff",
    kind: CommandKind.BUILT_IN,
    action: async (ctx, args) => {
      try {
        const payload = await ctx.request<DiffPayload>("command.diff", {}, 60_000);
        const turns = payload.turns || [];
        const gitDiff = payload.gitDiff || null;

        if (ctx.enterDiffViewer) {
          ctx.enterDiffViewer(payload as unknown as Record<string, unknown>);
          return;
        }

        const parts: string[] = [];
        if (gitDiff && gitDiff.stats.filesChanged > 0) {
          parts.push(
            `Working tree: ${gitDiff.stats.filesChanged} files, +${gitDiff.stats.linesAdded} -${gitDiff.stats.linesRemoved}`
          );
        }
        if (turns.length > 0) {
          parts.push(`${turns.length} turn(s) with file changes`);
        }
        const summary = parts.length > 0
          ? parts.join(" | ")
          : "No file changes in this session";

        ctx.addItem(
          addDiff(ctx.sessionId, summary, { turns, gitDiff }),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `diff failed: ${message}`));
      }
    },
  };
}
