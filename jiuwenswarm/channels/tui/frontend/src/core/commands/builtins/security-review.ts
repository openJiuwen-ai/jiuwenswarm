import { makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

/**
 * /security-review - Security review of pending changes on the current branch.
 * Usage: /security-review [optional focus instructions]
 *
 * Sends the raw /security-review command to the backend. The backend parses it,
 * builds the security review prompt via build_security_review_prompt, and
 * forwards it to the Agent. This keeps a single source of truth for the
 * security review prompt template.
 */
export function createSecurityReviewCommand(): SlashCommand {
  return {
    name: "security-review",
    description:
      "Complete a security review of the pending changes on the current branch",
    usage: "/security-review [optional focus instructions]",
    example: "/security-review",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: (ctx, args) => {
      const command = args.trim()
        ? `/security-review ${args.trim()}`
        : "/security-review";
      const requestId = ctx.sendMessage(command);
      if (!requestId) {
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            "offline: waiting for reconnect before sending security review request",
          ),
        );
      }
    },
  };
}
