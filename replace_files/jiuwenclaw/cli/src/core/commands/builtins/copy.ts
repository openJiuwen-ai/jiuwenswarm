import { copyToClipboard } from "../clipboard.js";
import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

function getRecentAssistantMessages(ctx: Parameters<SlashCommand["action"]>[0]): string[] {
  const texts: string[] = [];
  for (let index = ctx.entries.length - 1; index >= 0; index -= 1) {
    const entry = ctx.entries[index];
    if (!entry || entry.kind !== "assistant") continue;
    const text = entry.content.trim();
    if (!text) continue;
    texts.push(text);
  }
  return texts;
}

export function createCopyCommand(): SlashCommand {
  return {
    name: "copy",
    description: "Copy the latest assistant response to clipboard (or /copy N for the Nth-latest)",
    usage: "/copy [N]",
    example: "/copy 2",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: (ctx, args) => {
      const arg = args.trim();
      const index = arg ? Number.parseInt(arg, 10) : 1;
      if (!Number.isInteger(index) || index < 1) {
        ctx.addItem(
          addError(ctx.sessionId, `Usage: /copy [N] where N is 1, 2, 3, ... Got: ${arg}`),
        );
        return;
      }

      const texts = getRecentAssistantMessages(ctx);
      const text = texts[index - 1];
      if (!text) {
        ctx.addItem(addError(ctx.sessionId, `No assistant response found for /copy ${index}`));
        return;
      }

      if (!copyToClipboard(text)) {
        ctx.addItem(addError(ctx.sessionId, "Clipboard integration is unavailable on this system"));
        return;
      }

      ctx.addItem(addInfo(ctx.sessionId, `Copied assistant response #${index} to clipboard`, "c"));
    },
  };
}
