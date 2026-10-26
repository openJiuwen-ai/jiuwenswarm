import { makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

/**
 * /review - Review a pull request locally in your current session.
 * Usage: /review [PR number or URL]
 *
 * Sends the raw /review command to the backend. The backend parses it,
 * builds the review prompt via build_review_prompt, and
 * forwards it to the Agent. This keeps a single source of truth for the
 * review prompt template.
 */
export function createReviewCommand(): SlashCommand {
  return {
    name: "review",
    description: "Review a pull request locally in your current session",
    usage: "/review [PR number or URL]",
    example: "/review 123",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: (ctx, args) => {
      const command = args.trim() ? `/review ${args.trim()}` : "/review";
      const requestId = ctx.sendMessage(command);
      if (!requestId) {
        ctx.addItem(
          makeItem(ctx.sessionId, "error", "offline: waiting for reconnect before sending review request"),
        );
      }
    },
  };
}
