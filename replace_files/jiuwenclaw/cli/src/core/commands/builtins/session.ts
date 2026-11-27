import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export function createSessionCommand(): SlashCommand {
  return {
    name: "session",
    altNames: ["remote", "sessions"],
    description: "Show remote session URL and QR code",
    usage: "/session",
    example: "/remote",
    kind: CommandKind.BUILT_IN,
    action: async (ctx) => {
      try {
        const payload = await ctx.request<{
          session_id?: string;
          remote_url?: string;
          qr_text?: string;
        }>("command.session", {});
        ctx.addItem(
          addInfo(ctx.sessionId, "Session details", "s", {
            view: "kv",
            title: "Session",
            items: [
              { label: "current", value: payload.session_id ?? ctx.sessionId },
              { label: "remote", value: payload.remote_url ?? "not available" },
              ...(payload.qr_text ? [{ label: "qr", value: payload.qr_text }] : []),
            ],
          }),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `session failed: ${message}`));
      }
    },
  };
}
