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
    action: async (ctx, args) => {
      const arg = args.trim();
      const index = arg ? Number.parseInt(arg, 10) : 1;
      if (!Number.isInteger(index) || index < 1) {
        ctx.addItem(
          addError(ctx.sessionId, `Usage: /copy [N] where N is 1, 2, 3, ... Got: ${arg}`),
        );
        return;
      }

      const texts = getRecentAssistantMessages(ctx);
      if (texts.length === 0) {
        ctx.addItem(addError(ctx.sessionId, "No assistant message to copy"));
        return;
      }

      if (index > texts.length) {
        const countText = texts.length === 1 ? "message" : "messages";
        ctx.addItem(
          addError(ctx.sessionId, `Only ${texts.length} assistant ${countText} available to copy`),
        );
        return;
      }

      const text = texts[index - 1];

      const ok = await copyToClipboard(text);
      if (!ok) {
        ctx.addItem(addError(ctx.sessionId, "Clipboard integration is unavailable on this system"));
        return;
      }

      ctx.addItem(addInfo(ctx.sessionId, `Copied assistant response #${index} to clipboard`, "c"));
    },
  };
}
